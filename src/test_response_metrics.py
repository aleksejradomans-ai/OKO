"""RESPONSE_METRICS (M6) + OBSERVER_WARNING.

1. response_metrics.measure -- чистая функция на эталонных текстах.
2. governor: одна INFO-строка [METRICS] на ответ (point=B -- после судьи,
   point=D -- /discuss), без текста ответа; служебные маркеры не мерятся;
   сбой метрики -> WARNING [METRICS_FAIL], ответ не меняется; сбой
   структурной проверки -> WARNING (не ERROR).
AgentGovernor создаётся через __new__ с подменёнными зависимостями:
Ollama, Registry и судья не нужны.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import governor as gov_mod
from governor import AgentGovernor, LLMTimeoutError
from response_metrics import measure

SECRET = "Квазипротокол"  # слово-метка: не должно попасть в лог


# ------------------------------------------------------------------
# 1. measure()
# ------------------------------------------------------------------

def test_neutral_factual_answer():
    m = measure("JUDGE_UNPARSEABLE означает нераспознанный вердикт судьи. "
                "Ответ скрывается, растёт счётчик compliance_check_failures.")
    assert m.affective_decoupling == 1.0
    assert m.resistance_level == 0.0
    assert m.cognitive_empathy == 0.0
    assert m.intent == "inform" and m.intent_share == 1.0
    assert m.cjk == 0.0 and m.cyr > 0.5


def test_emotional_reassuring_answer():
    m = measure("Мне очень жаль, я понимаю ваши чувства. Не волнуйтесь, всё будет хорошо.")
    assert m.affective_decoupling < 0.5
    assert m.cognitive_empathy > 0.0
    assert m.intent == "reassure"


def test_refusal():
    m = measure("Я не могу это сделать. Это невозможно.")
    assert m.intent == "refuse" and m.resistance_level == 1.0


def test_hedge_counts_half():
    assert measure("Однако это работает.").resistance_level == 0.5


def test_numbered_steps_are_instruct():
    m = measure("1. Откройте PowerShell.\n2. Запустите скрипт.\n3. Проверьте вывод.")
    assert (m.intent, m.intent_share) == ("instruct", 1.0)


def test_question_is_ask_and_user_directed():
    m = measure("Какой файл вы имеете в виду?")
    assert m.intent == "ask" and m.cognitive_empathy == 1.0


def test_cjk_drift_is_visible():
    m = measure("Ответ: 这是一个测试，没有问题。")
    assert m.cjk > 0.5


@pytest.mark.parametrize("text", ["", "   ", None, "\n\n"])
def test_empty_is_safe(text):
    m = measure(text)
    assert m.intent == "none" and m.affective_decoupling == 1.0


def test_values_are_bounded():
    m = measure("жаль " * 50 + "не могу. " * 20)
    for v in (m.cognitive_empathy, m.affective_decoupling, m.resistance_level,
              m.intent_share, m.cyr, m.lat, m.cjk):
        assert 0.0 <= v <= 1.0


def test_log_line_has_no_text():
    line = measure(f"{SECRET} — это тестовое слово. Вы его видите?").to_log()
    assert SECRET not in line and "тестовое" not in line
    assert line.startswith("ce=") and "intent=" in line


# ------------------------------------------------------------------
# 2. governor
# ------------------------------------------------------------------

class _Cap:
    """Захват записей логгера ядра (propagate может быть выключен)."""

    def __init__(self):
        self.records = []
        outer = self

        class _H(logging.Handler):
            def emit(self, r):
                outer.records.append(r)

        self._h = _H(logging.DEBUG)

    def __enter__(self):
        self._lvl = gov_mod.logger.level
        gov_mod.logger.addHandler(self._h)
        gov_mod.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *a):
        gov_mod.logger.removeHandler(self._h)
        gov_mod.logger.setLevel(self._lvl)

    def metrics(self):
        return [r for r in self.records if "[METRICS]" in r.getMessage()]

    def at_least(self, level):
        return [r for r in self.records if r.levelno >= level]


def _gov(final="итоговый ответ", raw="сырой ответ"):
    g = AgentGovernor.__new__(AgentGovernor)
    g.registry = MagicMock()
    g.monitor = MagicMock()
    g.identity_text = None
    g.manifesto = MagicMock()
    g._process = AsyncMock(return_value=raw)
    g._enforce_compliance = AsyncMock(return_value=final)
    g._observe_structure = MagicMock()
    return g


def test_request_logs_one_metrics_line_at_info_without_text():
    g = _gov(final=f"{SECRET}: всё штатно. Проверьте лог.")
    with _Cap() as cap:
        out = asyncio.run(g._run_request("вопрос", None))
    assert out == f"{SECRET}: всё штатно. Проверьте лог."
    lines = cap.metrics()
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert "point=B" in lines[0].getMessage()
    assert SECRET not in lines[0].getMessage()


def test_service_marker_is_not_measured():
    g = _gov(final="[ERR: JUDGE_UNPARSEABLE]")
    with _Cap() as cap:
        asyncio.run(g._run_request("вопрос", None))
    assert cap.metrics() == []


def test_raw_service_marker_skips_judge_and_metrics():
    g = _gov(raw="[ERR: LLM_TIMEOUT]")
    with _Cap() as cap:
        assert asyncio.run(g._run_request("вопрос", None)) == "[ERR: LLM_TIMEOUT]"
    assert cap.metrics() == []
    g._enforce_compliance.assert_not_called()


def test_metrics_failure_is_warning_and_answer_unchanged(monkeypatch):
    def boom(text):
        raise RuntimeError("lexicon broken")

    monkeypatch.setattr(gov_mod, "measure_response", boom)
    g = _gov(final="ответ")
    with _Cap() as cap:
        assert asyncio.run(g._run_request("вопрос", None)) == "ответ"
    fails = [r for r in cap.records if "[METRICS_FAIL]" in r.getMessage()]
    assert len(fails) == 1 and fails[0].levelno == logging.WARNING
    assert cap.at_least(logging.ERROR) == []


def test_structural_check_failure_is_warning_not_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("struct broken")

    monkeypatch.setattr(gov_mod, "structural_check", boom)
    g = AgentGovernor.__new__(AgentGovernor)
    g.identity_text = None
    g.manifesto = MagicMock()
    with _Cap() as cap:
        g._observe_structure("текст", point="A")
    assert cap.at_least(logging.ERROR) == []
    assert any("Structural check failed" in r.getMessage() and r.levelno == logging.WARNING
               for r in cap.records)


def _discuss_gov(answer=None, exc=None):
    g = AgentGovernor.__new__(AgentGovernor)
    g.registry = MagicMock()
    g.identity_text = None
    g._call_llm_with_timeout = AsyncMock(return_value=answer, side_effect=exc)
    return g


def test_discuss_answer_is_measured_point_d():
    g = _discuss_gov(answer="В документе сказано: порог 0.48.")
    with _Cap() as cap:
        out = asyncio.run(g.execute_discuss_request("что сказано?", "текст документа", 111))
    assert out == "В документе сказано: порог 0.48."
    lines = cap.metrics()
    assert len(lines) == 1 and "point=D" in lines[0].getMessage()


def test_discuss_timeout_is_not_measured():
    g = _discuss_gov(exc=LLMTimeoutError("timeout"))
    with _Cap() as cap:
        out = asyncio.run(g.execute_discuss_request("вопрос", "документ", 111))
    assert out == "[ERR: DISCUSS_TIMEOUT]"
    assert cap.metrics() == []


def test_metrics_never_reach_error_level():
    g = _gov(final="Мне очень жаль. Я не могу. 这是测试?")
    with _Cap() as cap:
        asyncio.run(g._run_request("вопрос", None))
    assert len(cap.metrics()) == 1
    assert cap.at_least(logging.ERROR) == []
