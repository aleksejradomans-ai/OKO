"""CALLBACK_EDGE_GUARD (M2) и M3_INGEST_GUARD (M3).

M2: повторный клик / устаревшее сообщение не должны давать необработанный
    BadRequest ("Message is not modified") -> [ERROR] + [ERR: INTERNAL];
    ответ на нажатие (answer) -- до правки сообщения.
M3: пустой / не-UTF-8 / нечитаемый / битый документ после approve не пишется
    в SQLite и ChromaDB, уходит в ARCHIVE_DIR/rejected, лог -- WARNING;
    ошибки индексации в боте не маскируются под "SECURITY" и "not found".
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

import bot
import config
import db
import knowledge_indexer

NOT_MODIFIED = (
    "Message is not modified: specified new message content and reply markup "
    "are exactly the same as a current content and reply markup of the message"
)


# ------------------------------------------------------------------
# Общие помощники
# ------------------------------------------------------------------

@pytest.fixture
def admin_ids(monkeypatch):
    ids = frozenset({111})
    monkeypatch.setattr(config, "ADMIN_USER_IDS", ids)
    return ids


class _LogCapture:
    """Ловит записи конкретного логгера независимо от propagate/handlers
    (logger.py проекта может отключать propagate -> caplog их не видит)."""

    def __init__(self, target: logging.Logger):
        self.target = target
        self.records: list[logging.LogRecord] = []

        class _H(logging.Handler):
            def emit(_self, record):
                self.records.append(record)

        self._h = _H(logging.DEBUG)

    def __enter__(self):
        self._old = self.target.level
        self.target.addHandler(self._h)
        self.target.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self.target.removeHandler(self._h)
        self.target.setLevel(self._old)

    def levels(self, min_level=logging.ERROR):
        return [r for r in self.records if r.levelno >= min_level]


def _callback(data, user_id=111, edit_exc=None, order=None):
    """Мок CallbackQuery. order -- список, куда пишется порядок вызовов."""
    query = MagicMock()
    query.from_user.id = user_id
    query.data = data

    async def _answer(*a, **k):
        if order is not None:
            order.append("answer")

    async def _edit(*a, **k):
        if order is not None:
            order.append("edit")
        if edit_exc is not None:
            raise edit_exc

    query.answer = AsyncMock(side_effect=_answer)
    query.edit_message_text = AsyncMock(side_effect=_edit)
    update = MagicMock()
    update.callback_query = query
    return update


def _context(staging_manager=None):
    ctx = MagicMock()
    ctx.application.bot_data = {
        "governor": MagicMock(),
        "staging_manager": staging_manager or MagicMock(),
    }
    return ctx


def _run(update, ctx):
    asyncio.run(bot.handle_staging_callback(update, ctx))


# ------------------------------------------------------------------
# M2: CALLBACK_EDGE_GUARD
# ------------------------------------------------------------------

class TestCategoryRepeatClick:
    def _sm(self):
        sm = MagicMock()
        sm.repo.get_task = MagicMock(return_value={"original_path": "/x/doc.txt"})
        return sm

    def test_not_modified_is_swallowed_without_error_log(self, admin_ids):
        update = _callback("cat_general_uuid-1", edit_exc=BadRequest(NOT_MODIFIED))
        with _LogCapture(bot.logger) as cap:
            _run(update, _context(self._sm()))  # не должно бросить
        assert cap.levels(logging.WARNING) == []
        update.callback_query.answer.assert_awaited_once()

    def test_answer_happens_before_edit(self, admin_ids):
        order = []
        update = _callback("cat_general_uuid-1", order=order)
        _run(update, _context(self._sm()))
        assert order == ["answer", "edit"]

    def test_category_still_saved(self, admin_ids):
        sm = self._sm()
        update = _callback("cat_system0_uuid-2", edit_exc=BadRequest(NOT_MODIFIED))
        _run(update, _context(sm))
        sm.repo.update_category.assert_called_once_with("uuid-2", bot.CATEGORY_LABELS["system0"])

    def test_other_bad_request_is_warning_not_raised(self, admin_ids):
        update = _callback("cat_general_uuid-1", edit_exc=BadRequest("Message to edit not found"))
        with _LogCapture(bot.logger) as cap:
            _run(update, _context(self._sm()))
        assert cap.levels(logging.ERROR) == []
        assert any(r.levelno == logging.WARNING for r in cap.records)

    def test_expired_answer_does_not_raise(self, admin_ids):
        update = _callback("cat_general_uuid-1")
        update.callback_query.answer = AsyncMock(side_effect=BadRequest("Query is too old"))
        _run(update, _context(self._sm()))
        update.callback_query.edit_message_text.assert_awaited_once()


class TestGameCallback:
    @pytest.fixture
    def games_file(self, tmp_path, monkeypatch):
        base = tmp_path / "data"
        base.mkdir()
        monkeypatch.setattr(config, "BASE_DIR", base)
        return tmp_path / "games.txt"

    def test_repeat_add_not_modified_swallowed(self, admin_ids, games_file):
        games_file.write_text("Game.exe\n", encoding="ascii")
        update = _callback("game_add_Game.exe", edit_exc=BadRequest(NOT_MODIFIED))
        with _LogCapture(bot.logger) as cap:
            _run(update, _context())
        assert cap.levels(logging.WARNING) == []
        update.callback_query.answer.assert_awaited_once()
        assert games_file.read_text(encoding="ascii") == "Game.exe\n"

    def test_answer_before_edit(self, admin_ids, games_file):
        order = []
        update = _callback("game_ignore_Game.exe", order=order)
        _run(update, _context())
        assert order == ["answer", "edit"]

    def test_add_writes_once(self, admin_ids, games_file):
        _run(_callback("game_add_New.exe"), _context())
        assert games_file.read_text(encoding="ascii") == "New.exe\n"


class TestApproveEdgeCases:
    def _sm(self):
        sm = MagicMock()
        sm.repo.get_task = MagicMock(return_value={"category": None})
        sm.approve = MagicMock(return_value=Path("/fake/staging/doc.txt"))
        return sm

    def test_approved_message_not_modified_swallowed(self, admin_ids, monkeypatch):
        bot._notified_uuids.clear()
        monkeypatch.setattr(bot, "_index_approved_file_in_thread", lambda p, c: "processed")
        update = _callback("approve_u1", edit_exc=BadRequest(NOT_MODIFIED))
        _run(update, _context(self._sm()))  # не должно бросить

    def test_unicode_error_in_indexing_is_not_security(self, admin_ids, monkeypatch):
        """UnicodeDecodeError -- подкласс ValueError: раньше попадал в ветку
        hash mismatch -> ложное "SECURITY" оператору."""
        bot._notified_uuids.clear()

        def boom(p, c):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        monkeypatch.setattr(bot, "_index_approved_file_in_thread", boom)
        update = _callback("approve_u2")
        with _LogCapture(bot.logger) as cap:
            _run(update, _context(self._sm()))
        msg = update.callback_query.edit_message_text.await_args.args[0]
        assert "SECURITY" not in msg
        assert "Indexing failed" in msg and "doc.txt" in msg
        assert any(r.levelno == logging.ERROR for r in cap.records)

    def test_key_error_in_indexing_is_not_task_not_found(self, admin_ids, monkeypatch):
        bot._notified_uuids.clear()

        def boom(p, c):
            raise KeyError("embedding")

        monkeypatch.setattr(bot, "_index_approved_file_in_thread", boom)
        update = _callback("approve_u3")
        _run(update, _context(self._sm()))
        msg = update.callback_query.edit_message_text.await_args.args[0]
        assert "not found" not in msg
        assert "Indexing failed" in msg

    @pytest.mark.parametrize("status", ["unreadable", "empty"])
    def test_rejected_status_reported_as_not_indexed(self, admin_ids, monkeypatch, status):
        bot._notified_uuids.clear()
        monkeypatch.setattr(bot, "_index_approved_file_in_thread", lambda p, c: status)
        update = _callback("approve_u4")
        with _LogCapture(bot.logger) as cap:
            _run(update, _context(self._sm()))
        msg = update.callback_query.edit_message_text.await_args.args[0]
        assert "Not indexed" in msg and status in msg
        assert "Approved and indexed" not in msg
        assert cap.levels(logging.ERROR) == []

    def test_hash_mismatch_still_security(self, admin_ids):
        bot._notified_uuids.clear()
        sm = self._sm()
        sm.approve = MagicMock(side_effect=ValueError("Hash mismatch for doc.txt"))
        update = _callback("approve_u5")
        _run(update, _context(sm))
        assert "SECURITY" in update.callback_query.edit_message_text.await_args.args[0]


# ------------------------------------------------------------------
# M3: M3_INGEST_GUARD (knowledge_indexer.process_single)
# ------------------------------------------------------------------

TOFU = "■"
GOOD_RU = "Проверка индексации: обычный читаемый текст документа для базы знаний."


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """Изолированная БД + ARCHIVE_DIR; эмбеддинги и ChromaDB отключены."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "kb.db")
    archive = tmp_path / "knowledge" / "archived"
    monkeypatch.setattr(knowledge_indexer, "ARCHIVE_DIR", archive)
    calls = {"embed": 0, "rag": 0}

    def _embed(text):
        calls["embed"] += 1
        return None

    def _rag(*a, **k):
        calls["rag"] += 1
        return 1

    monkeypatch.setattr(knowledge_indexer, "get_embedding", _embed)
    monkeypatch.setattr(knowledge_indexer, "index_chunks_to_rag", _rag)
    knowledge_indexer.setup_database()
    staging = tmp_path / "staging"
    staging.mkdir()
    conn = db.get_connection()
    yield {"conn": conn, "staging": staging, "archive": archive, "calls": calls}
    conn.close()


