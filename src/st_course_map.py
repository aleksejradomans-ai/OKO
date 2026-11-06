"""st_course_map.py — карта «программа -> допустимые опции» Samsung из сохранённого состояния (28.10.2026). Без сети.

Источник: samsungce.washerCycle.supportedCycles / samsungce.dryerCycle.supportedCycles — у каждой программы
поле supportedOptions: {опция: {"raw", "default", "options": [...]}}. Пустой options = опция в этой программе
не меняется (сверено 28.10: сушильная, программа 51 — dryingTime options=[], на дисплее время не появлялось).

Читает последний %LOCALAPPDATA%\\oko\\st_status_*.json (или --file), печатает карту и пишет
%LOCALAPPDATA%\\oko\\st_course_map.json — справочник для адаптера Ока.
Запуск:  python st_course_map.py [--file путь]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _base() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")) / "oko"


def build(status_by_kind: dict) -> dict:
    out = {}
    for kind, st in status_by_kind.items():
        main = (st.get("components") or {}).get("main") or {}
        cyc = (main.get(f"samsungce.{kind}Cycle") or {})
        table = ((cyc.get("referenceTable") or {}).get("value") or {}).get("id")
        current = ((cyc.get(f"{kind}Cycle") or {}).get("value"))
        courses = {}
        for c in (cyc.get("supportedCycles") or {}).get("value") or []:
            code = c.get("cycle")
            opts = {}
            for name, spec in sorted((c.get("supportedOptions") or {}).items()):
                spec = spec or {}
                values = spec.get("options") or []
                opts[name] = {"default": spec.get("default"), "values": values, "changeable": len(values) > 1}
            courses[code] = {"full": f"{table}_Course_{code}" if table else code,
                             "cycleType": c.get("cycleType"), "options": opts}
        out[kind] = {"referenceTable": table, "current": current, "courses": courses}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", type=Path)
    a = ap.parse_args()
    src = a.file
    if src is None:
        files = sorted(_base().glob("st_status_*.json"))
        if not files:
            raise SystemExit(f"СТОП: нет st_status_*.json в {_base()}")
        src = files[-1]
    data = json.loads(src.read_text(encoding="utf-8"))
    m = build(data)
    print(f"[источник] {src}")
    for kind, info in m.items():
        print(f"\n===== {kind}  таблица {info['referenceTable']}  текущая {info['current']}  программ {len(info['courses'])}")
        names = sorted({o for c in info["courses"].values() for o in c["options"]})
        for code, c in info["courses"].items():
            cells = []
            for n in names:
                o = c["options"].get(n)
                if o is None:
                    continue
                if o["changeable"]:
                    cells.append(f"{n}={','.join(map(str, o['values']))}(по умолч. {o['default']})")
                else:
                    cells.append(f"{n}=фикс:{o['default']}")
            print(f"  {code:4} {c['cycleType'] or '':12} " + "; ".join(cells))
    out = _base() / "st_course_map.json"
    out.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[сохранено] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
