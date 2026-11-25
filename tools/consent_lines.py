"""B33 H2.6: строки согласия, добавленные в блоке, сверяются с ЛОКАЛЬНЫМ журналом согласий (src/consent.py).

  python tools/consent_lines.py [--db P] --new F [--new F ...] [--old F ...]

--new -- списки разрешений снимка, который пушится (.github/test_weakening_allow.txt, .github/guarded_change_allow.txt);
--old -- те же файлы опубликованного снимка (нет файла -- пустой). Строка (формат src/weakening_guard.py), которой
нет в --old (или она другая), -- добавлена в этом блоке: её entry_sha16 обязан быть в журнале, запись -- с ответом 1,
цепочка журнала целая. Иначе STOP (push_*_stage не пушит). CI журнала не видит и проверяет только формат.
Вывод -- одна строка JSON (только ключи, sha16 и причины; текста вопросов нет). Код возврата: 0 -- GO; 3 -- STOP;
2 -- строки не разбираются. Только стандартная библиотека; ничего не пишет."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import consent as cs  # noqa: E402
import weakening_guard as wg  # noqa: E402


def _out(obj) -> None:
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8"))


def allow_lines(paths: list) -> tuple:
    allow, errs = {}, []
    for p in paths:
        f = Path(p)
        lines = f.read_text(encoding="utf-8-sig").splitlines() if f.is_file() else []
        a, e = wg.parse_allow(lines)
        errs += [f"{f.name}: {x}" for x in e]
        allow.update(a)
    return allow, errs


def check(new: list, old: list, db) -> tuple:
    na, ne = allow_lines(new)
    oa, _ = allow_lines(old)
    if ne:
        return 2, {"error": "consent lines do not parse", "errors": ne[:10]}
    added = {k: v for k, v in na.items() if oa.get(k) != v}
    v = cs.verify(db)
    found = cs.find_entries([x[3] for x in added.values()], db)
    bad = []
    for k, x in sorted(added.items()):
        r = found.get(x[3])
        if r is None:
            bad.append({"key": k, "entry_sha16": x[3], "why": "not_in_journal"})
        elif r["answer"] != 1:
            bad.append({"key": k, "entry_sha16": x[3], "why": "answer_0"})
    ok = v["ok"] and not bad
    return (0 if ok else 3), {"chain_ok": v["ok"], "rows": v["rows"], "added": len(added), "bad": bad,
                              "decision": "GO" if ok else "STOP"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="consent_lines")
    ap.add_argument("--db", default=None)
    ap.add_argument("--new", action="append", default=[], required=True)
    ap.add_argument("--old", action="append", default=[])
    a = ap.parse_args(argv)
    rc, obj = check(a.new, a.old, Path(a.db) if a.db else None)
    _out({"cmd": "check-lines", **obj})
    return rc


if __name__ == "__main__":
    sys.exit(main())
