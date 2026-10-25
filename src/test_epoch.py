"""M7.3 /newepoch: граница истории по ts, журнал не трогается."""
import sqlite3
import time

import pytest

from src.memory_core import Envelope, MemoryCore


class _NullIndex:
    def upsert(self, event_id, subject_id, text): pass
    def delete_subject(self, subject_id): pass
    def search(self, subject_id, query, k): return []


def _core(tmp_path):
    return MemoryCore(tmp_path / "m.db", _NullIndex(), lambda t: (t, []))


def _put(core, ext, role, text, ts):
    """Событие с заданным ts (import_legacy не индексирует и не трогает часы)."""
    core.import_legacy("telegram", ext, [(role, text, ts)])


def _pair(core, ext, n, ts):
    _put(core, ext, "user", f"q{n}", ts)
    _put(core, ext, "assistant", f"a{n}", ts)


def test_no_epoch_history_unchanged(tmp_path):
    core = _core(tmp_path)
    for i in range(3):
        _pair(core, "1", i, 1000.0 + i)
    sid = core.resolve("telegram", "1")
    assert core.epoch_start(sid) is None
    assert core.history(sid, 10) == core.history(sid, 10, since_ts=None)
    assert len(core.history(sid, 10)) == 6


def test_history_since_ts_boundary_inclusive(tmp_path):
    core = _core(tmp_path)
    _pair(core, "1", 0, 1000.0)
    _pair(core, "1", 1, 2000.0)
    _pair(core, "1", 2, 3000.0)
    sid = core.resolve("telegram", "1")
    assert core.history(sid, 10, since_ts=2000.0) == [
        ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")]
    assert core.history(sid, 10, since_ts=3000.5) == []
    assert core.history(sid, 3, since_ts=1000.0) == [
        ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")]


def test_new_epoch_hides_old_keeps_journal(tmp_path):
    core = _core(tmp_path)
    env = Envelope("telegram", "1", "x")
    _pair(core, "1", 0, time.time() - 10.0)  # запас: грубые часы Windows (~15 мс)
    sid = core.resolve("telegram", "1")
    before = core.inventory(sid)["events"]
    ts = core.new_epoch(sid)  # те же часы time.time(), что у record_exchange
    assert core.epoch_start(sid) == ts
    assert core.history(sid, 10, since_ts=core.epoch_start(sid)) == []
    assert core.inventory(sid)["events"] == before
    core.record_exchange(env, "new q", "new a")
    assert core.history(sid, 10, since_ts=core.epoch_start(sid)) == [
        ("user", "new q"), ("assistant", "new a")]


def test_epoch_isolated_per_subject(tmp_path):
    core = _core(tmp_path)
    _pair(core, "1", 0, 1000.0)
    _pair(core, "2", 0, 1000.0)
    s1, s2 = core.resolve("telegram", "1"), core.resolve("telegram", "2")
    core.new_epoch(s1, ts=1500.0)
    assert core.epoch_start(s2) is None
    assert core.history(s1, 10, since_ts=core.epoch_start(s1)) == []
    assert len(core.history(s2, 10, since_ts=core.epoch_start(s2))) == 2


@pytest.mark.parametrize("raw", ["abc", "nan", "inf", "", "1e30"])
def test_bad_epoch_value_is_none(tmp_path, raw):
    core = _core(tmp_path)
    sid = core.resolve("telegram", "1")
    core.meta_set(MemoryCore.EPOCH_KEY_PREFIX + sid, raw)
    assert core.epoch_start(sid) is None


def test_session_stats_zero_after_fresh_epoch(tmp_path):
    """Регресс: раньше ts[0] входил в сессию без проверки since_ts -> turns=1."""
    core = _core(tmp_path)
    _pair(core, "1", 0, 1000.0)
    _pair(core, "1", 1, 1010.0)
    sid = core.resolve("telegram", "1")
    st = core.session_stats(sid, 2400, since_ts=1020.0)
    assert st == {"turns": 0, "active_s": 0.0, "span_s": 0.0}


def test_session_stats_pause_within_epoch(tmp_path):
    core = _core(tmp_path)
    for i, t in enumerate((1000.0, 1010.0, 9000.0, 9020.0)):
        _pair(core, "1", i, t)
    sid = core.resolve("telegram", "1")
    epoch_only = core.session_stats(sid, 2400, since_ts=1000.0)
    assert epoch_only["turns"] == 4
    both = core.session_stats(sid, 2400, since_ts=1000.0, pause_within_epoch=True)
    assert both == {"turns": 2, "active_s": 20.0, "span_s": 20.0}
    later_epoch = core.session_stats(sid, 2400, since_ts=9010.0, pause_within_epoch=True)
    assert later_epoch["turns"] == 1


def test_forget_removes_epoch_key(tmp_path):
    core = _core(tmp_path)
    _pair(core, "1", 0, 1000.0)
    sid = core.resolve("telegram", "1")
    core.new_epoch(sid, ts=1500.0)
    core.forget(sid)
    with sqlite3.connect(tmp_path / "m.db") as c:
        n = c.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'epoch_start:%'").fetchone()[0]
    assert n == 0
