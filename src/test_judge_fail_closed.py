"""Судья fail-closed по содержанию (JUDGE_FAIL_CLOSED).

Размещение: C:\\oko\\src\\test_judge_fail_closed.py
Запуск:     python -m pytest src\\test_judge_fail_closed.py -q

Решение Оператора 17.10 (п. 3):
  * нераспознанный вердикт судьи (ERROR) -> ответ не отдаётся, жёлтый
    JUDGE_UNPARSEABLE (технический сбой формата, не нарушение);
  * FAIL, который не устранила одна коррекция (или коррекция упала не по
    транспорту) -> текст с нарушением не отдаётся, красный
    COMPLIANCE_UNRESOLVED.
Не меняется: исключение при вызове судьи не по транспорту (LLMContentError)
-> ответ уходит UNCHECKED (test_judge_transport.py), транспорт -> жёлтый
LLM_TIMEOUT/LLM_UNAVAILABLE.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


def ok(text):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"response": text})
    return resp


def bad(tag=""):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={f"error{tag}": "model not loaded"})
    return resp


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def red(self):
        return [r for r in self.records if r.levelno >= logging.ERROR]

    def with_marker(self, code):
        return [r for r in self.records if f"[ERR: {code}]" in r.getMessage()]


@pytest.fixture
def gc(tmp_path):
    import httpx
    from src.registry import Registry
    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    gov = governor.AgentGovernor(http_client=client)
    yield gov, client
    Registry._instance = None


@pytest.fixture
def cap():
    import governor
    handler = _Capture()
    lg = governor.logger
    old_level = lg.level
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    yield handler
    lg.removeHandler(handler)
    lg.setLevel(old_level)


def run(coro):
    return asyncio.run(coro)


def state(gov, key):
    return gov.registry.get_state(key)


class TestUnparseableVerdictBlocks:
    @pytest.mark.parametrize("garbage", [
        "Не могу оценить этот текст",
        "FAIL :POL_001",          # пробел перед двоеточием (Red-Team сценарий 5)
        "FAIL:POL_999",           # неизвестное правило
        "",
    ])
    def test_garbage_first_verdict_blocks_response(self, gc, cap, garbage):
        gov, client = gc
        client.post.side_effect = [ok("основной ответ"), ok(garbage)]

        result = run(gov.execute_request("вопрос"))

        assert result == "[ERR: JUDGE_UNPARSEABLE]"
        assert state(gov, "last_compliance_status") == "UNCHECKED"
        assert state(gov, "compliance_check_failures") == 1
        assert len(cap.with_marker("JUDGE_UNPARSEABLE")) == 1
        assert cap.red() == [], [r.getMessage() for r in cap.red()]

    def test_garbage_verdict_after_correction_blocks(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [
            ok("нарушающий ответ"), ok("FAIL:POL_001"),
            ok("исправленный ответ"), ok("кажется, всё нормально"),
        ]

        result = run(gov.execute_request("вопрос"))

        assert result == "[ERR: JUDGE_UNPARSEABLE]"
        assert state(gov, "last_compliance_status") == "UNCHECKED_AFTER_CORRECTION"
        assert state(gov, "compliance_violations") == 1


class TestUnresolvedViolationBlocks:
    def test_fail_after_correction_blocks(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [
            ok("нарушающий ответ"), ok("FAIL:POL_001"),
            ok("всё ещё нарушающий"), ok("FAIL:POL_001"),
        ]

        result = run(gov.execute_request("вопрос"))

        assert result == "[ERR: COMPLIANCE_UNRESOLVED]"
        assert state(gov, "last_compliance_status") == "FAIL_AFTER_CORRECTION"
        assert state(gov, "compliance_violations") == 1
        hits = cap.with_marker("COMPLIANCE_UNRESOLVED")
        assert len(hits) == 1 and hits[0].levelno == logging.ERROR
        assert len(cap.red()) == 1, [r.getMessage() for r in cap.red()]

    def test_self_correct_content_failure_does_not_leak_violation(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [ok("нарушающий ответ"), ok("FAIL:POL_001"), bad("_corr")]

        result = run(gov.execute_request("вопрос"))

        assert result == "[ERR: COMPLIANCE_UNRESOLVED]"
        assert client.post.call_count == 3
        assert state(gov, "compliance_violations") == 1
        assert state(gov, "compliance_check_failures") == 1
        assert len(cap.with_marker("COMPLIANCE_UNRESOLVED")) == 1

    def test_unresolved_violation_counts_in_quality_window(self, gc, cap):
        from src.monitor import ComplianceEvent
        gov, client = gc
        client.post.side_effect = [
            ok("нарушающий ответ"), ok("FAIL:POL_001"),
            ok("всё ещё нарушающий"), ok("FAIL:POL_001"),
        ]
        recorded = []
        orig = gov.monitor.record_event
        gov.monitor.record_event = lambda e: (recorded.append(e), orig(e))

        run(gov.execute_request("вопрос"))

        assert recorded == [ComplianceEvent.VIOLATION]


class TestUnchangedPaths:
    def test_pass_still_returns_response(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [ok("основной ответ"), ok("PASS")]
        assert run(gov.execute_request("вопрос")) == "основной ответ"

    def test_fail_then_pass_still_returns_correction(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [
            ok("нарушающий ответ"), ok("FAIL:POL_001"),
            ok("исправленный ответ"), ok("PASS"),
        ]
        assert run(gov.execute_request("вопрос")) == "исправленный ответ"
        assert state(gov, "last_compliance_status") == "PASS_AFTER_CORRECTION"

    def test_halt_still_blocks_with_halt_code(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [ok("противоречивый ответ"), ok("HALT:POL_002")]
        assert run(gov.execute_request("вопрос")) == "[ERR: HALT_CONDITION]"


class TestBotExplainsNewCodes:
    @pytest.mark.parametrize("code", ["JUDGE_UNPARSEABLE", "COMPLIANCE_UNRESOLVED"])
    def test_bot_has_human_explanation(self, code):
        bot = pytest.importorskip("bot")
        marker = f"[ERR: {code}]"
        explained = bot._explain_error(marker)
        assert explained.startswith(marker) and len(explained) > len(marker) + 10
