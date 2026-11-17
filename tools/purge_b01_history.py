"""Чистка истории чата от выдуманных статусов ТВ (решение 2026-11-12 08:5x п.2).

Удаляются ТОЛЬКО пары из PAIRS (номера событий memory.db), и только если каждая пара цела:
  - первое событие -- role user, второе -- role assistant, тот же subject_id, |ts| разница <= 1 с
    (record_exchange пишет пару одной транзакцией);
  - ответ assistant ловится router.fake_status (защита ) -- удаляется только выдумка.
Пара, которой уже нет целиком, пропускается (повторный запуск безопасен). Пара, от которой осталась половина
или которая не прошла проверку, -- отказ всего запуска (код 4), ничего не удаляется.
Перед удалением пары пишутся в аудит (JSONL, все поля событий) -- вне контекста модели (бот этот файл не читает).
--backup PATH -- копия базы через sqlite3 backup API до любых изменений.

По умолчанию -- только просмотр (dry run). --apply удаляет одной транзакцией.
    python tools\\purge_b01_history.py [--db PATH] [--backup PATH] [--audit PATH] [--apply]
Код возврата: 0 -- успех, 2 -- база не найдена, 3 -- после удаления пары остались, 4 -- проверка пар не прошла.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

TZ = _dt.timezone(_dt.timedelta(hours=2))   # Europe/Riga, ноябрь (EET)

# 16 пар (прогон 11.11 22:3x + пропуск 478; решение 12.11 08:5x п.2).
PAIRS = [(255, 256), (275, 276), (289, 290), (325, 326), (327, 328), (447, 448), (449, 450), (451, 452),
         (453, 454), (455, 456), (459, 460), (463, 464), (465, 466), (467, 468), (475, 476), (477, 478)]
COLS = "event_id, subject_id, channel, role, content, content_hash, ts"


def _row(c: sqlite3.Connection, eid: int):
    r = c.execute(f"SELECT {COLS} FROM events WHERE event_id=?", (eid,)).fetchone()
    return dict(zip([x.strip() for x in COLS.split(",")], r)) if r else None


def check(c: sqlite3.Connection, pairs=PAIRS, fake=None) -> tuple[list, list, list]:
    """-> (к удалению [(u_row, a_row, marker)], уже нет [(u, a)], ошибки [str])."""
    if fake is None:
        import router
        fake = router.fake_status
    todo, gone, bad = [], [], []
    for u, a in pairs:
        ru, ra = _row(c, u), _row(c, a)
        if ru is None and ra is None:
            gone.append((u, a))
            continue
        if ru is None or ra is None:
            bad.append(f"#{u}/#{a}: осталась половина пары")
            continue
        if ru["role"] != "user" or ra["role"] != "assistant":
            bad.append(f"#{u}/#{a}: роли {ru['role']}/{ra['role']}")
            continue
        if ru["subject_id"] != ra["subject_id"] or abs(ra["ts"] - ru["ts"]) > 1.0:
            bad.append(f"#{u}/#{a}: не одна пара (subject/ts)")
            continue
        mark = fake(ra["content"])
        if not mark:
            bad.append(f"#{u}/#{a}: ответ не ловится fake_status -- не выдумка?")
            continue
        todo.append((ru, ra, mark))
    return todo, gone, bad


def write_audit(path: Path, todo) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for ru, ra, mark in todo:
            f.write(json.dumps({"pair": [ru["event_id"], ra["event_id"]], "marker": mark,
                                "user": ru, "assistant": ra}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def backup(db: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db, timeout=10.0)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
        return out.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        out.close()
        src.close()


def default_db() -> Path:
    import config
    return Path(config.MEMORY_DB_PATH)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--backup", type=Path, default=None)
    ap.add_argument("--audit", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    db = a.db or default_db()
    print(f"db={db} exists={db.exists()}")
    if not db.exists():
        return 2
    if a.backup:
        print(f"backup={a.backup} events={backup(db, a.backup)}")
    c = sqlite3.connect(db, timeout=10.0)
    c.execute("PRAGMA foreign_keys=ON")
    try:
        total = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        todo, gone, bad = check(c)
        print(f"events={total} pairs={len(todo)} gone={len(gone)} bad={len(bad)} ({'APPLY' if a.apply else 'dry run'})")
        for ru, ra, mark in todo:
            when = _dt.datetime.fromtimestamp(ra["ts"], TZ).strftime("%d.%m %H:%M:%S")
            print(f"  {when}  #{ru['event_id']}/#{ra['event_id']}  marker={mark[:40]!r}")
        for u, a_ in gone:
            print(f"  gone #{u}/#{a_}")
        for b in bad:
            print(f"  BAD {b}")
        if bad:
            return 4
        if not a.apply or not todo:
            return 0
        audit = a.audit or (db.parent / f"audit_b01_purge_{_dt.datetime.now(TZ):%Y%m%d_%H%M%S}.jsonl")
        write_audit(audit, todo)
        print(f"audit={audit} pairs={len(todo)}")
        ids = [r["event_id"] for ru, ra, _ in todo for r in (ru, ra)]
        with c:
            c.executemany("DELETE FROM events WHERE event_id=?", [(i,) for i in ids])
        left, _, bad2 = check(c)
        after = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print(f"deleted={total - after} events (expect {len(ids)}); left pairs={len(left)} bad={len(bad2)}; events now={after}")
        return 0 if not left and not bad2 and total - after == len(ids) else 3
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
