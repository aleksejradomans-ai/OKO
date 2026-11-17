"""Чистка истории чата от обменов живых прогонов red-zone (решение 2026-11-10 23:15 п.2).

Удаляются ТОЛЬКО пары событий memory.db, у которых одновременно:
  - роль user и текст -- ровно один из входов tools/redzone_role.CASES (без учёта регистра и пробелов по краям);
  - время -- внутри окна известного прогона (WINDOWS, местное время Риги);
  - следом идёт ответ assistant того же субъекта с тем же ts (record_exchange пишет пару одной транзакцией).
Журналы прогонов exchange\\claude\\**\\redzone_*.txt не трогаются.

По умолчанию -- только просмотр (dry run). --apply удаляет одной транзакцией.
    python tools\\purge_redzone_history.py [--db PATH] [--apply]
Код возврата: 0 -- успех (в т.ч. 0 найдено), 2 -- база не найдена, 3 -- после удаления совпадения остались.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

TZ = _dt.timezone(_dt.timedelta(hours=2))   # Europe/Riga, ноябрь (EET, без летнего времени)

# Окна прогонов: 10.11 15:37; 11.11 08:00 (оборвался на C4), 08:05, 08:06, 08:25.
WINDOWS = [
    ("2026-11-10 15:30", "2026-11-10 15:50"),
    ("2026-11-11 07:58", "2026-11-11 08:12"),
    ("2026-11-11 08:20", "2026-11-11 08:30"),
]


def _ts(s: str) -> float:
    return _dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()


def windows() -> list[tuple[float, float]]:
    return [(_ts(a), _ts(b)) for a, b in WINDOWS]


def case_inputs() -> set[str]:
    import redzone_role
    return {q.strip().lower() for _, q, _, _ in redzone_role.CASES}


def find(c: sqlite3.Connection, inputs: set[str], wins: list[tuple[float, float]]) -> list[tuple]:
    """[(user_event_id, assistant_event_id, ts, user_text, answer_head)] -- пары под удаление."""
    out = []
    rows = c.execute("SELECT event_id, subject_id, content, ts FROM events WHERE role='user' ORDER BY event_id").fetchall()
    for eid, sid, content, ts in rows:
        if (content or "").strip().lower() not in inputs:
            continue
        if not any(a <= ts <= b for a, b in wins):
            continue
        nxt = c.execute("SELECT event_id, role, subject_id, ts, content FROM events WHERE event_id > ? "
                        "ORDER BY event_id LIMIT 1", (eid,)).fetchone()
        if not nxt or nxt[1] != "assistant" or nxt[2] != sid or abs(nxt[3] - ts) > 1.0:
            continue
        out.append((eid, nxt[0], ts, content, (nxt[4] or "")[:60].replace("\n", " / ")))
    return out


def default_db() -> Path:
    import config
    return Path(config.MEMORY_DB_PATH)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    db = a.db or default_db()
    print(f"db={db} exists={db.exists()}")
    if not db.exists():
        return 2
    c = sqlite3.connect(db, timeout=10.0)
    c.execute("PRAGMA foreign_keys=ON")
    try:
        total = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        pairs = find(c, case_inputs(), windows())
        print(f"events={total} pairs={len(pairs)} ({'APPLY' if a.apply else 'dry run'})")
        for u, s, ts, q, head in pairs:
            when = _dt.datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M:%S")
            print(f"  {when}  #{u}/#{s}  {q[:40]!r} -> {head!r}")
        if not a.apply or not pairs:
            return 0
        ids = [i for u, s, *_ in pairs for i in (u, s)]
        with c:
            c.executemany("DELETE FROM events WHERE event_id=?", [(i,) for i in ids])
        left = find(c, case_inputs(), windows())
        after = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print(f"deleted={total - after} events (expect {len(ids)}); left pairs={len(left)}; events now={after}")
        return 0 if not left and total - after == len(ids) else 3
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
