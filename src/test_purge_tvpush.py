"""tools/purge_tvpush_history.py (решение 12.11 21:47 п.2): 9 пар «Используй /tv» -- только они, с проверкой."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from memory_core import Envelope, MemoryCore

ROOT = Path(__file__).resolve().parent.parent
PUSH = "Не выполнялось. Используй /tv для управления телевизором, например: /tv цвет красный."
HONEST = "Не распознано. Сформулируй задачу иначе."


class _NoIndex:
    def upsert(self, *a, **k): pass
    def delete_subject(self, *a, **k): pass


def _mod():
    spec = importlib.util.spec_from_file_location("purge_tvpush", ROOT / "tools" / "purge_tvpush_history.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _db(tmp_path, answers):
    core = MemoryCore(tmp_path / "m.db", _NoIndex(), lambda t: (t, []))
    env = Envelope("telegram", "1", "")
    for i, a in enumerate(answers):
        core.record_exchange(env, f"vvod {i}", a)
    return tmp_path / "m.db"


def _ids(db):
    c = sqlite3.connect(db)
    try:
        return [r[0] for r in c.execute("SELECT event_id FROM events ORDER BY event_id")]
    finally:
        c.close()


def test_pairs_are_the_nine_approved():
    m = _mod()
    assert m.PAIRS == [(479, 480), (487, 488), (489, 490), (491, 492), (493, 494), (495, 496), (497, 498),
                       (499, 500), (501, 502)]


def test_marker():
    m = _mod()
    assert m.tv_push(PUSH) == "/tv"
    assert m.tv_push("Команды отправляются через /tv.") == "/tv"
    assert m.tv_push(HONEST) is None
    assert m.tv_push("путь C:/tvshow") is None


def test_apply_deletes_only_listed_push_pairs(tmp_path, monkeypatch):
    m = _mod()
    db = _db(tmp_path, [PUSH, HONEST, PUSH])            # пары 1/2, 3/4, 5/6
    monkeypatch.setattr(m, "PAIRS", [(1, 2), (5, 6)])
    audit = tmp_path / "a.jsonl"
    assert m.main(["--db", str(db), "--audit", str(audit), "--apply"]) == 0
    assert _ids(db) == [3, 4]
    assert audit.read_text(encoding="utf-8").count('"pair"') == 2


def test_refuses_honest_answer(tmp_path, monkeypatch):
    m = _mod()
    db = _db(tmp_path, [PUSH, HONEST])
    monkeypatch.setattr(m, "PAIRS", [(1, 2), (3, 4)])
    assert m.main(["--db", str(db), "--apply"]) == 4      # 3/4 -- честный ответ: отказ всего запуска
    assert _ids(db) == [1, 2, 3, 4]


def test_dry_run_changes_nothing_and_rerun_is_safe(tmp_path, monkeypatch):
    m = _mod()
    db = _db(tmp_path, [PUSH])
    monkeypatch.setattr(m, "PAIRS", [(1, 2)])
    assert m.main(["--db", str(db)]) == 0 and _ids(db) == [1, 2]
    assert m.main(["--db", str(db), "--apply"]) == 0 and _ids(db) == []
    assert m.main(["--db", str(db), "--apply"]) == 0      # повтор: пар уже нет -- пропуск
