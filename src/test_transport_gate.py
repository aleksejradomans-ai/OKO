"""TRANSPORT_GATE (M5a) + TG_TRANSPORT (M5b).

1. TransportGate: CLOSED -> OPEN после THRESHOLD сбоев подряд, экспонента
   паузы с потолком, одна проба /api/version на всех, сброс счёта успехом.
2. governor: при OPEN в Ollama не ходим (жёлтый LLM_UNAVAILABLE сразу),
   ожидание GPU-очереди не считается сбоем узла, зависший запрос в полёте
   считается, 4xx/битый JSON узел не "роняют".
3. bot: пояснение "автоповтор через ~N с", /status, сетевые ошибки Telegram
   -> WARNING [TG_TRANSPORT]; BadRequest/InvalidToken -- не транспорт.
Фикстуры как в test_stagnation_guard.py: изолированный Registry на tmp_path,
логи ловятся на governor.logger; время и разброс -- ручные.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.transport_gate as tg

SRC_DIR = Path(__file__).resolve().parent


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(tg, "_now", c)
    monkeypatch.setattr(tg, "_jitter_factor", lambda j: 1.0)
    return c


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, r):
        self.records.append(r)

    def red(self):
        return [r for r in self.records if r.levelno >= logging.ERROR]

    def having(self, text):
        return [r for r in self.records if text in r.getMessage()]


@pytest.fixture
def lg():
    log = logging.getLogger("test.transport_gate")
    cap = _Cap()
    log.addHandler(cap)
    old = log.level
    log.setLevel(logging.DEBUG)
    yield log, cap
    log.removeHandler(cap)
    log.setLevel(old)


def run(c):
    return asyncio.run(c)


async def _yes():
    return True


async def _no():
    return False


# ------------------------------------------------------------------
# 1. TransportGate
# ------------------------------------------------------------------

def test_opens_after_threshold_consecutive_failures(clock, lg):
    log, cap = lg
    g = tg.TransportGate(logger=log)
    for _ in range(tg.THRESHOLD - 1):
        g.record_failure("x")
    assert g.state == tg.CLOSED and run(g.allow(_yes))
    g.record_failure("x")
    assert g.state == tg.OPEN
    assert g.retry_in() == pytest.approx(tg.BASE_S)
    assert len(cap.having("[TRANSPORT_GATE] -> OPEN")) == 1
    assert cap.red() == []


def test_success_resets_consecutive_count(clock):
    g = tg.TransportGate()
    for _ in range(tg.THRESHOLD - 1):
        g.record_failure("x")
    g.record_success()
    for _ in range(tg.THRESHOLD - 1):
        g.record_failure("x")
    assert g.state == tg.CLOSED


def test_open_refuses_without_probe_until_pause_elapses(clock):
    g = tg.TransportGate()
    for _ in range(tg.THRESHOLD):
        g.record_failure("x")
    probe = AsyncMock(return_value=True)
    clock.advance(tg.BASE_S - 0.1)
    assert run(g.allow(probe)) is False
    probe.assert_not_called()


def test_successful_probe_closes(clock, lg):
    log, cap = lg
    g = tg.TransportGate(logger=log)
    for _ in range(tg.THRESHOLD):
        g.record_failure("x")
    clock.advance(tg.BASE_S)
    assert g.state == tg.HALF_OPEN
    assert run(g.allow(_yes)) is True
    assert g.state == tg.CLOSED and g.retry_in() == 0.0
    assert cap.having("-> CLOSED") and cap.red() == []


def test_failed_probes_double_pause_up_to_cap(clock):
    g = tg.TransportGate()
    for _ in range(tg.THRESHOLD):
        g.record_failure("x")
    pauses = []
    for _ in range(10):
        clock.advance(g.retry_in())
        assert run(g.allow(_no)) is False
        pauses.append(g.retry_in())
    assert pauses[:3] == [tg.BASE_S * 2, tg.BASE_S * 4, tg.BASE_S * 8]
    assert max(pauses) == tg.MAX_S and pauses[-1] == tg.MAX_S


def test_probe_exception_counts_as_failure(clock, lg):
    log, cap = lg
    g = tg.TransportGate(logger=log)
    for _ in range(tg.THRESHOLD):
        g.record_failure("x")
    clock.advance(tg.BASE_S)

    async def boom():
        raise RuntimeError("probe broken")

    assert run(g.allow(boom)) is False
    assert g.state == tg.OPEN
    assert cap.red() == []


def test_single_probe_under_concurrency(clock):
    g = tg.TransportGate()
    for _ in range(tg.THRESHOLD):
        g.record_failure("x")
    clock.advance(tg.BASE_S)
    calls = {"n": 0}

    async def slow_probe():
        calls["n"] += 1
        await asyncio.sleep(0.02)
        return False

    async def many():
        return await asyncio.gather(*(g.allow(slow_probe) for _ in range(5)))

    assert run(many()) == [False] * 5
    assert calls["n"] == 1


def test_jitter_stays_within_bounds(monkeypatch):
    monkeypatch.setattr(tg, "_now", lambda: 0.0)
    for _ in range(50):
        g = tg.TransportGate()
        for _ in range(tg.THRESHOLD):
            g.record_failure("x")
        assert tg.BASE_S * (1 - tg.JITTER) <= g.retry_in() <= tg.BASE_S * (1 + tg.JITTER)


# ------------------------------------------------------------------
# 2. governor
# ------------------------------------------------------------------

def ok(text):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"response": text})
    return r


def http_status(code):
    import httpx
    r = MagicMock()
    r.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "err", request=MagicMock(), response=MagicMock(status_code=code)))
    return r


@pytest.fixture
def gc(tmp_path, clock):
    import httpx
    from src.registry import Registry
    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    client.get = AsyncMock(return_value=MagicMock(status_code=200))
    import governor
    gov = governor.AgentGovernor(http_client=client)
    cap = _Cap()
    governor.logger.addHandler(cap)
    old = governor.logger.level
    governor.logger.setLevel(logging.DEBUG)
    yield gov, client, cap
    governor.logger.removeHandler(cap)
    governor.logger.setLevel(old)
    Registry._instance = None


def _open_gate(gov, client):
    import httpx
    client.post.side_effect = httpx.ConnectError("Ollama down")
    for i in range(tg.THRESHOLD):
        assert run(gov.execute_request(f"q{i}")) == "[ERR: LLM_UNAVAILABLE]"
    assert gov.transport_gate_state == tg.OPEN


def test_open_gate_refuses_without_calling_ollama(gc):
    gov, client, cap = gc
    _open_gate(gov, client)
    before = client.post.call_count
    assert run(gov.execute_request("ещё")) == "[ERR: LLM_UNAVAILABLE]"
    assert client.post.call_count == before
    assert gov.transport_retry_in() == pytest.approx(tg.BASE_S)
    assert cap.red() == []
    assert "transport gate OPEN" in str(gov.registry.get_state("last_error"))


def test_recovery_after_pause_and_successful_probe(gc, clock):
    gov, client, cap = gc
    _open_gate(gov, client)
    clock.advance(tg.BASE_S)
    client.post.side_effect = [ok("ответ"), ok("PASS")]
    assert run(gov.execute_request("снова")) == "ответ"
    client.get.assert_awaited()
    assert "/api/version" in client.get.await_args.args[0]
    assert gov.transport_gate_state == tg.CLOSED


def test_failed_probe_keeps_refusing_with_longer_pause(gc, clock):
    gov, client, cap = gc
    _open_gate(gov, client)
    clock.advance(tg.BASE_S)
    client.get.return_value = MagicMock(status_code=503)
    before = client.post.call_count
    assert run(gov.execute_request("снова")) == "[ERR: LLM_UNAVAILABLE]"
    assert client.post.call_count == before
    assert gov.transport_retry_in() == pytest.approx(tg.BASE_S * 2)


def test_judge_is_not_called_when_gate_open(gc):
    gov, client, cap = gc
    _open_gate(gov, client)
    before = client.post.call_count
    run(gov.execute_request("вопрос"))
    run(gov.execute_discuss_request("вопрос", "документ"))
    assert client.post.call_count == before


def test_content_errors_do_not_open_gate(gc):
    gov, client, cap = gc
    from governor import LLMContentError
    client.post.side_effect = [http_status(400)] * 2 + [http_status(404)] * 3
    for _ in range(5):
        with pytest.raises(LLMContentError):
            run(gov._route_to_llm("p"))
    assert gov.transport_gate_state == tg.CLOSED


def test_5xx_counts_as_transport_failure(gc):
    gov, client, cap = gc
    client.post.side_effect = [http_status(503)] * tg.THRESHOLD
    for _ in range(tg.THRESHOLD):
        run(gov.execute_request("q"))
    assert gov.transport_gate_state == tg.OPEN


def test_gpu_queue_wait_timeout_is_not_a_node_failure(gc):
    gov, client, cap = gc
    client.post.side_effect = [ok("x")]

    async def scenario():
        async with gov._gpu_limiter:              # очередь занята другим запросом
            for _ in range(tg.THRESHOLD + 1):
                with pytest.raises(asyncio.TimeoutError):
                    await gov._call_llm_with_timeout("p", timeout=0.01)

    run(scenario())
    assert gov.transport_gate_state == tg.CLOSED
    client.post.assert_not_called()


def test_hung_request_in_flight_counts_as_failure(gc):
    gov, client, cap = gc

    async def hang(*a, **k):
        await asyncio.sleep(5)

    client.post.side_effect = hang

    async def scenario():
        for _ in range(tg.THRESHOLD):
            with pytest.raises(asyncio.TimeoutError):
                await gov._call_llm_with_timeout("p", timeout=0.01)

    run(scenario())
    assert gov.transport_gate_state == tg.OPEN


# ------------------------------------------------------------------
# 3. bot
# ------------------------------------------------------------------

@pytest.fixture
def bot_mod():
    return pytest.importorskip("bot")


def test_transport_note_shows_seconds(bot_mod):
    g = MagicMock()
    g.transport_retry_in = MagicMock(return_value=42.3)
    note = bot_mod._transport_note(g, "[ERR: LLM_UNAVAILABLE]")
    assert "~42 с" in note and "автоповтор" in note


@pytest.mark.parametrize("resp,retry", [
    ("[ERR: LLM_UNAVAILABLE]", 0.0),
    ("[ERR: LLM_TIMEOUT]", 42.0),
    ("обычный ответ", 42.0),
    (None, 42.0),
])
def test_transport_note_empty_otherwise(bot_mod, resp, retry):
    g = MagicMock()
    g.transport_retry_in = MagicMock(return_value=retry)
    assert bot_mod._transport_note(g, resp) == ""


def test_open_gate_replaces_generic_advice(bot_mod):
    """GATE_TEXT_FIX: при открытом предохранителе нет совета про короткий запрос."""
    g = MagicMock()
    g.transport_retry_in = MagicMock(return_value=11.0)
    out = bot_mod._explain_for_user(g, "[ERR: LLM_UNAVAILABLE]")
    assert out.startswith("[ERR: LLM_UNAVAILABLE]")
    assert "~11 с" in out and "Ollama" in out
    assert "короткий запрос" not in out and "через минуту" not in out


def test_closed_gate_keeps_standard_explanation(bot_mod):
    g = MagicMock()
    g.transport_retry_in = MagicMock(return_value=0.0)
    out = bot_mod._explain_for_user(g, "[ERR: LLM_UNAVAILABLE]")
    assert out == bot_mod._explain_error("[ERR: LLM_UNAVAILABLE]")
    assert "автоповтор" not in out


def test_transport_note_survives_governor_without_gate(bot_mod):
    assert bot_mod._transport_note(object(), "[ERR: LLM_UNAVAILABLE]") == ""


def test_tg_transport_classification(bot_mod):
    from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, TimedOut
    assert bot_mod._is_tg_transport_error(NetworkError("httpx.ConnectError: getaddrinfo failed"))
    assert bot_mod._is_tg_transport_error(TimedOut())
    assert not bot_mod._is_tg_transport_error(BadRequest("Query is too old"))
    assert not bot_mod._is_tg_transport_error(InvalidToken())
    assert not bot_mod._is_tg_transport_error(Forbidden("blocked"))
    assert not bot_mod._is_tg_transport_error(RuntimeError("bug"))


def _on_error(bot_mod, err):
    from telegram import Update
    update = MagicMock(spec=Update)
    update.effective_message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.error = err
    cap = _Cap()
    bot_mod.logger.addHandler(cap)
    old = bot_mod.logger.level
    bot_mod.logger.setLevel(logging.DEBUG)
    try:
        asyncio.run(bot_mod.on_error(update, ctx))
    finally:
        bot_mod.logger.removeHandler(cap)
        bot_mod.logger.setLevel(old)
    return update, cap


def test_on_error_network_is_yellow_and_silent(bot_mod):
    from telegram.error import NetworkError
    update, cap = _on_error(bot_mod, NetworkError("httpx.ConnectError: getaddrinfo failed"))
    assert cap.red() == []
    assert cap.having("[TG_TRANSPORT]")
    update.effective_message.reply_text.assert_not_called()


def test_on_error_bug_stays_red(bot_mod):
    update, cap = _on_error(bot_mod, RuntimeError("bug"))
    assert any("Unhandled exception in handler" in r.getMessage() for r in cap.red())


def test_on_error_bad_request_stays_red(bot_mod):
    from telegram.error import BadRequest
    update, cap = _on_error(bot_mod, BadRequest("Query is too old"))
    assert cap.red()


def test_status_has_transport_gate_block():
    src = (SRC_DIR / "bot.py").read_text(encoding="utf-8-sig")
    assert "TRANSPORT GATE" in src and "transport_gate_state" in src
