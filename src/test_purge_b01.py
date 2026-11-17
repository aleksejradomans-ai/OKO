"""tools/purge_b01_history.py (12.11, решение 08:5x п.2): удаляются только 16 пар выдуманного статуса ТВ,
каждая пара проверяется (роли, subject, ts, ответ ловится router.fake_status); аудит пишется до удаления."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from memory_core import Envelope, MemoryCore

ROOT = Path(__file__).resolve().parent.parent
FAKE = "ТВ: подсветка музыка, цвет розовый"
HONEST = "Не распознано. Сформулируй задачу иначе."


class _NoIndex:
    def upsert(self, *a, **k): pass
    def delete_subject(self, *a, **k): pass


def _mod():
    spec = importlib.util.spec_from_file_location("purge_b01", ROOT / "tools" / "purge_b01_history.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _db(tmp_path, answers):
    """answers: список ответов assistant по порядку пар; event_id идут 1/2, 3/4, …"""
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


def test_pairs_list_is_the_decided_sixteen():
    m = _mod()
    assert len(m.PAIRS) == 16 and all(a == u + 1 for u, a in m.PAIRS)
    assert (477, 478) in m.PAIRS and (255, 256) in m.PAIRS


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    m = _mod()
    db = _db(tmp_path, [FAKE, HONEST, FAKE])
    monkeypatch.setattr(m, "PAIRS", [(1, 2), (5, 6)])
    monkeypatch.setattr(m.check, "__defaults__", ([(1, 2), (5, 6)], None))
    assert m.main(["--db", str(db)]) == 0
    assert _ids(db) == [1, 2, 3, 4, 5, 6]


def test_apply_deletes_only_listed_fake_pairs_and_writes_audit_first(tmp_path, monkeypatch):
    m = _mod()
    db = _db(tmp_path, [FAKE, HONEST, FAKE])
    monkeypatch.setattr(m.check, "__defaults__", ([(1, 2), (5, 6)], None))
    audit = tmp_path / "audit.jsonl"
    bak = tmp_path / "bak.db"
    assert m.main(["--db", str(db), "--apply", "--audit", str(audit), "--backup", str(bak)]) == 0
    assert _ids(db) == [3, 4]                                   # честная пара цела
    rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
    assert [r["pair"] for r in rows] == [[1, 2], [5, 6]]
    assert rows[0]["assistant"]["content"] == FAKE and rows[0]["user"]["role"] == "user"
    assert _ids(bak) == [1, 2, 3, 4, 5, 6]                      # копия до изменений


def test_honest_answer_in_list_refuses_whole_run(tmp_path, monkeypatch):
    m = _mod()
    db = _db(tmp_path, [FAKE, HONEST])
    monkeypatch.setattr(m.check, "__defaults__", ([(1, 2), (3, 4)], None))
    assert m.main(["--db", str(db), "--apply", "--audit", str(tmp_path / "a.jsonl")]) == 4
    assert _ids(db) == [1, 2, 3, 4] and not (tmp_path / "a.jsonl").exists()


def test_half_pair_or_wrong_order_refuses(tmp_path, monkeypatch):
    m = _mod()
    db = _db(tmp_path, [FAKE, FAKE])
    c = sqlite3.connect(db)
    with c:
        c.execute("DELETE FROM events WHERE event_id=3")       # от пары 3/4 осталась половина
    c.close()
    monkeypatch.setattr(m.check, "__defaults__", ([(1, 2), (3, 4)], None))
    assert m.main(["--db", str(db), "--apply"]) == 4
    monkeypatch.setattr(m.check, "__defaults__", ([(2, 1)], None))                 # роли наоборот
    assert m.main(["--db", str(db), "--apply"]) == 4
    assert _ids(db) == [1, 2, 4]


def test_rerun_after_apply_is_noop(tmp_path, monkeypatch):
    m = _mod()
    db = _db(tmp_path, [FAKE])
    monkeypatch.setattr(m.check, "__defaults__", ([(1, 2)], None))
    assert m.main(["--db", str(db), "--apply", "--audit", str(tmp_path / "a.jsonl")]) == 0
    assert m.main(["--db", str(db), "--apply", "--audit", str(tmp_path / "b.jsonl")]) == 0     # уже нет -- пропуск
    assert _ids(db) == [] and not (tmp_path / "b.jsonl").exists()


def test_index_queue_rows_go_with_events(tmp_path, monkeypatch):
    m = _mod()
    db = _db(tmp_path, [FAKE])
    c = sqlite3.connect(db)
    with c:
        c.execute("INSERT OR IGNORE INTO index_queue (event_id) VALUES (1), (2)")
    c.close()
    monkeypatch.setattr(m.check, "__defaults__", ([(1, 2)], None))
    assert m.main(["--db", str(db), "--apply", "--audit", str(tmp_path / "a.jsonl")]) == 0
    c = sqlite3.connect(db)
    try:
        assert c.execute("SELECT COUNT(*) FROM index_queue").fetchone()[0] == 0
    finally:
        c.close()
