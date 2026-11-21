"""CLI журнала согласий Оператора (src/consent.py).

  python tools/consent.py [--db P] add --ts ISO --source telegram|chat|window --question-id ID --answer 1|0
                                       --code CODE [--task B30] (--question-text TEXT | --question-sha16 H)
                                       [--ts-approx] [--origin live|retro_summary]
  python tools/consent.py [--db P] add-file <entries.json>     (список записей; все проверяются до записи)
  python tools/consent.py [--db P] list [--task B30] [--code CODE]
  python tools/consent.py [--db P] verify [--require-task B30 --require-code CODE]

Вывод -- одна строка JSON (текст вопроса не печатается и не хранится -- только sha16). Код возврата: 0 -- ok / GO;
3 -- verify --require: ASK (нет записи [1], последний ответ [0], цепочка нарушена); 4 -- цепочка нарушена (verify
без --require); 2 -- ошибка аргументов или записи (ничего не записано)."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import consent as cs  # noqa: E402


def _out(obj) -> None:
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="consent")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("add")
    a1.add_argument("--ts", required=True)
    a1.add_argument("--source", required=True, choices=cs.SOURCES)
    a1.add_argument("--question-id", required=True)
    a1.add_argument("--answer", required=True, type=int, choices=(0, 1))
    a1.add_argument("--code", required=True)
    a1.add_argument("--task", default=None)
    g = a1.add_mutually_exclusive_group(required=True)
    g.add_argument("--question-text", default=None)
    g.add_argument("--question-sha16", default=None)
    a1.add_argument("--ts-approx", action="store_true")
    a1.add_argument("--origin", default="live", choices=cs.ORIGINS)
    af = sub.add_parser("add-file")
    af.add_argument("json")
    ls = sub.add_parser("list")
    ls.add_argument("--task", default=None)
    ls.add_argument("--code", default=None)
    vf = sub.add_parser("verify")
    vf.add_argument("--require-task", default=None)
    vf.add_argument("--require-code", default=None)
    a = ap.parse_args(argv)
    db = Path(a.db) if a.db else None
    try:
        if a.cmd == "add":
            e = {"ts": a.ts, "ts_exact": not a.ts_approx, "source": a.source, "question_id": a.question_id,
                 "answer": a.answer, "question_code": a.code, "task": a.task, "origin": a.origin}
            if a.question_text is not None:
                e["question_text"] = a.question_text
            else:
                e["question_sha16"] = a.question_sha16
            r = cs.add(e, db)
            _out({"cmd": "add", **r})
            return 0
        if a.cmd == "add-file":
            evs = json.loads(Path(a.json).read_text(encoding="utf-8-sig"))
            if not isinstance(evs, list):
                raise cs.ConsentError("список записей")
            r = cs.add_many(evs, db)
            _out({"cmd": "add-file", "records": r["records"], "new": r["new"]})
            return 0
    except (cs.ConsentError, ValueError) as e:
        _out({"cmd": a.cmd, "error": str(e)})
        return 2
    if a.cmd == "list":
        rows = cs.list_entries(db, a.task, a.code)
        _out({"cmd": "list", "rows": len(rows), "entries": [{k: r[k] for k in ("seq", "ts", "ts_exact", "source", "question_id",
                                                                             "answer", "question_code", "task", "origin")}
                                                            for r in rows]})
        return 0
    v = cs.verify(db)
    if a.require_task or a.require_code:
        if not (a.require_task and a.require_code):
            _out({"cmd": "verify", "error": "--require-task и --require-code -- вместе"})
            return 2
        q = cs.require(a.require_task, a.require_code, db)
        _out({"cmd": "verify", **v, "require": q})
        return 0 if q["decision"] == "GO" else 3
    _out({"cmd": "verify", **v})
    return 0 if v["ok"] else 4


if __name__ == "__main__":
    sys.exit(main())
