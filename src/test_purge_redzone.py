"""tools/purge_redzone_history.py (11.11): удаляются только пары «вход прогона -> ответ» внутри окон прогонов."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from memory_core import Envelope, MemoryCore

ROOT = Path(__file__).resolve().parent.parent


class _NoIndex:
    def upsert(self, *a, **k): pass
    def delete_subject(self, *a, **k): pass


def _mod():
    spec = importlib.util.spec_from_file_location("purge_rz", ROOT / "tools" / "purge_redzone_history.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _core(tmp_path):
    return MemoryCore(tmp_path / "m.db", _NoIndex(), lambda t: (t, []))


def _at(monkeypatch, ts):
    import memory_core
    monkeypatch.setattr(memory_core.time, "time", lambda: ts)


def test_only_case_inputs_inside_windows(tmp_path, monkeypatch):
    m = _mod()
    core = _core(tmp_path)
    env = Envelope("telegram", "1", "")
    in_win = m._ts("2026-11-11 08:06")
    out_win = m._ts("2026-11-11 09:30")
    _at(monkeypatch, in_win)
    core.record_exchange(env, "кто ты", "Око — система.")                  # вход прогона в окне -> удалить
    core.record_exchange(env, "Кто ты ", "Око.")                             # регистр/пробелы -> удалить
    core.record_exchange(env, "кто ты такой", "Око.")                       # другой текст -> оставить
    _at(monkeypatch, out_win)
    core.record_exchange(env, "кто ты", "Око.")                              # вне окна -> оставить
    db = tmp_path / "m.db"
    assert m.main(["--db", str(db)]) == 0                                     # dry run ничего не удаляет
    import sqlite3
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 8
    c.close()
    assert m.main(["--db", str(db), "--apply"]) == 0
    c = sqlite3.connect(db)
    left = [r[0] for r in c.execute("SELECT content FROM events WHERE role='user' ORDER BY event_id")]
    assert left == ["кто ты такой", "кто ты"]
    assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4
    c.close()


def test_missing_db_rc2(tmp_path):
    assert _mod().main(["--db", str(tmp_path / "nope.db")]) == 2


def test_windows_cover_logged_runs():
    m = _mod()
    w = m.windows()
    for t in ("2026-11-10 15:37", "2026-11-11 08:00", "2026-11-11 08:05", "2026-11-11 08:08", "2026-11-11 08:25"):
        assert any(a <= m._ts(t) <= b for a, b in w), t
