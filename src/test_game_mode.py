"""GAME_MODE v2 (02.11.2026): игровой режим без остановки OkoBot.

1. game_mode: режимы disabled/awake/stale/force/sleep, force-флаг, атомарная запись.
2. governor: в игре POST в Ollama нет, TransportGate не тронут, код жёлтый; force/keep_alive в payload.
3. bot.py (по исходнику -- импорт тянет Telegram): заглушка до истории/RAG, маршрутизатор без
   модели смысла, сон пропускается, /force_brain только для админа и зарегистрирован.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import game_mode

SRC = Path(__file__).resolve().parent


def _cfg(tmp_path, **kw):
    base = dict(OKO_GAME_MODE=True, OKO_GAME_FLAG=tmp_path / "game_mode.flag",
                OKO_GAME_FORCE_FLAG=tmp_path / "brain_force.flag", OKO_GAME_FLAG_STALE_S=120.0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _touch(p: Path, age_s: float = 0.0, text: str = "v2 since=1 game=x beat=1\n"):
    p.write_text(text, encoding="ascii")
    t = time.time() - age_s
    os.utime(p, (t, t))


@pytest.fixture(autouse=True)
def _reset_note():
    game_mode._last_mode["mode"] = None
    yield
    game_mode._last_mode["mode"] = None


# ------------------------------------------------------------------ 1. game_mode
class TestBrainMode:
    def test_disabled_switch(self, tmp_path):
        c = _cfg(tmp_path, OKO_GAME_MODE=False)
        _touch(c.OKO_GAME_FLAG)
        assert game_mode.brain_mode(c) == game_mode.DISABLED
        assert not game_mode.brain_asleep(c) and not game_mode.in_game(c)

    def test_missing_attrs_mean_disabled(self):
        assert game_mode.brain_mode(types.SimpleNamespace()) == game_mode.DISABLED

    def test_no_flag_awake(self, tmp_path):
        assert game_mode.brain_mode(_cfg(tmp_path)) == game_mode.AWAKE

    def test_fresh_flag_sleep(self, tmp_path):
        c = _cfg(tmp_path)
        _touch(c.OKO_GAME_FLAG, age_s=10)
        assert game_mode.brain_mode(c) == game_mode.SLEEP
        assert game_mode.brain_asleep(c) and game_mode.in_game(c)

    def test_stale_flag_fail_open(self, tmp_path):
        c = _cfg(tmp_path)
        _touch(c.OKO_GAME_FLAG, age_s=121)
        assert game_mode.brain_mode(c) == game_mode.STALE
        assert not game_mode.brain_asleep(c) and not game_mode.in_game(c)

    def test_force_in_game(self, tmp_path):
        c = _cfg(tmp_path)
        _touch(c.OKO_GAME_FLAG)
        game_mode.write_force(c.OKO_GAME_FORCE_FLAG, time.time() + 600)
        assert game_mode.brain_mode(c) == game_mode.FORCE
        assert not game_mode.brain_asleep(c) and game_mode.in_game(c)

    def test_expired_force_sleeps(self, tmp_path):
        c = _cfg(tmp_path)
        _touch(c.OKO_GAME_FLAG)
        game_mode.write_force(c.OKO_GAME_FORCE_FLAG, time.time() - 1)
        assert game_mode.brain_mode(c) == game_mode.SLEEP

    def test_force_without_game_is_awake(self, tmp_path):
        c = _cfg(tmp_path)
        game_mode.write_force(c.OKO_GAME_FORCE_FLAG, time.time() + 600)
        assert game_mode.brain_mode(c) == game_mode.AWAKE


class TestForceFlag:
    @pytest.mark.parametrize("text", ["", "garbage", "until=", "until=abc", "v2 until=-5",
                                      "xuntil=99999999999"])
    def test_broken_is_none(self, tmp_path, text):
        p = tmp_path / "f"
        p.write_text(text, encoding="ascii")
        assert game_mode.force_until(p, 0.0) is None

    def test_missing_is_none(self, tmp_path):
        assert game_mode.force_until(tmp_path / "nope", 0.0) is None

    def test_write_read_clear(self, tmp_path):
        p = tmp_path / "sub" / "brain_force.flag"
        until = time.time() + 300
        game_mode.write_force(p, until)
        assert p.read_text(encoding="ascii") == f"v2 until={int(until)}\n"
        assert not p.with_name(p.name + ".tmp").exists()
        assert game_mode.force_until(p, time.time()) == float(int(until))
        assert game_mode.clear_force(p) is True
        assert game_mode.clear_force(p) is False


class TestTransitionsLogged:
    def test_logged_once_per_change(self, tmp_path, caplog):
        import logging
        c = _cfg(tmp_path)
        _touch(c.OKO_GAME_FLAG)
        old = game_mode.logger.propagate
        game_mode.logger.propagate = True
        try:
            with caplog.at_level(logging.INFO, logger=game_mode.logger.name):
                for _ in range(5):
                    game_mode.brain_mode(c)
                _touch(c.OKO_GAME_FLAG, age_s=500)
                game_mode.brain_mode(c)
        finally:
            game_mode.logger.propagate = old
        lines = [r.getMessage() for r in caplog.records if "[GAME_MODE]" in r.getMessage()]
        assert len(lines) == 2
        assert "brain_mode=sleep" in lines[0]
        assert "stale_flag" in lines[1]


def test_sleep_text_points_to_force_command():
    assert "/force_brain" in game_mode.SLEEP_TEXT
    assert game_mode.SLEEP_TEXT.startswith("⚠️ Игровой режим активен")


# ------------------------------------------------------------------ config
def test_config_defaults():
    import config
    assert config.OKO_GAME_FLAG.name == "game_mode.flag"
    assert config.OKO_GAME_FORCE_FLAG.name == "brain_force.flag"
    assert config.OKO_GAME_FLAG.parent == config.OKO_GAME_FORCE_FLAG.parent
    assert config.OKO_GAME_FLAG_STALE_S > 0
    assert 1 <= config.OKO_GAME_FORCE_MIN <= config.OKO_GAME_FORCE_MAX_MIN
    assert isinstance(config.OKO_LLM_KEEP_ALIVE, str)


# ------------------------------------------------------------------ 2. governor
def _ok(text):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"response": text})
    return resp


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
def game_cfg(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "OKO_GAME_MODE", True)
    monkeypatch.setattr(config, "OKO_GAME_FLAG", tmp_path / "game_mode.flag")
    monkeypatch.setattr(config, "OKO_GAME_FORCE_FLAG", tmp_path / "brain_force.flag")
    monkeypatch.setattr(config, "OKO_GAME_FLAG_STALE_S", 120.0)
    monkeypatch.setattr(config, "OKO_LLM_KEEP_ALIVE", "60m")
    monkeypatch.setattr(config, "OKO_GAME_FORCE_NUM_GPU", None)
    return config


class TestGovernor:
    def test_game_sleep_no_post_gate_closed_yellow(self, gov_client, game_cfg):
        import governor
        gov, client = gov_client
        _touch(game_cfg.OKO_GAME_FLAG)
        assert issubclass(governor.GameModeSleep, governor.LLMTransportError)
        result = asyncio.run(gov.execute_request("q"))
        assert result == "[ERR: LLM_UNAVAILABLE]"
        assert client.post.call_count == 0
        assert gov.transport_gate_state == "CLOSED"

    def test_awake_payload_has_keep_alive(self, gov_client, game_cfg):
        gov, client = gov_client
        client.post.side_effect = [_ok("ответ"), _ok("PASS")]
        assert asyncio.run(gov.execute_request("q")) == "ответ"
        payload = client.post.call_args_list[0].kwargs["json"]
        assert payload["keep_alive"] == "60m"
        assert "num_gpu" not in (payload.get("options") or {})

    def test_empty_keep_alive_not_sent(self, gov_client, game_cfg, monkeypatch):
        monkeypatch.setattr(game_cfg, "OKO_LLM_KEEP_ALIVE", "")
        gov, client = gov_client
        client.post.side_effect = [_ok("ответ"), _ok("PASS")]
        asyncio.run(gov.execute_request("q"))
        assert "keep_alive" not in client.post.call_args_list[0].kwargs["json"]

    def test_force_cpu_sets_num_gpu(self, gov_client, game_cfg, monkeypatch):
        monkeypatch.setattr(game_cfg, "OKO_GAME_FORCE_NUM_GPU", 0)
        gov, client = gov_client
        _touch(game_cfg.OKO_GAME_FLAG)
        game_mode.write_force(game_cfg.OKO_GAME_FORCE_FLAG, time.time() + 600)
        client.post.side_effect = [_ok("ответ"), _ok("PASS")]
        assert asyncio.run(gov.execute_request("q")) == "ответ"
        for call in client.post.call_args_list:
            assert call.kwargs["json"]["options"]["num_gpu"] == 0

    def test_force_gpu_default_no_num_gpu(self, gov_client, game_cfg):
        gov, client = gov_client
        _touch(game_cfg.OKO_GAME_FLAG)
        game_mode.write_force(game_cfg.OKO_GAME_FORCE_FLAG, time.time() + 600)
        client.post.side_effect = [_ok("ответ"), _ok("PASS")]
        assert asyncio.run(gov.execute_request("q")) == "ответ"
        assert "num_gpu" not in (client.post.call_args_list[0].kwargs["json"].get("options") or {})

    def test_source_check_before_gate_and_vector(self):
        src = (SRC / "governor.py").read_text(encoding="utf-8")
        body = src[src.index("    async def _route_to_llm("):]
        assert body.index("raise GameModeSleep(") < body.index("gate.allow(")
        vec = src[src.index("    def _observe_vector("):src.index("    async def _embed_for_marker(")]
        assert "game_mode.in_game(config)" in vec


# ------------------------------------------------------------------ 3. bot.py (исходник)
@pytest.fixture(scope="module")
def bot_src():
    return (SRC / "bot.py").read_text(encoding="utf-8-sig")


def _func(src: str, name: str) -> str:
    start = src.index(f"def {name}(")
    m = re.search(r"\n(async def |def )", src[start + 10:])
    return src[start: start + 10 + m.start()] if m else src[start:]


@pytest.mark.parametrize("fn", ["process_logic", "process_discuss_logic"])
def test_guard_before_dispatch_history_rag(bot_src, fn):
    body = _func(bot_src, fn)
    g = body.index("game_mode.brain_asleep(config)")
    assert g < body.index("asyncio.to_thread(dispatcher.dispatch")
    for later in ("asyncio.to_thread(_fetch_history", "await retrieve_context(", "await governor.execute"):
        if later in body:
            assert g < body.index(later)
    assert "return game_mode.SLEEP_TEXT" in body


def test_router_without_intent_when_asleep(bot_src):
    body = _func(bot_src, "_route_reply")
    # 03.11 CTX_FOLLOW: пятым аргументом -- контекст прошлой команды ТВ; модель смысла (intent) в игре по-прежнему None
    assert "router.route, text, raw, None, _tv_ctx_get(user_id))" in body
    assert body.index("game_mode.brain_asleep(config)") < body.index("router.route, text, raw, None, ")


def test_sleep_job_skipped_in_game(bot_src):
    body = _func(bot_src, "sleep_cycle_job")
    assert body.index("game_mode.in_game(config)") < body.index("sleep_digest.run_once")


def test_force_brain_admin_only_and_registered(bot_src):
    body = _func(bot_src, "handle_force_brain")
    assert body.index("not in config.ADMIN_USER_IDS") < body.index("game_mode.write_force")
    assert 'CommandHandler("force_brain", handle_force_brain)' in bot_src