def _rows(conn):
    return conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]


def _rejected(archive):
    d = archive / "rejected"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _process(kb, path):
    with _LogCapture(knowledge_indexer.logger) as cap:
        status = knowledge_indexer.process_single(path, kb["conn"])
    return status, cap


class TestIngestGuard:
    def test_zero_byte_file_rejected_and_moved(self, kb):
        f = kb["staging"] / "zero.txt"
        f.write_bytes(b"")
        status, cap = _process(kb, f)
        assert status == "empty"
        assert not f.exists()
        assert len(_rejected(kb["archive"])) == 1
        assert _rows(kb["conn"]) == 0
        assert cap.levels(logging.ERROR) == []

    def test_whitespace_only_rejected_and_moved(self, kb):
        f = kb["staging"] / "blank.txt"
        f.write_text("  \n\t\n", encoding="utf-8")
        status, _ = _process(kb, f)
        assert status == "empty"
        assert not f.exists()
        assert _rows(kb["conn"]) == 0

    def test_non_utf8_text_is_unreadable_not_exception(self, kb):
        f = kb["staging"] / "cp1251.txt"
        f.write_bytes("Привет, это cp1251".encode("cp1251"))
        status, cap = _process(kb, f)
        assert status == "unreadable"
        assert not f.exists()
        assert _rows(kb["conn"]) == 0
        assert kb["calls"] == {"embed": 0, "rag": 0}
        assert cap.levels(logging.ERROR) == []
        assert any("[UNREADABLE_DOCUMENT]" in r.getMessage() for r in cap.records)

    def test_tofu_text_is_unreadable(self, kb):
        if knowledge_indexer.check_readable is None:
            pytest.skip("loader без RAG_READABILITY_GUARD")
        f = kb["staging"] / "tofu.txt"
        f.write_text(TOFU * 200 + " abc", encoding="utf-8")
        status, _ = _process(kb, f)
        assert status == "unreadable"
        assert _rows(kb["conn"]) == 0
        assert kb["calls"]["rag"] == 0

    def test_pdf_unreadable_document_error_is_unreadable(self, kb, monkeypatch):
        if not knowledge_indexer.RAG_SYNC_AVAILABLE:
            pytest.skip("oko_rag недоступен")

        def _raise(path):
            raise knowledge_indexer.UnreadableDocumentError("90% символов-заменителей")

        monkeypatch.setattr(knowledge_indexer, "load_document", _raise)
        f = kb["staging"] / "tofu.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        status, _ = _process(kb, f)
        assert status == "unreadable"
        assert not f.exists()
        assert _rows(kb["conn"]) == 0

    def test_corrupt_pdf_is_unreadable(self, kb):
        if not knowledge_indexer.RAG_SYNC_AVAILABLE:
            pytest.skip("oko_rag недоступен")
        f = kb["staging"] / "broken.pdf"
        f.write_bytes(b"this is not a pdf at all " * 10)
        status, _ = _process(kb, f)
        assert status == "unreadable"
        assert not f.exists()
        assert _rows(kb["conn"]) == 0

    def test_readable_text_still_processed_and_archived(self, kb):
        f = kb["staging"] / "good.txt"
        f.write_text(GOOD_RU, encoding="utf-8")
        status, _ = _process(kb, f)
        assert status == "processed"
        assert _rows(kb["conn"]) == 1
        assert kb["calls"]["rag"] == 1
        assert _rejected(kb["archive"]) == []
        assert not f.exists()

    def test_rejected_dir_not_scanned_by_batch(self, kb, monkeypatch):
        """rejected лежит внутри ARCHIVE_DIR -> не попадает в index_all_files."""
        f = kb["staging"] / "zero.txt"
        f.write_bytes(b"")
        _process(kb, f)
        input_dir = kb["archive"].parent
        monkeypatch.setattr(knowledge_indexer, "INPUT_DIR", input_dir)
        seen = []
        monkeypatch.setattr(knowledge_indexer, "process_single", lambda p, c: seen.append(p))
        knowledge_indexer.index_all_files()
        assert seen == []
