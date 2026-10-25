"""LAST_ERROR_CLEAR (25.10): транспортный last_error снимается первым успешным
ответом модели; содержательные ошибки остаются."""
from __future__ import annotations

import asyncio

import pytest

from src.governor import AgentGovernor, LLMTransportError


class _Reg:
    def __init__(self, **state):
        self.state = dict(state)
        self.writes = []

    def update_state(self, key, value):
        self.state[key] = value
        self.writes.append(key)
        return True

    def get_state(self, key):
        return self.state.get(key)

    def increment_state(self, key):
        self.state[key] = self.state.get(key, 0) + 1


def _gov(last_error, answer="ok"):
    g = AgentGovernor.__new__(AgentGovernor)   # без __init__: без сети и registry на диске
    g.registry = _Reg(status="ERROR", last_error=last_error)
    g.identity_text = None

    async def call(*a, **k):
        if isinstance(answer, BaseException):
            raise answer
        return answer
    g._call_llm_with_timeout = call
    return g


@pytest.mark.parametrize("err", [
    "LLM_UNAVAILABLE: transport gate OPEN, retry in 11s",
    "LLM_TIMEOUT: LLM call exceeded timeout",
    "DISCUSS_TIMEOUT: LLM call exceeded timeout",
])
def test_success_clears_transport_error(err, caplog):
    g = _gov(err)
    caplog.set_level("INFO")
    assert asyncio.run(g._process("p", depth=0)) == "ok"
    assert g.registry.state["last_error"] is None and g.registry.state["status"] == "READY"
    assert any("[STATE] last_error cleared" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("err", [
    "KERNEL_FAULT: boom", "RETRY_LIMIT_EXCEEDED: x", "RECOVERY_FAILED: x",
    "STAGNATION_BREAK: identical failure", "DISCUSS_FAULT: x", None,
])
def test_success_keeps_content_errors_and_no_extra_write(err):
    g = _gov(err)
    asyncio.run(g._process("p", depth=0))
    assert g.registry.state["last_error"] == err
    assert "last_error" not in g.registry.writes


def test_transport_failure_then_recovery():
    g = _gov(None, answer=LLMTransportError("transport gate OPEN, retry in 11s"))
    marker = asyncio.run(g._process("p", depth=0))
    assert marker == "[ERR: LLM_UNAVAILABLE]"
    assert g.registry.state["last_error"].startswith("LLM_UNAVAILABLE:")

    async def ok(*a, **k):
        return "back"
    g._call_llm_with_timeout = ok
    assert asyncio.run(g._process("p", depth=0)) == "back"
    assert g.registry.state["last_error"] is None


def test_discuss_success_clears_transport_error():
    g = _gov("DISCUSS_TIMEOUT: LLM call exceeded timeout", answer="doc answer")
    g._observe_metrics = lambda *a, **k: None
    assert asyncio.run(g.execute_discuss_request("q", "doc")) == "doc answer"
    assert g.registry.state["last_error"] is None
