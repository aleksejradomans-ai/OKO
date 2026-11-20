"""B26 P4: ядро памяти -- честная очередь индексации при заглушке, один resolve на обмен, WAL один раз.

1. Индекс-заглушка объявляет себя явным атрибутом is_null = True (не isinstance через модули): запись не вызывает
   drain_index, очередь index_queue копится, inventory.pending_index -- правда. Настоящий индекс -- дренаж как раньше;
   rebuild_index -- без изменений.
2. record_exchange принимает уже найденный subject_id (необязательный; без него -- resolve, как раньше);
   bot._save_exchange передаёт свой sid -- resolve на обмен один раз.
3. PRAGMA journal_mode=WAL -- один раз при инициализации БД; synchronous=FULL и foreign_keys=ON -- на каждом
   соединении (действуют на соединение).
"""
from __future__ import annotations

import sqlite3

import pytest

from src import memory_core as M
from src.memory_core import Envelope, MemoryCore


class _StubIndex:
    is_null = True

    def __init__(self):
        self.upserts = []

    def upsert(self, event_id, subject_id, text):
        self.upserts.append(event_id)

    def delete_subject(self, subject_id):
        pass

    def search(self, subject_id, query, k):
        return []


class _FakeIndex:
    def __init__(self, fail_ids=()):
        self.upserts, self.fail_ids = [], set(fail_ids)

    def upsert(self, event_id, subject_id, text):
        if event_id in self.fail_ids:
            raise RuntimeError("index down")
        self.upserts.append(event_id)

    def delete_subject(self, subject_id):
        pass

    def search(self, subject_id, query, k):
        return []


def _queue(core):
    with sqlite3.connect(core.db_path) as c:
        return [r[0] for r in c.execute("SELECT event_id FROM index_queue ORDER BY event_id")]


def _core(tmp_path, index):
    return MemoryCore(tmp_path / "m.db", index, lambda t: (t, []))


# --- 1. очередь при заглушке ---------------------------------------------------------------------------------------
def test_null_index_queue_grows_and_is_not_drained(tmp_path):
    idx = _StubIndex()
    core = _core(tmp_path, idx)
    env = Envelope("telegram", "1", "q")
    core.record_exchange(env, "q1", "a1")
    core.record_exchange(env, "q2", "a2")
    core.record(env, "user", "q3")
    assert _queue(core) == [1, 2, 3, 4, 5] and idx.upserts == []
    inv = core.inventory(core.resolve("telegram", "1"))
    assert (inv["events"], inv["pending_index"], inv["vectors"]) == (5, 5, 0)


def test_fake_real_index_drains_as_before(tmp_path):
    idx = _FakeIndex()
    core = _core(tmp_path, idx)
    env = Envelope("telegram", "1", "q")
    core.record_exchange(env, "q1", "a1")
    core.record(env, "user", "q2")
    assert _queue(core) == [] and idx.upserts == [1, 2, 3]
    inv = core.inventory(core.resolve("telegram", "1"))
    assert (inv["events"], inv["pending_index"], inv["vectors"]) == (3, 0, 3)


def test_real_index_failure_keeps_event_in_queue(tmp_path):
    core = _core(tmp_path, _FakeIndex(fail_ids={2}))
    core.record_exchange(Envelope("telegram", "1", "q"), "q1", "a1")
    assert _queue(core) == [2]
    with sqlite3.connect(core.db_path) as c:
        assert c.execute("SELECT attempts, last_error FROM index_queue").fetchone() == (1, "RuntimeError: index down")


@pytest.mark.parametrize("flag", [False, None, 1, "yes"])
def test_only_explicit_true_marks_null_index(tmp_path, flag):
    idx = _FakeIndex()
    idx.is_null = flag
    core = _core(tmp_path, idx)
    core.record_exchange(Envelope("telegram", "1", "q"), "q", "a")
    assert _queue(core) == [] and idx.upserts == [1, 2]


