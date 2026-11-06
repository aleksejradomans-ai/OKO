"""Тесты CIRCUIT_BREAKER_WIRED: подключение src/circuit_breaker.py к governor.

Размещение: C:\\oko\\src\\test_breaker_wiring.py. Запуск:
    python -m pytest src\\test_breaker_wiring.py -q

LLM не используется: _process и _enforce_compliance подменяются, а время
предохранителя -- ручными часами (circuit_breaker._now).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC_DIR = Path(__file__).resolve().parent


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import httpx
    import src.circuit_breaker as cb
    from src.monitor import ComplianceEvent
    from src.registry import Registry

    clock = Clock()
    monkeypatch.setattr(cb, "_now", clock)

    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    gov = governor.AgentGovernor(http_client=client)

    state = {"reply": "ответ", "compliance": "PASS", "delay": 0.0}
    calls = {"process": 0}

    async def fake_process(prompt, depth=0, *a, **k):
        calls["process"] += 1
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        return state["reply"]

    async def fake_enforce(raw, prompt):
        gov.registry.update_state("last_compliance_status", state["compliance"])
        return raw

    gov._process = fake_process
    gov._enforce_compliance = fake_enforce

    handler = _Capture()
    governor.logger.addHandler(handler)
    old = governor.logger.level
    governor.logger.setLevel(logging.DEBUG)

    class E:
        pass

    e = E()
    e.gov, e.clock, e.state, e.calls, e.cap, e.Event = gov, clock, state, calls, handler, ComplianceEvent
    e.open = lambda: [gov.monitor.record_event(ComplianceEvent.VIOLATION) for _ in range(10)]
    e.run = lambda text="вопрос": asyncio.run(gov.execute_request(text))
    yield e
    governor.logger.removeHandler(handler)
    governor.logger.setLevel(old)
    Registry._instance = None


def test_closed_passes_through(env):
    assert env.run() == "ответ"
    assert env.calls["process"] == 1
    assert env.gov.breaker_state == "CLOSED"


def test_open_refuses_without_llm_and_stays_yellow(env):
    env.open()
    assert env.run() == "[ERR: SYSTEM_DEGRADED]"
    assert env.calls["process"] == 0
    assert env.gov.registry.get_state("total_requests") == 0
    assert env.gov.breaker_state == "OPEN"
    assert [r for r in env.cap.records if r.levelno >= logging.ERROR] == []
    assert any("[ERR: SYSTEM_DEGRADED]" in r.getMessage() and r.levelno == logging.WARNING
               for r in env.cap.records)


def test_open_within_cooldown_still_refuses(env):
    env.open()
    env.run()
    env.clock.advance(59)
    assert env.run() == "[ERR: SYSTEM_DEGRADED]"
    assert env.calls["process"] == 0


def test_half_open_lets_exactly_one_probe_through_under_concurrency(env):
    env.open()
    env.run()
    env.clock.advance(61)
    env.state["delay"] = 0.05

    async def both():
        return await asyncio.gather(
            env.gov.execute_request("a"), env.gov.execute_request("b"))

    results = asyncio.run(both())
    assert sorted(results) == sorted(["ответ", "[ERR: SYSTEM_DEGRADED]"])
    assert env.calls["process"] == 1
    assert env.gov._probe_in_flight is False


def test_two_successful_probes_close_and_stale_window_does_not_reopen(env):
    env.open()
    env.run()
    env.clock.advance(61)
    assert env.run() == "ответ"
    assert env.gov.breaker_state == "HALF_OPEN"      # одного успеха мало
    assert env.run() == "ответ"
    assert env.gov.breaker_state == "CLOSED"
    assert env.run() == "ответ"                       # старый хвост не переоткрывает
    assert env.gov.breaker_state == "CLOSED"
    assert env.calls["process"] == 3


def test_failed_probe_reopens_for_another_cooldown(env):
    env.open()
    env.run()
    env.clock.advance(61)
    env.state["reply"] = "[ERR: LLM_UNAVAILABLE]"
    assert env.run() == "[ERR: LLM_UNAVAILABLE]"
    assert env.gov.breaker_state == "OPEN"
    env.state["reply"] = "ответ"
    assert env.run() == "[ERR: SYSTEM_DEGRADED]"
    assert env.gov._probe_in_flight is False
    env.clock.advance(61)
    assert env.run() == "ответ"


def test_probe_with_compliance_fail_counts_as_failure(env):
    env.open()
    env.run()
    env.clock.advance(61)
    env.state["compliance"] = "FAIL"
    assert env.run() == "ответ"
    assert env.gov.breaker_state == "OPEN"


def test_probe_flag_released_and_failure_recorded_on_exception(env):
    env.open()
    env.run()
    env.clock.advance(61)

    async def boom(raw, prompt):
        raise RuntimeError("judge exploded")

    env.gov._enforce_compliance = boom
    with pytest.raises(RuntimeError):
        env.run()
    assert env.gov._probe_in_flight is False
    assert env.gov.breaker_state == "OPEN"


def test_breaker_state_is_plain_string_for_status(env):
    assert isinstance(env.gov.breaker_state, str)
    assert env.gov.breaker_state in {"CLOSED", "OPEN", "HALF_OPEN"}


def test_source_contract():
    src = (SRC_DIR / "governor.py").read_text(encoding="utf-8")
    assert "CIRCUIT_BREAKER_WIRED" in src
    assert "from src.circuit_breaker import BreakerState, CircuitBreaker" in src
    assert "self._degraded_since" not in src
    assert "self._maybe_allow_probe" not in src
