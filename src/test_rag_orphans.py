"""RAG_ORPHAN_SCAN (rag_resync): чанки ChromaDB без строки SQLite,
--prune-orphans, --drop-id. Реальная ChromaDB и Ollama не используются."""
import sqlite3
from types import SimpleNamespace

import pytest

import config
import knowledge_indexer as ki
import rag_resync
from test_rag_desync import FakeCollection


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "kb.db")
    coll = FakeCollection()
    monkeypatch.setattr(
        ki, "oko_rag_indexer",
        SimpleNamespace(get_collection=lambda: coll, COLLECTION_NAME="test_coll"),
        raising=False,
    )
    monkeypatch.setattr(ki, "RAG_SYNC_AVAILABLE", True)
    monkeypatch.setattr(ki, "CHUNK_SIZE", 10, raising=False)
    monkeypatch.setattr(ki, "CHUNK_OVERLAP", 0, raising=False)
    monkeypatch.setattr(ki, "chunk_text",
                        lambda t, s, o: [t[i:i + s] for i in range(0, len(t), s)] if t.strip() else [],
                        raising=False)
    calls = []
    monkeypatch.setattr(ki, "_request_embedding_vector", lambda t: calls.append(t) or [0.1])
    ki.setup_database()
    return SimpleNamespace(coll=coll, calls=calls)


def _insert(text, name, h, source="file", category="general"):
    c = sqlite3.connect(str(config.DB_PATH))
    cur = c.execute(
        "INSERT INTO knowledge (filename, content, content_hash, source, category) VALUES (?,?,?,?,?)",
        (name, text, h, source, category))
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


def _chunk(coll, h, i, source="old.txt", category="general"):
    meta = {"content_hash": h, "source": source, "chunk": i}
    if category:
        meta["category"] = category
    coll.rows[f"{h}_{i}"] = (meta, "x")


def _rows():
    c = sqlite3.connect(str(config.DB_PATH))
    n = c.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    c.close()
    return n


def _scan(env):
    import db
    with db.get_connection() as conn:
        return rag_resync.scan_orphans(conn, env.coll)


def test_orphans_detected_with_sources_and_missing_category(env):
    _insert("c" * 10, "ok.txt", "hok")
    _chunk(env.coll, "hok", 0, "ok.txt")
    _chunk(env.coll, "horph", 0, "gone.txt", category=None)
    _chunk(env.coll, "horph", 1, "gone.txt")
    orphans, no_hash = _scan(env)
    assert [(o["hash"], o["chunks"], o["no_category"]) for o in orphans] == [("horph", 2, 1)]
    assert orphans[0]["sources"] == {"gone.txt"}
    assert no_hash == 0


def test_hash_of_non_file_row_is_not_orphan(env):
    _insert("alert", None, "htrig", source="trigger")
    _chunk(env.coll, "htrig", 0)
    orphans, _ = _scan(env)
    assert orphans == []


def test_chunks_without_hash_are_counted_not_orphaned(env):
    env.coll.rows["manual_0"] = ({"source": "m.txt", "full_path": "/x/m.txt"}, "x")
    orphans, no_hash = _scan(env)
    assert orphans == [] and no_hash == 1


def test_dry_run_reports_and_changes_nothing(env, capsys):
    _chunk(env.coll, "horph", 0)
    assert rag_resync.main([]) == 0
    out = capsys.readouterr().out
    assert "Коллекция ChromaDB: test_coll" in out
    assert "ORPHAN" in out and "horph" in out
    assert len(env.coll.rows) == 1


def test_prune_requires_apply(env):
    _chunk(env.coll, "horph", 0)
    assert rag_resync.main(["--prune-orphans"]) == 3
    assert rag_resync.main(["--drop-id", "1"]) == 3
    assert len(env.coll.rows) == 1


def test_apply_without_prune_flag_keeps_orphans(env, capsys):
    _chunk(env.coll, "horph", 0)
    assert rag_resync.main(["--apply"]) == 0
    assert len(env.coll.rows) == 1
    assert "ORPHAN пропущены" in capsys.readouterr().out


def test_prune_removes_only_orphans_and_is_idempotent(env):
    _insert("c" * 10, "ok.txt", "hok")
    _chunk(env.coll, "hok", 0, "ok.txt")
    _chunk(env.coll, "horph", 0)
    _chunk(env.coll, "horph", 1)
    assert rag_resync.main(["--apply", "--prune-orphans"]) == 0
    assert set(env.coll.rows) == {"hok_0"}
    assert rag_resync.main(["--apply", "--prune-orphans"]) == 0
    assert set(env.coll.rows) == {"hok_0"}
    assert env.calls == []  # здоровая запись не переиндексировалась


def test_drop_id_removes_row_and_chunks_without_reindex(env):
    rid = _insert("a" * 10, "test.txt", "htest")
    keep = _insert("b" * 10, "keep.txt", "hkeep")
    _chunk(env.coll, "htest", 0, "test.txt")
    _chunk(env.coll, "hkeep", 0, "keep.txt")
    assert rag_resync.main(["--apply", "--drop-id", str(rid)]) == 0
    assert _rows() == 1
    assert set(env.coll.rows) == {"hkeep_0"}
    assert env.calls == []
    assert keep


def test_drop_id_of_missing_record_is_not_reindexed(env):
    """Запись без чанков (MISSING) + --drop-id: не должна быть доиндексирована
    в том же прогоне перед удалением."""
    rid = _insert("a" * 25, "test.txt", "htest")
    assert rag_resync.main(["--apply", "--drop-id", str(rid)]) == 0
    assert _rows() == 0
    assert env.coll.rows == {}
    assert env.calls == []


def test_drop_id_refuses_non_file_and_unknown(env):
    rid = _insert("alert", None, "htrig", source="trigger")
    assert rag_resync.main(["--apply", "--drop-id", str(rid)]) == 1
    assert rag_resync.main(["--apply", "--drop-id", "999"]) == 1
    assert _rows() == 1
