"""CLI состояния карантина: snapshot / purge / weekly / zoom (см. docs/QUARANTINE_STATE.md).

  python tools/quarantine_state_cli.py snapshot <dir> [--db P] [--ttl-h 24]
  python tools/quarantine_state_cli.py purge <dir> [--db P] [--dry-run]
  python tools/quarantine_state_cli.py weekly [--db P] [--end-day YYYY-MM-DD] [--k 3]
  python tools/quarantine_state_cli.py zoom <weekly_uuid> [--db P]
  python tools/quarantine_state_cli.py cycle --root DIR [--glob knowledge_*] [--weekly-on Sun|none] [--dry-run] [--log P]
    (лог по умолчанию -- logs/quarantine_state.log в корне репозитория; строка на каталог + итог цикла)
  python tools/quarantine_state_cli.py record-run <file.json | -> [--db P]
    (объект или список объектов: task, block, started_at, ended_at, steps, rollbacks, uac, hitl, parse_errors,
     net_errors, flaky, signal -- только числа и коды; повтор той же записи -- та же строка)
Вывод -- одна строка JSON (без имён файлов и содержимого). Код возврата 0; ошибка аргументов -- 2;
record-run: неверная запись -- 2, ни одна запись этого вызова не пишется."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import quarantine_state as qs  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="quarantine_state_cli")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("dir")
    s.add_argument("--ttl-h", type=float, default=qs.DEFAULT_TTL_H)
    p = sub.add_parser("purge")
    p.add_argument("dir")
    p.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("weekly")
    w.add_argument("--end-day", default=None)
    w.add_argument("--k", type=float, default=qs.DEFAULT_K)
    z = sub.add_parser("zoom")
    z.add_argument("weekly_uuid")
    c = sub.add_parser("cycle")
    c.add_argument("--root", required=True)
    c.add_argument("--glob", default="knowledge_*")
    c.add_argument("--weekly-on", default="Sun")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--log", default=str(Path(__file__).resolve().parent.parent / "logs" / "quarantine_state.log"))
    rr = sub.add_parser("record-run")
    rr.add_argument("json")
    a = ap.parse_args(argv)
    db = Path(a.db) if a.db else None
    if a.cmd == "snapshot":
        r = qs.snapshot(Path(a.dir), db, a.ttl_h)
        r = {"cmd": "snapshot", "daily_uuid": r["daily_uuid"], "aggregate_sha256": r["aggregate_sha256"],
             "new": r["new"], "aggregate": r["aggregate"]}
    elif a.cmd == "purge":
        r = dict(qs.purge(Path(a.dir), db, a.dry_run), cmd="purge", dry_run=a.dry_run)
    elif a.cmd == "weekly":
        r = dict(qs.weekly(db, a.end_day, a.k), cmd="weekly")
    elif a.cmd == "zoom":
        r = dict(qs.zoom(a.weekly_uuid, db), cmd="zoom")
    elif a.cmd == "record-run":
        raw = sys.stdin.read() if a.json == "-" else Path(a.json).read_text(encoding="utf-8-sig")
        try:
            recs = json.loads(raw)
            recs = recs if isinstance(recs, list) else [recs]
            for x in recs:                                   # сначала проверка всех, потом запись
                if not isinstance(x, dict):
                    raise ValueError("запись -- объект JSON")
                qs.validate_run(x)
        except ValueError as e:
            sys.stdout.buffer.write((json.dumps({"cmd": "record-run", "error": str(e)}, ensure_ascii=False) + "\n").encode("utf-8"))
            return 2
        res = [qs.record_run(x, db) for x in recs]
        r = {"cmd": "record-run", "records": len(res), "new": sum(1 for x in res if x["new"]),
             "run_ids": [x["run_id"] for x in res]}
    else:
        wo = None if a.weekly_on.lower() == "none" else a.weekly_on
        c = qs.cycle(Path(a.root), a.glob, wo, db, a.dry_run, log=Path(a.log) if a.log else None)
        for d in c["dirs"]:
            sys.stdout.buffer.write((json.dumps(dict(d, cmd="cycle_dir"), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        r = dict(c["summary"], cmd="cycle")
    sys.stdout.buffer.write((json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