def test_rebuild_index_unchanged_drains_whole_journal(tmp_path):
    core = _core(tmp_path, _StubIndex())
    env = Envelope("telegram", "1", "q")
    core.record_exchange(env, "q1", "a1")
    real = _FakeIndex()
    core.index = real                              # индекс подключён -> пересборка из журнала
    core.rebuild_index()
    assert _queue(core) == [] and real.upserts == [1, 2]


def test_bot_null_index_is_marked():
    import bot
    assert bot._NullIndex.is_null is True and bot._NullIndex().search("s", "q", 3) == []


# --- 2. один resolve на обмен --------------------------------------------------------------------------------------
def _count_resolve(core, monkeypatch):
    calls = []
    real = core.resolve

    def counted(channel, external_id):
        calls.append((channel, external_id))
        return real(channel, external_id)
    monkeypatch.setattr(core, "resolve", counted)
    return calls


def test_record_exchange_with_subject_id_does_not_resolve(tmp_path, monkeypatch):
    core = _core(tmp_path, _StubIndex())
    sid = core.resolve("telegram", "1")
    calls = _count_resolve(core, monkeypatch)
    core.record_exchange(Envelope("telegram", "1", "q"), "q", "a", subject_id=sid)
    assert calls == [] and [c for _, c in core.history(sid)] == ["q", "a"]


def test_record_exchange_without_subject_id_resolves_as_before(tmp_path, monkeypatch):
    core = _core(tmp_path, _StubIndex())
    calls = _count_resolve(core, monkeypatch)
    core.record_exchange(Envelope("telegram", "7", "q"), "q", "a")
    assert calls == [("telegram", "7")]
    assert [c for _, c in core.history(core.resolve("telegram", "7"))] == ["q", "a"]


def test_save_exchange_resolves_once_per_exchange(tmp_path, monkeypatch):
    import bot
    core = _core(tmp_path, bot._NullIndex())
    monkeypatch.setattr(bot, "_get_memory", lambda: core)
    calls = _count_resolve(core, monkeypatch)
    assert bot._save_exchange(42, "вопрос", "ответ достаточно длинный, чтобы пройти порог проверки P1 петли") is True
    assert calls == [("telegram", "42")]
    sid = core.resolve("telegram", "42")
    assert [r for r, _ in core.history(sid)] == ["user", "assistant"] and len(_queue(core)) == 2


# --- 3. WAL один раз ---------------------------------------------------------------------------------------------------
def _trace(monkeypatch):
    stmts = []
    real = sqlite3.connect

    def connect(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(stmts.append)
        return c
    monkeypatch.setattr(M.sqlite3, "connect", connect)
    return stmts


def _pragmas(stmts, word):
    return [s for s in stmts if s.upper().startswith("PRAGMA") and word in s.lower()]


def test_wal_once_at_init_other_pragmas_per_connection(tmp_path, monkeypatch):
    stmts = _trace(monkeypatch)
    core = _core(tmp_path, _StubIndex())
    assert len(_pragmas(stmts, "journal_mode")) == 1
    stmts.clear()
    core.record_exchange(Envelope("telegram", "1", "q"), "q", "a")
    core.history(core.resolve("telegram", "1"))
    n_conn = len(_pragmas(stmts, "synchronous"))
    assert n_conn >= 3 and len(_pragmas(stmts, "foreign_keys")) == n_conn
    assert _pragmas(stmts, "journal_mode") == []


def test_new_connection_sees_wal(tmp_path):
    core = _core(tmp_path, _StubIndex())
    with sqlite3.connect(core.db_path) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c = core._conn()
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA synchronous").fetchone()[0] == 2          # FULL
    finally:
        c.close()


def test_foreign_keys_cascade_still_on_every_connection(tmp_path):
    core = _core(tmp_path, _StubIndex())
    sid = core.resolve("telegram", "1")
    core.record_exchange(Envelope("telegram", "1", "q"), "q", "a", subject_id=sid)
    assert core.forget(sid) == 2
    with sqlite3.connect(core.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM index_queue").fetchone()[0] == 0
