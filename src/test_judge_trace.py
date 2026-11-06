"""Тесты JUDGE_TRACE: не-PASS вердикты судьи трассируются WARNING-строкой."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC_DIR = Path(__file__).resolve().parent


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def trace(self):
        return [r for r in self.records if "[JUDGE_TRACE]" in r.getMessage()]


@pytest.fixture
def env(tmp_path):
    import httpx
    from src.registry import Registry

    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    gov = governor.AgentGovernor(http_client=client)
    handler = _Capture()
    governor.logger.addHandler(handler)
    old = governor.logger.level
    governor.logger.setLevel(logging.DEBUG)

    class E:
        pass

    e = E()
    e.gov, e.cap = gov, handler
    e.verdict = lambda raw, text="проверяемый текст": _run(gov, raw, text)
    yield e
    governor.logger.removeHandler(handler)
    governor.logger.setLevel(old)
    Registry._instance = None


def _run(gov, raw, text):
    gov._call_llm_with_timeout = AsyncMock(return_value=raw)
    return asyncio.run(gov._verify_compliance(text))


def test_pass_is_not_traced(env):
    assert env.verdict("PASS").verdict.value == "PASS"
    assert env.cap.trace() == []


def test_halt_is_traced_with_raw_and_text(env):
    res = env.verdict("HALT:POL_002", text="ответ про HALT_CONDITION")
    assert res.verdict.value == "HALT"
    (rec,) = env.cap.trace()
    msg = rec.getMessage()
    assert rec.levelno == logging.WARNING
    assert "verdict=HALT" in msg and "rule=POL_002" in msg
    assert "HALT:POL_002" in msg and "ответ про HALT_CONDITION" in msg


def test_fail_and_error_are_traced(env):
    env.verdict("FAIL:POL_001")
    env.verdict("что-то непонятное")
    verdicts = [r.getMessage() for r in env.cap.trace()]
    assert len(verdicts) == 2
    assert "verdict=FAIL" in verdicts[0] and "verdict=ERROR" in verdicts[1]


def test_long_text_is_truncated(env):
    env.verdict("HALT:POL_002", text="x" * 5000)
    (rec,) = env.cap.trace()
    assert len(rec.getMessage()) < 800


def test_trace_is_never_red(env):
    env.verdict("HALT:POL_002")
    assert all(r.levelno < logging.ERROR for r in env.cap.trace())


def test_source_marker():
    src = (SRC_DIR / "governor.py").read_text(encoding="utf-8")
    assert "[JUDGE_TRACE]" in src
