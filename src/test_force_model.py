"""FORCE_SMALL_MODEL (02.11.2026, R7=F1): /force_brain в игре -- малая модель, короткий промпт,
сбой не открывает TransportGate, пользователь видит человеческий текст."""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import game_mode

SRC = Path(__file__).resolve().parent


def _ok(text):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"response": text})
    return resp


@pytest.fixture(autouse=True)
def _reset_note():
    game_mode._last_mode["mode"] = None
    yield
    game_mode._last_mode["mode"] = None


@pytest.fixture
def gov_client(tmp_path):
    import httpx
    from src.registry import Registry
    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    yield governor.AgentGovernor(http_client=client), client
    Registry._instance = None


@pytest.fixture
def forced(tmp_path, monkeypatch):
    import config
    flag, force = tmp_path / "game_mode.flag", tmp_path / "brain_force.flag"
    for k, v in (("OKO_GAME_MODE", True), ("OKO_GAME_FLAG", flag), ("OKO_GAME_FORCE_FLAG", force),
                 ("OKO_GAME_FLAG_STALE_S", 120.0), ("OKO_GAME_FORCE_NUM_GPU", None),
                 ("OKO_GAME_FORCE_MODEL", "small-model:test")):
        monkeypatch.setattr(config, k, v)
    flag.write_text("v2 since=1 game=x beat=1\n", encoding="ascii")
    os.utime(flag, None)
    game_mode.write_force(force, time.time() + 600)
    return config


def test_config_default_small_model():
    import config
    assert isinstance(config.OKO_GAME_FORCE_MODEL, str)


def test_force_uses_small_model(gov_client, forced):
    gov, client = gov_client
    client.post.side_effect = [_ok("ответ"), _ok("PASS")]
    assert asyncio.run(gov.execute_request("q")) == "ответ"
    for call in client.post.call_args_list:
        assert call.kwargs["json"]["model"] == "small-model:test"


def test_force_empty_model_falls_back_to_main(gov_client, forced, monkeypatch):
    monkeypatch.setattr(forced, "OKO_GAME_FORCE_MODEL", "")
    gov, client = gov_client
    client.post.side_effect = [_ok("ответ"), _ok("PASS")]
    asyncio.run(gov.execute_request("q"))
    assert client.post.call_args_list[0].kwargs["json"]["model"] == forced.OLLAMA_MODEL


def test_awake_uses_main_model(gov_client, forced):
    forced.OKO_GAME_FLAG.unlink()
    gov, client = gov_client
    client.post.side_effect = [_ok("ответ"), _ok("PASS")]
    asyncio.run(gov.execute_request("q"))
    assert client.post.call_args_list[0].kwargs["json"]["model"] == forced.OLLAMA_MODEL


def test_force_timeouts_do_not_open_transport_gate(gov_client, forced):
    import httpx
    import src.transport_gate as tg
    gov, client = gov_client
    client.post.side_effect = httpx.ReadTimeout("slow")
    n = tg.THRESHOLD + 1
    results = [asyncio.run(gov.execute_request(f"q{i}")) for i in range(n)]
    assert results == ["[ERR: LLM_TIMEOUT]"] * n
    assert client.post.call_count == n          # предохранитель не отсёк ни одного запроса
    assert gov.transport_gate_state == "CLOSED"


def test_governor_source_failures_through_wrapper():
    src = (SRC / "governor.py").read_text(encoding="utf-8")
    body = src[src.index("    async def _route_to_llm("):]
    body = body[:body.index("\n    async def ", 10)] if "\n    async def " in body[10:] else body
    assert len(re.findall(r"(?<!\w)gate\.record_failure\(", body)) == 0
    assert body.count("record_failure(") == 4


@pytest.fixture(scope="module")
def bot_src():
    return (SRC / "bot.py").read_text(encoding="utf-8-sig")


def _func(src: str, name: str) -> str:
    start = src.index(f"def {name}(")
    m = re.search(r"\n(async def |def )", src[start + 10:])
    return src[start: start + 10 + m.start()] if m else src[start:]


def test_bot_force_short_prompt_and_no_history(bot_src):
    body = _func(bot_src, "process_logic")
    i_forced = body.index("forced = game_mode.brain_mode(config) == game_mode.FORCE")
    i_hist = body.index("asyncio.to_thread(_fetch_history")
    assert i_forced < i_hist
    assert "history_list, knowledge = [], \"\"" in body
    i_fail = body.index("return FORCE_FAIL_TEXT")
    assert i_fail < body.index("persisted = None")
    save = body.index("asyncio.to_thread(_save_exchange, user_id, sanitized_input, raw_response)")
    assert body.rindex("if forced:", 0, save) > i_fail


def test_force_fail_text_human():
    src = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    m = re.search(r'FORCE_FAIL_MARKERS = \(([^)]*)\)', src)
    assert m and "LLM_TIMEOUT" in m.group(1) and "LLM_UNAVAILABLE" in m.group(1)
    assert "видеокарта занята игрой" in src
