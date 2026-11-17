"""STARTUP_BACKOFF (M5): нарастающие паузы ожидания Ollama при старте бота."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import bot


def test_default_delays_grow_and_are_capped():
    assert bot._startup_delays(6, 3.0) == [3.0, 6.0, 12.0, 24.0, 30.0]
    assert sum(bot._startup_delays(6, 3.0)) == 75.0


@pytest.mark.parametrize("n,expected", [(1, []), (0, []), (2, [3.0])])
def test_edge_counts(n, expected):
    assert bot._startup_delays(n, 3.0) == expected


def test_cap_is_respected():
    assert max(bot._startup_delays(10, 3.0)) == bot.STARTUP_BACKOFF_CAP_S


def _governor(results):
    g = MagicMock()
    it = iter(results)
    g.registry.check_model_loaded = MagicMock(side_effect=lambda timeout_s: next(it))
    return g


@pytest.fixture
def sleeps(monkeypatch):
    seen = []

    async def fake_sleep(s):
        seen.append(s)

    monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)
    return seen


def test_wait_uses_growing_pauses_until_ready(sleeps):
    g = _governor([(False, "down")] * 3 + [(True, "")])
    asyncio.run(bot.wait_for_model(g))
    assert sleeps == [3.0, 6.0, 12.0]
    assert g.registry.check_model_loaded.call_count == 4


def test_wait_gives_up_after_default_attempts(sleeps):
    g = _governor([(False, "down")] * 6)
    with pytest.raises(RuntimeError, match="after 6 attempts"):
        asyncio.run(bot.wait_for_model(g))
    assert sleeps == [3.0, 6.0, 12.0, 24.0, 30.0]


def test_ready_at_once_does_not_sleep(sleeps):
    g = _governor([(True, "")])
    asyncio.run(bot.wait_for_model(g))
    assert sleeps == []
