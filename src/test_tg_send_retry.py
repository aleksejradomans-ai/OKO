"""TG_SEND_RETRY (25.10): сбой сети Telegram при отправке ответа -> повтор, WARNING."""
from __future__ import annotations

import asyncio

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

import bot


class _Msg:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def reply_text(self, text):
        self.calls += 1
        o = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(o, BaseException):
            raise o


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(bot, "SEND_RETRY_DELAYS_S", (0.0, 0.0))


def test_connect_error_retried_then_ok(caplog):
    m = _Msg(NetworkError("httpx.ConnectError: "), None)
    caplog.set_level("INFO")
    assert asyncio.run(bot._reply_with_retry(m, "t", 1)) is True
    assert m.calls == 2
    assert not any(r.levelname in ("ERROR", "CRITICAL") for r in caplog.records)
    assert any("send ok after retry=1" in r.getMessage() for r in caplog.records)


def test_all_attempts_fail_is_warning_not_error(caplog):
    m = _Msg(*[NetworkError("httpx.ConnectError: ")] * 3)
    caplog.set_level("INFO")
    assert asyncio.run(bot._reply_with_retry(m, "t", 1)) is False
    assert m.calls == 3
    assert not any(r.levelname in ("ERROR", "CRITICAL") for r in caplog.records)
    assert any("send fail final attempts=3" in r.getMessage() for r in caplog.records)


def test_timed_out_not_retried_to_avoid_duplicate(caplog):
    m = _Msg(TimedOut(), None)
    caplog.set_level("INFO")
    assert asyncio.run(bot._reply_with_retry(m, "t", 1)) is False
    assert m.calls == 1


def test_bad_request_is_error_no_retry(caplog):
    m = _Msg(BadRequest("Message is too long"), None)
    caplog.set_level("INFO")
    assert asyncio.run(bot._reply_with_retry(m, "t", 1)) is False
    assert m.calls == 1
    assert any(r.levelname == "ERROR" for r in caplog.records)
