"""st_labels_probe.py — ищет в облаке SmartThings ЧИТАЕМЫЕ названия программ Samsung (только чтение, 28.10.2026).

Оператор: у программ есть описания (на машине — «Rinse+Spin» и т.п.), коды не нужны, нужен перевод на русский.
Где облако может их хранить (проверяется, не предполагается):
  1. описание интерфейса устройства: GET /devices/{id} -> presentationId + manufacturerName -> GET /presentation?...
  2. то же с Accept-Language: ru (вдруг подписи локализованы);
  3. сами элементы supportedCycles (иногда там есть name/label).
Скрипт ищет в ответах строки рядом с кодами программ (1C, Table_02_Course_1C) и печатает найденные пары.
Сырые ответы — %LOCALAPPDATA%\\oko\\st_labels_<kind>_<источник>.json, для разбора.

Запуск (OKO_SMARTTHINGS_TOKEN в окружении):  python st_labels_probe.py [--only washer|dryer]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
from pathlib import Path

from st_mode_walk import KINDS, Api, _read, _val


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def _pairs(obj, codes: set[str]) -> dict[str, set[str]]:
    """Для каждого кода — строки-соседи в том же объекте (label/name/value/displayName...)."""
    found: dict[str, set[str]] = {}

    def visit(o):
        if isinstance(o, dict):
            strs = {k: v for k, v in o.items() if isinstance(v, str)}
            hit = [c for c in codes for v in strs.values() if v == c or v.endswith("_" + c)]
            for c in hit:
                for k, v in strs.items():
                    if v != c and not v.endswith("_" + c) and len(v) < 80 and not v.startswith("Table_"):
                        found.setdefault(c, set()).add(f"{k}={v}")
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)
    visit(obj)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=("washer", "dryer"))
    a = ap.parse_args()
    token = os.environ.get("OKO_SMARTTHINGS_TOKEN", "").strip()
    if not token:
        raise SystemExit("СТОП: нет OKO_SMARTTHINGS_TOKEN")
    api = Api(token)
    out_dir = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")) / "oko"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        devs = [d for d in api.get("/devices").get("items") or []
                if KINDS.get((d.get("ocf") or {}).get("ocfDeviceType"))
                and (not a.only or KINDS[d["ocf"]["ocfDeviceType"]] == a.only)]
    except urllib.error.HTTPError as e:
        print(f"[ЛОГИКА] HTTP {e.code}")
        return 3
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[ТРАНСПОРТ] {type(e).__name__}: {e}")
        return 2

    any_found = False
    for d in devs:
        kind = KINDS[d["ocf"]["ocfDeviceType"]]
        m = _read(api, d["deviceId"])
        cyc = _val(m, f"samsungce.{kind}Cycle", "supportedCycles") or []
        codes = {c["cycle"] for c in cyc if isinstance(c, dict) and c.get("cycle")}
        print(f"\n===== {d.get('label')} ({kind}): программ {len(codes)}")
        sources = {"supportedCycles": cyc}
        info = api.get(f"/devices/{d['deviceId']}")
        pid, mnmn = info.get("presentationId"), info.get("manufacturerName")
        print(f"  presentationId={pid} manufacturerName={mnmn}")
        if pid and mnmn:
            q = urllib.parse.urlencode({"presentationId": pid, "manufacturerName": mnmn})
            for lang in ("en", "ru"):
                api.h["Accept-Language"] = lang
                try:
                    sources[f"presentation_{lang}"] = api.get(f"/presentation?{q}")
                except urllib.error.HTTPError as e:
                    print(f"  presentation[{lang}]: HTTP {e.code}")
            api.h.pop("Accept-Language", None)
        for name, data in sources.items():
            p = out_dir / f"st_labels_{kind}_{name}.json"
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            pairs = _pairs(data, codes)
            print(f"  [{name}] кодов с подписями: {len(pairs)}/{len(codes)}  -> {p.name}")
            for c in sorted(pairs):
                print(f"     {c:4} " + " | ".join(sorted(pairs[c]))[:150])
            any_found |= bool(pairs)
    print(f"\n[облако] запросов {Api.calls}, ожидание по лимиту {Api.limited_s:.0f} с")
    print("[ИТОГ] подписи " + ("НАЙДЕНЫ" if any_found else "в облаке не найдены — остаётся словарь Оператора"))
    return 0 if any_found else 1


if __name__ == "__main__":
    sys.exit(main())
