"""SHELL_S1 (10.11): канал desktop для Oko Shell — те же обработчики, что у Telegram, только 127.0.0.1, ключ."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import shell_channel as sc

SRC = Path(__file__).resolve().parent
KEY = "k" * 32


def _handlers(log):
    async def text(update, context):
        log.append(("text", update.message.text, update.message.from_user.id, context.application))
        await update.message.reply_text("ответ: " + update.message.text)

    async def tv(update, context):
        log.append(("tv", list(context.args)))
        await update.message.reply_text("tv:" + " ".join(context.args))

    async def status(update, context):
        log.append(("status",))
        await update.message.reply_text("status ok")

    async def touch(update, context):
        log.append(("touch", update.effective_user.id))

    return {"text": text, "tv": tv, "status": status, "touch": touch}


# --- кто хозяин канала -------------------------------------------------------------
def test_operator_single_admin():
    assert sc.operator_id({42}) == 42


def test_operator_several_admins_needs_explicit():
    assert sc.operator_id({1, 2}) is None
    assert sc.operator_id({1, 2}, "2") == 2


def test_operator_explicit_must_be_admin():
    assert sc.operator_id({1}, "7") is None
    assert sc.operator_id({1}, "abc") is None
    assert sc.operator_id(set()) is None


# --- dispatch: тот же обработчик, что у Telegram ------------------------------------
def test_text_goes_to_telegram_message_handler():
    log, app = [], SimpleNamespace(bot_data={})
    out = asyncio.run(sc.dispatch(app, 42, "  подсветка синяя ", _handlers(log)))
    assert out == ["ответ: подсветка синяя"]
    assert ("touch", 42) in log and ("text", "подсветка синяя", 42, app) in log


def test_tv_command_with_args():
    log = []
    out = asyncio.run(sc.dispatch(SimpleNamespace(bot_data={}), 42, "/tv да", _handlers(log)))
    assert out == ["tv:да"] and ("tv", ["да"]) in log


def test_status_and_bot_suffix():
    log = []
    out = asyncio.run(sc.dispatch(SimpleNamespace(bot_data={}), 42, "/status@OkoBot", _handlers(log)))
    assert out == ["status ok"]


def test_unknown_command_not_sent_to_brain():
    log = []
    out = asyncio.run(sc.dispatch(SimpleNamespace(bot_data={}), 42, "/forget confirm", _handlers(log)))
    assert len(out) == 1 and "Telegram" in out[0]
    assert not any(x[0] == "text" for x in log)


def test_empty_text_nothing():
    log = []
    assert asyncio.run(sc.dispatch(SimpleNamespace(bot_data={}), 42, "   ", _handlers(log))) == []
    assert log == []


# --- сервер ------------------------------------------------------------------------
async def _ask(port, payload: bytes):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(payload)
    await w.drain()
    line = await r.readline()
    w.close()
    return json.loads(line.decode("utf-8"))


def _with_server(fn, max_text=4000):
    async def run():
        log = []
        ch = sc.ShellChannel(SimpleNamespace(bot_data={}), KEY, 42, _handlers(log), max_text=max_text)
        port = await ch.start(0)
        try:
            assert ch.server.sockets[0].getsockname()[0] == "127.0.0.1"
            return await fn(port, log)
        finally:
            await ch.stop()
    return asyncio.run(run())


def _req(key, text):
    return (json.dumps({"key": key, "text": text}, ensure_ascii=False) + "\n").encode("utf-8")


def test_server_good_key():
    async def fn(port, log):
        return await _ask(port, _req(KEY, "привет"))
    assert _with_server(fn) == {"ok": True, "replies": ["ответ: привет"]}


def test_server_bad_key_rejected_and_handler_not_called():
    async def fn(port, log):
        res = await _ask(port, _req("wrong", "выключи телевизор"))
        return res, log
    res, log = _with_server(fn)
    assert res == {"ok": False, "error": "auth"} and log == []


def test_server_too_long():
    async def fn(port, log):
        return await _ask(port, _req(KEY, "x" * 11))
    assert _with_server(fn, max_text=10)["error"] == "too_long"


def test_server_bad_json():
    async def fn(port, log):
        return await _ask(port, b"not json\n")
    assert _with_server(fn) == {"ok": False, "error": "bad_request"}


def test_disabled_without_key():
    cfg = SimpleNamespace(OKO_SHELL_KEY="", ADMIN_USER_IDS=frozenset({42}))
    assert asyncio.run(sc.start_from_config(SimpleNamespace(bot_data={}), cfg, {})) is None


def test_disabled_when_operator_unknown():
    cfg = SimpleNamespace(OKO_SHELL_KEY=KEY, ADMIN_USER_IDS=frozenset({1, 2}), OKO_SHELL_USER_ID="")
    assert asyncio.run(sc.start_from_config(SimpleNamespace(bot_data={}), cfg, {})) is None


# --- проводка: нет второй ветки --------------------------------------------------------
def test_module_has_no_own_brain_or_route():
    src = (SRC / "shell_channel.py").read_text(encoding="utf-8")
    for bad in ("import router", "process_logic", "governor", "execute_request", "import bot"):
        assert bad not in src


def test_bot_wires_same_handlers():
    text = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    init = text.split("async def post_init(", 1)[1].split("\nasync def ", 1)[0]
    assert "shell_channel.start_from_config(" in init
    for h in ('"text": handle_message', '"tv": handle_tv', '"status": handle_status', '"touch": _touch_activity'):
        assert h in init
    shut = text.split("async def post_shutdown(", 1)[1].split("\ndef ", 1)[0]
    assert "_shell" in shut and ".stop()" in shut


def test_config_has_shell_settings():
    import config
    assert hasattr(config, "OKO_SHELL_KEY") and isinstance(config.OKO_SHELL_PORT, int)
