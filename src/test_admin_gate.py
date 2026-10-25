"""ADMIN_GATE (25.10): /memory и /forget закрыты для не-админов (fail-closed, молча),
посторонний не получает ответа и не создаёт subject в memory.db."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import config


def _update(uid):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                           message=SimpleNamespace(reply_text=AsyncMock()))


@pytest.fixture
def core(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({111}))
    c = MagicMock()
    c.resolve.return_value = "sid"
    c.inventory.return_value = {"aliases": [("telegram", "111")], "events": 4, "first_ts": None,
                                "last_ts": None, "vectors": 4, "pending_index": 0, "derived_labels": 0}
    c.forget.return_value = 4
    monkeypatch.setattr(bot, "_get_memory", lambda: c)
    return c


@pytest.mark.parametrize("handler,args", [("handle_memory", []), ("handle_forget", []),
                                          ("handle_forget", ["confirm"])])
def test_non_admin_ignored_no_memory_touch(core, caplog, handler, args):
    up = _update(999)
    caplog.set_level("WARNING")
    asyncio.run(getattr(bot, handler)(up, SimpleNamespace(args=args)))
    up.message.reply_text.assert_not_called()
    core.resolve.assert_not_called()
    core.forget.assert_not_called()
    assert any("Unauthorized" in r.getMessage() and "999" in r.getMessage() for r in caplog.records)


def test_admin_memory_and_forget_preview_still_work(core):
    up = _update(111)
    asyncio.run(bot.handle_memory(up, SimpleNamespace(args=[])))
    assert "записей: 4" in up.message.reply_text.call_args[0][0]
    up2 = _update(111)
    asyncio.run(bot.handle_forget(up2, SimpleNamespace(args=[])))
    assert "/forget confirm" in up2.message.reply_text.call_args[0][0]
    core.forget.assert_not_called()  # без confirm ничего не удаляется
