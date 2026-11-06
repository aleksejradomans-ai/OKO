"""Тесты RAG_DESYNC_FIX (knowledge_indexer) и rag_resync. Реальная ChromaDB и
Ollama не используются: коллекция и эмбеддинги подменены."""
import logging
import pathlib
import sqlite3
from types import SimpleNamespace

import pytest

import config
import db
import knowledge_indexer as ki
import rag_resync


class FakeCollection:
    def __init__(self):
        self.rows = {}
        self.fail_add = False

    def add(self, ids, embeddings, metadatas, documents):
        if self.fail_add:
            raise RuntimeError("chroma down")
        for i, m, d in zip(ids, metadatas, documents):
            self.rows[i] = (m, d)

    def get(self, where=None, limit=None, include=None):
        ids = [i for i, (m, _) in self.rows.items()
               if not where or all(m.get(k) == v for k, v in where.items())]
        if limit:
            ids = ids[:limit]
        return {"ids": ids, "metadatas": [self.rows[i][0] for i in ids]}

    def delete(self, where=None):
        for i in [i for i, (m, _) in self.rows.items()
                  if all(m.get(k) == v for k, v in where.items())]:
            del self.rows[i]


class ClosingProbe:
    """Обёртка соединения: считает close()."""
    def __init__(self, conn, registry):
        self._c = conn
        registry.append(self)
        self.closed = False

    def close(self):
        self.closed = True
        self._c.close()

    def __getattr__(self, name):
        return getattr(self._c, name)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "kb.db")
    monkeypatch.setattr(ki, "INPUT_DIR", tmp_path / "knowledge")
    monkeypatch.setattr(ki, "ARCHIVE_DIR", tmp_path / "knowledge" / "archived")
    (tmp_path / "knowledge").mkdir()
    coll = FakeCollection()
    monkeypatch.setattr(ki, "oko_rag_indexer", SimpleNamespace(get_collection=lambda: coll), raising=False)
    monkeypatch.setattr(ki, "RAG_SYNC_AVAILABLE", True)
    monkeypatch.setattr(ki, "CHUNK_SIZE", 10, raising=False)
    monkeypatch.setattr(ki, "CHUNK_OVERLAP", 0, raising=False)
    monkeypatch.setattr(ki, "chunk_text",
                        lambda t, s, o: [t[i:i + s] for i in range(0, len(t), s)] if t.strip() else [],
                        raising=False)
    calls = []
    state = {"ok": True, "fail_on": None}

    def fake_vec(text):
        calls.append(text)
        if not state["ok"] or state["fail_on"] == len(calls):
            return None
        return [0.1, 0.2]

    monkeypatch.setattr(ki, "_request_embedding_vector", fake_vec)
    ki.setup_database()
    return SimpleNamespace(coll=coll, calls=calls, state=state, tmp=tmp_path)


def _desync_records(caplog):
    return [r for r in caplog.records if "[RAG_DESYNC]" in r.getMessage()]


# --- index_chunks_to_rag: all-or-nothing ---

def test_success_writes_all_chunks(env):
    n = ki.index_chunks_to_rag("a" * 25, "f.txt", "h1", "cat")
    assert n == 3 and len(env.coll.rows) == 3


def test_failure_on_middle_chunk_writes_nothing(env, caplog):
    env.state["fail_on"] = 2
    with caplog.at_level(logging.INFO, logger="knowledge_indexer"):
        n = ki.index_chunks_to_rag("a" * 25, "f.txt", "h1", "cat")
    assert n == 0
    assert env.coll.rows == {}
    assert len(_desync_records(caplog)) == 1


def test_failure_stops_further_embedding_requests(env):
    env.state["ok"] = False
    ki.index_chunks_to_rag("a" * 95, "f.txt", "h1", "cat")
    assert len(env.calls) == 1


def test_chroma_write_error_is_marked_and_returns_zero(env, caplog):
    env.coll.fail_add = True
    with caplog.at_level(logging.INFO, logger="knowledge_indexer"):
        n = ki.index_chunks_to_rag("a" * 25, "f.txt", "h1", "cat")
    assert n == 0
    recs = _desync_records(caplog)
    assert len(recs) == 1 and recs[0].levelno == logging.ERROR


# --- process_single ---

def _file(env, name="doc.txt", text="b" * 25):
    p = env.tmp / "knowledge" / name
    p.write_text(text, encoding="utf-8")
    return p


