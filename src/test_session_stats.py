"""session_stats (memory_core) — этап 1 рубежа, компонент B (без сети, tmp БД)."""
import pytest
from memory_core import MemoryCore, Envelope


class _NullIndex:
    def upsert(self, *a): pass
    def delete_subject(self, *a): pass
    def search(self, *a): return []


@pytest.fixture
def core(tmp_path):
    return MemoryCore(tmp_path / "m.db", _NullIndex(), lambda t: (t, []))


def _seed(core, sid, answer_ts):
    """Пишет ответы ассистента с заданными ts напрямую (в обход record)."""
    with core._tx() as c:
        for t in answer_ts:
            c.execute("INSERT INTO events (subject_id, channel, role, content, content_hash, ts) "
                      "VALUES (?,?,?,?,?,?)", (sid, "telegram", "assistant", "x", "h", t))


def test_empty_history_zero(core):
    sid = core.resolve("telegram", "1")
    assert core.session_stats(sid, 2400) == {"turns": 0, "active_s": 0.0, "span_s": 0.0}


def test_single_answer_turns1_no_span(core):
    sid = core.resolve("telegram", "1")
    _seed(core, sid, [1000.0])
    assert core.session_stats(sid, 2400) == {"turns": 1, "active_s": 0.0, "span_s": 0.0}


def test_contiguous_session_active_equals_span(core):
    sid = core.resolve("telegram", "1")
    _seed(core, sid, [0.0, 100.0, 250.0, 400.0])  # интервалы 100,150,150 < 600
    st = core.session_stats(sid, 600)
    assert st == {"turns": 4, "active_s": 400.0, "span_s": 400.0}


def test_long_pause_splits_takes_last_session(core):
    sid = core.resolve("telegram", "1")
    # старая сессия [0,100]; пауза 5000; текущая [5100,5200,5300]
    _seed(core, sid, [0.0, 100.0, 5100.0, 5200.0, 5300.0])
    st = core.session_stats(sid, 2400)
    assert st == {"turns": 3, "active_s": 200.0, "span_s": 200.0}


def test_since_ts_epoch_cuts_long_pauses_from_active(core):
    sid = core.resolve("telegram", "1")
    # эпоха с since_ts=0: ответы 0, 100, затем пауза 5000 (>2400), 5100
    _seed(core, sid, [0.0, 100.0, 5100.0])
    st = core.session_stats(sid, 2400, since_ts=0.0)
    assert st["turns"] == 3 and st["span_s"] == 5100.0
    assert st["active_s"] == 100.0  # пауза 5000 вырезана, учтён только интервал 100


def test_per_subject_isolation(core):
    a = core.resolve("telegram", "1")
    b = core.resolve("telegram", "2")
    _seed(core, a, [0.0, 100.0]); _seed(core, b, [0.0, 10.0, 20.0])
    assert core.session_stats(a, 2400)["turns"] == 2
    assert core.session_stats(b, 2400)["turns"] == 3


def test_user_messages_ignored(core):
    sid = core.resolve("telegram", "1")
    _seed(core, sid, [0.0, 100.0])
    with core._tx() as c:
        c.execute("INSERT INTO events (subject_id, channel, role, content, content_hash, ts) "
                  "VALUES (?,?,?,?,?,?)", (sid, "telegram", "user", "q", "h", 50.0))
    assert core.session_stats(sid, 2400)["turns"] == 2  # user-строка не считается
