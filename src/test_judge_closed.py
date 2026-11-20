"""Судья закрыт при сбое + сигнал деградации (20.11, решение Оператора: прежний «статус-кво» отменён).

Инварианты:
  J1  нетранспортный сбой судьи (любое исключение _verify_compliance) -> ОДИН повтор тем же текстом -> при повторном
      сбое ответ блокируется кодом JUDGE_ERROR; текст модели / коррекции наружу не выходит (оба прохода).
      Успешный повтор -> обычный путь. Транспортные ветки не меняются (test_judge_transport.py).
  J2  счётчик сбоев ПОДРЯД (сброс первым успешным вердиктом); на пороге config.JUDGE_DEGRADED_N -- состояние
      JUDGE_DEGRADED в реестре и одно сообщение Оператору; повторно -- нет; при восстановлении -- одно сообщение.
  AST в _enforce_compliance ни один обработчик исключения не возвращает response / corrected.
Сеть не затрагивается: изолированный Registry на tmp_path, http-клиент -- двойник.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def ok(text):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"response": text})
    return resp


@pytest.fixture
def gov(tmp_path):
    import httpx
    from src.registry import Registry
    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    g = governor.AgentGovernor(http_client=client)
    yield g
    Registry._instance = None


def run(coro):
    return asyncio.run(coro)


def results(gov):
    import governor
    R, V = governor.ComplianceResult, governor.ComplianceVerdict
    return R, V


def judge(gov, script):
    """Подменяет _verify_compliance сценарием: элемент-исключение бросается, иначе возвращается. Список вызовов."""
    calls = []
    it = iter(script)

    async def fake(text):
        calls.append(text)
        x = next(it)
        if isinstance(x, BaseException):
            raise x
        return x
    gov._verify_compliance = fake
    return calls


def verdict(gov, name, rule=None):
    import governor
    return governor.ComplianceResult(getattr(governor.ComplianceVerdict, name), rule)


def st(gov, key):
    return gov.registry.get_state(key)


# --- J1: первый проход ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("x"), KeyError("k")])
def test_first_pass_error_retried_once_then_blocked(gov, exc):
    calls = judge(gov, [exc, exc])
    out = run(gov._enforce_compliance("секретный текст модели", "вопрос"))
    assert out == "[ERR: JUDGE_ERROR]" and "секретный" not in out
    assert calls == ["секретный текст модели"] * 2                      # один повтор, тот же текст
    assert st(gov, "last_compliance_status") == "UNCHECKED" and st(gov, "compliance_check_failures") == 1
    assert gov.monitor.get_unchecked_rate() == 1.0 and st(gov, "judge_fail_streak") == 1


def test_first_pass_retry_success_returns_answer(gov):
    calls = judge(gov, [RuntimeError("boom"), verdict(gov, "PASS")])
    assert run(gov._enforce_compliance("ответ", "вопрос")) == "ответ"
    assert len(calls) == 2 and st(gov, "last_compliance_status") == "PASS"
    assert st(gov, "judge_fail_streak") == 0 and st(gov, "compliance_check_failures") == 0


def test_transport_error_on_retry_goes_to_transport_branch(gov):
    import governor
    judge(gov, [RuntimeError("boom"), governor.LLMTransportError("down")])
    out = run(gov._enforce_compliance("ответ", "вопрос"))
    assert out == "[ERR: LLM_UNAVAILABLE]" and st(gov, "judge_fail_streak") == 0   # транспорт -- не сбой судьи


def test_transport_error_is_not_retried(gov):
    import governor
    calls = judge(gov, [governor.LLMTransportError("down")])
    assert run(gov._enforce_compliance("ответ", "вопрос")) == "[ERR: LLM_UNAVAILABLE]" and len(calls) == 1


# --- J1: повторная проверка после коррекции -------------------------------------------------------------------
def test_reverify_error_retried_once_then_blocked_no_corrected_text(gov):
    calls = judge(gov, [verdict(gov, "FAIL", "POL_001"), RuntimeError("boom"), RuntimeError("boom")])
    gov._self_correct = AsyncMock(return_value="исправленный текст")
    out = run(gov._enforce_compliance("исходный текст", "вопрос"))
    assert out == "[ERR: JUDGE_ERROR]" and "исправленный" not in out and "исходный" not in out
    assert calls == ["исходный текст", "исправленный текст", "исправленный текст"]
    assert st(gov, "last_compliance_status") == "UNCHECKED_AFTER_CORRECTION"
    assert st(gov, "compliance_check_failures") == 1 and st(gov, "judge_fail_streak") == 1


def test_reverify_retry_success_returns_corrected(gov):
    judge(gov, [verdict(gov, "FAIL", "POL_001"), RuntimeError("boom"), verdict(gov, "PASS")])
    gov._self_correct = AsyncMock(return_value="исправленный текст")
    assert run(gov._enforce_compliance("исходный текст", "вопрос")) == "исправленный текст"
    assert st(gov, "last_compliance_status") == "PASS_AFTER_CORRECTION" and st(gov, "judge_fail_streak") == 0


# --- J2: счётчик подряд, порог, одно сообщение, восстановление ---------------------------------------------------
def _fail(gov, exc=None):
    e = exc or RuntimeError("boom")
    judge(gov, [e, e])
    return run(gov._enforce_compliance("ответ", "вопрос"))


def _pass(gov):
    judge(gov, [verdict(gov, "PASS")])
    return run(gov._enforce_compliance("ответ", "вопрос"))


def test_threshold_default_is_three():
    import config
    assert config.JUDGE_DEGRADED_N == 3


def test_streak_counts_consecutive_and_resets_on_first_verdict(gov):
    _fail(gov), _fail(gov)
    assert st(gov, "judge_fail_streak") == 2 and st(gov, "judge_state") == "OK" and gov.pop_judge_notice() == ""
    assert _pass(gov) == "ответ"
    assert st(gov, "judge_fail_streak") == 0 and gov.pop_judge_notice() == ""        # до порога -- без сообщений
    _fail(gov), _fail(gov)
    assert st(gov, "judge_fail_streak") == 2 and st(gov, "judge_state") == "OK"     # счёт заново, не 4


@pytest.mark.parametrize("name", ["FAIL", "HALT"])
def test_any_real_verdict_resets_streak(gov, name):
    _fail(gov)
    judge(gov, [verdict(gov, name, "POL_001"), verdict(gov, "PASS")])
    gov._self_correct = AsyncMock(return_value="исправлено")
    run(gov._enforce_compliance("ответ", "вопрос"))
    assert st(gov, "judge_fail_streak") == 0


def test_unparseable_verdict_neither_counts_nor_resets(gov):
    _fail(gov)
    judge(gov, [verdict(gov, "ERROR")])
    assert run(gov._enforce_compliance("ответ", "вопрос")) == "[ERR: JUDGE_UNPARSEABLE]"
    assert st(gov, "judge_fail_streak") == 1


def test_degraded_at_threshold_one_message_then_silence(gov):
    for _ in range(2):
        assert _fail(gov) == "[ERR: JUDGE_ERROR]"
    assert gov.pop_judge_notice() == ""
    assert _fail(gov, KeyError("k")) == "[ERR: JUDGE_ERROR]"
    assert st(gov, "judge_state") == "JUDGE_DEGRADED" and st(gov, "judge_fail_reason") == "KeyError"
    note = gov.pop_judge_notice()
    assert note == "Проверяющий сбоит 3 раз подряд — ответы блокируются; причина: KeyError."
    assert gov.pop_judge_notice() == ""                                  # выдано один раз
    for _ in range(4):
        assert _fail(gov) == "[ERR: JUDGE_ERROR]"                        # ответы по-прежнему блокируются
        assert gov.pop_judge_notice() == ""                              # без повторов до восстановления
    assert st(gov, "judge_fail_streak") == 7 and st(gov, "judge_state") == "JUDGE_DEGRADED"


def test_recovery_one_message_and_degraded_again_messages_again(gov):
    for _ in range(3):
        _fail(gov)
    gov.pop_judge_notice()
    assert _pass(gov) == "ответ"
    assert st(gov, "judge_state") == "OK" and st(gov, "judge_fail_streak") == 0 and st(gov, "judge_fail_reason") is None
    assert gov.pop_judge_notice() == "Проверяющий снова работает." and gov.pop_judge_notice() == ""
    assert _pass(gov) == "ответ" and gov.pop_judge_notice() == ""
    for _ in range(3):
        _fail(gov)
    assert gov.pop_judge_notice().startswith("Проверяющий сбоит 3 раз подряд")


def test_threshold_from_config(gov, monkeypatch):
    import config
    monkeypatch.setattr(config, "JUDGE_DEGRADED_N", 1)
    _fail(gov)
    assert st(gov, "judge_state") == "JUDGE_DEGRADED" and "сбоит 1 раз" in gov.pop_judge_notice()


def test_degraded_state_survives_restart_without_repeat(gov, tmp_path):
    import governor
    from src.registry import Registry
    for _ in range(3):
        _fail(gov)
    gov.pop_judge_notice()
    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    g2 = governor.AgentGovernor(http_client=MagicMock())
    assert st(g2, "judge_state") == "JUDGE_DEGRADED" and st(g2, "judge_fail_streak") == 3
    assert _fail(g2) == "[ERR: JUDGE_ERROR]" and g2.pop_judge_notice() == ""
    assert _pass(g2) == "ответ" and g2.pop_judge_notice() == "Проверяющий снова работает."


def test_registry_rejects_unknown_judge_state(gov):
    assert gov.registry.update_state("judge_state", "BROKEN") is False and st(gov, "judge_state") == "OK"


# --- бот: сообщение -- отдельной строкой в конце ближайшего ответа, один раз --------------------------------------
def test_bot_appends_judge_notice_once(gov):
    import bot   # обычный import: сломанный модуль ядра = красный тест, не пропуск
    for _ in range(3):
        out = _fail(gov)
    shown = bot._explain_for_user(gov, out)
    assert shown.startswith("[ERR: JUDGE_ERROR] " + bot._ERR_TEXT["JUDGE_ERROR"])
    assert shown.endswith("\nПроверяющий сбоит 3 раз подряд — ответы блокируются; причина: RuntimeError.")
    assert bot._explain_for_user(gov, out) == "[ERR: JUDGE_ERROR] " + bot._ERR_TEXT["JUDGE_ERROR"]
    assert bot._explain_for_user(gov, _pass(gov)) == "ответ\nПроверяющий снова работает."
    assert bot._explain_for_user(gov, "ответ") == "ответ"
    assert bot._explain_for_user(object(), "ответ") == "ответ" and bot._explain_for_user(MagicMock(), "ответ") == "ответ"


def test_judge_error_is_yellow_service_code():
    import governor
    assert "JUDGE_ERROR" in governor.YELLOW_CODES and governor.is_service_marker("[ERR: JUDGE_ERROR]")


# --- сторож (AST): обработчик исключения в _enforce_compliance не возвращает текст ---------------------------------
def _enforce_ast():
    import governor
    src = inspect.getsource(governor.AgentGovernor._enforce_compliance)
    return ast.parse("class _X:\n" + src).body[0].body[0]


def test_ast_no_except_handler_returns_response_or_corrected():
    fn = _enforce_ast()
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 6                                           # 3 блока try: транспорт + прочее
    for h in handlers:
        rets = [n for n in ast.walk(h) if isinstance(n, ast.Return)]
        assert rets, "обработчик без return -- управление ушло бы дальше с непроверенным текстом"
        for r in rets:
            names = {n.id for n in ast.walk(r) if isinstance(n, ast.Name)}
            assert not names & {"response", "corrected"}, f"строка {r.lineno}: обработчик возвращает текст модели"
            assert isinstance(r.value, ast.Call), f"строка {r.lineno}: возврат не через служебный код"
            f = r.value.func
            called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            assert called in {"_transport_failure", "_judge_failure", "log_service_code"}, called


def test_ast_enforce_calls_judge_only_through_retry_wrapper():
    fn = _enforce_ast()
    attrs = [n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)]
    assert "_verify_compliance" not in attrs and attrs.count("_judge_call") == 2


def test_readme_limitation_matches_behaviour():
    readme = Path(__file__).resolve().parent.parent / "README.md"
    if not readme.exists():
        pytest.skip("README.md вне дерева")
    text = readme.read_text(encoding="utf-8")
    assert "ответ уходит\n  непроверенным" not in text and "JUDGE_ERROR" in text and "JUDGE_DEGRADED" in text