def test_process_single_ollama_down_marks_desync_but_keeps_contract(env, caplog):
    env.state["ok"] = False
    p = _file(env)
    with db.get_connection() as conn, caplog.at_level(logging.INFO, logger="knowledge_indexer"):
        status = ki.process_single(p, conn)
    assert status == "processed"
    assert not p.exists()
    assert env.coll.rows == {}
    msgs = [r.getMessage() for r in _desync_records(caplog)]
    # одно сообщение из index_chunks_to_rag + одно из process_single
    assert len(msgs) == 2
    assert any("hash=" in m and "rag_resync" in m for m in msgs)
    c = sqlite3.connect(str(config.DB_PATH))
    assert c.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1
    c.close()


def test_process_single_ok_has_no_desync_marker(env, caplog):
    p = _file(env)
    with db.get_connection() as conn, caplog.at_level(logging.INFO, logger="knowledge_indexer"):
        ki.process_single(p, conn)
    assert _desync_records(caplog) == []
    assert len(env.coll.rows) == 3


def test_rag_unavailable_is_not_reported_as_desync(env, monkeypatch, caplog):
    monkeypatch.setattr(ki, "RAG_SYNC_AVAILABLE", False)
    p = _file(env)
    with db.get_connection() as conn, caplog.at_level(logging.INFO, logger="knowledge_indexer"):
        assert ki.process_single(p, conn) == "processed"
    assert _desync_records(caplog) == []


# --- закрытие соединений ---

def test_connections_are_closed(env, monkeypatch):
    opened = []
    real = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: ClosingProbe(real(*a, **k), opened))
    ki.setup_database()
    ki.log_pulse_anomaly_alert()
    _file(env)
    ki.index_all_files()
    assert len(opened) == 3
    assert all(p.closed for p in opened)


def test_no_bare_with_get_connection_left():
    src = pathlib.Path(ki.__file__).read_text(encoding="utf-8")
    assert "with db.get_connection() as conn" not in src
    assert "RAG_DESYNC_FIX" in src


# --- rag_resync ---

def _insert(text, name, h, category="general"):
    c = sqlite3.connect(str(config.DB_PATH))
    c.execute("INSERT INTO knowledge (filename, content, content_hash, source, category) VALUES (?,?,?,?,?)",
              (name, text, h, "file", category))
    c.commit()
    c.close()


def test_scan_finds_missing_and_partial(env):
    _insert("a" * 25, "m.txt", "hm")
    _insert("b" * 25, "p.txt", "hp")
    _insert("c" * 25, "ok.txt", "hok")
    ki.index_chunks_to_rag("c" * 25, "ok.txt", "hok", "general")
    env.coll.rows["hp_0"] = ({"content_hash": "hp", "source": "p.txt"}, "b" * 10)
    with db.get_connection() as conn:
        missing, partial, total = rag_resync.scan(conn, env.coll)
    assert total == 3
    assert [m["hash"] for m in missing] == ["hm"]
    assert [(p["hash"], p["actual"], p["expected"]) for p in partial] == [("hp", 1, 3)]


def test_dry_run_changes_nothing(env, capsys):
    _insert("a" * 25, "m.txt", "hm")
    assert rag_resync.main([]) == 0
    assert env.coll.rows == {}
    assert "dry-run" in capsys.readouterr().out


def test_apply_heals_missing_and_is_idempotent(env):
    _insert("a" * 25, "m.txt", "hm")
    assert rag_resync.main(["--apply"]) == 0
    assert len(env.coll.rows) == 3
    assert rag_resync.main(["--apply"]) == 0
    assert len(env.coll.rows) == 3


def test_apply_skips_partial_without_flag_and_rebuilds_with_flag(env):
    _insert("b" * 25, "p.txt", "hp")
    env.coll.rows["hp_0"] = ({"content_hash": "hp"}, "b" * 10)
    rag_resync.main(["--apply"])
    assert len(env.coll.rows) == 1
    rag_resync.main(["--apply", "--include-partial"])
    assert len(env.coll.rows) == 3


def test_apply_with_ollama_down_stops_and_returns_1(env):
    for i in range(5):
        _insert("a" * 25, f"m{i}.txt", f"h{i}")
    env.state["ok"] = False
    assert rag_resync.main(["--apply"]) == 1
    assert len(env.calls) == rag_resync.MAX_CONSECUTIVE_FAILURES
    assert env.coll.rows == {}
