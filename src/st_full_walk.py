"""st_full_walk.py — полный перебор программ И дополнительных функций стиральной/сушильной Samsung (28.10.2026).

Для каждой ВКЛЮЧЁННОЙ машины (включить кнопкой: удалённое включение без Smart Control не работает — замер 28.10):
  1. программы: каждая из supportedCycles -> команда -> чтение-сверка; исходная возвращается;
  2. доп. функции ТЕКУЩЕЙ программы: всё, что машина сама описывает парой «атрибут X + атрибут supportedX»
     (температура, отжим, полоскания, степень сушки и т.п. — список не зашит, берётся из ответа машины),
     команда set<X> — из определения capability в API, не угадывается; каждое значение -> сверка; исходное возвращается.
Не трогает: питание, machineState/operatingState (никаких запусков), обновления ПО, дистанционное управление.
Сверка: чтение через SETTLE_S после команды; ответ облака ACCEPTED/COMPLETED не считается.
Лимит облака (429) обрабатывается в Api из st_mode_walk.py. Отчёт — JSON вне репозитория:
%LOCALAPPDATA%\\oko\\st_walk_<время>.json — основа справочника команд для Ока.

Запуск:  OKO_SMARTTHINGS_TOKEN в окружении; python st_full_walk.py [--only washer|dryer] [--no-cycles] [--no-options]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
from pathlib import Path

from st_mode_walk import FORBIDDEN_COMMANDS, KINDS, Api, _cycle_info, _read, _same_cycle, _val

SETTLE_S = 1.2          # замер 28.10: программа применяется за 0.2–1.2 с
RETRY_READ_S = 1.5      # одно повторное чтение, если первое не совпало
SKIP_CAP_PARTS = ("OperatingState", "MachineState", "softwareUpdate", "remoteControl", "switch",
                  "DelayEnd", "kidsLock", "childLock", "ocf", "execute", "refresh", "healthCheck")


def _code(v) -> str | None:
    if isinstance(v, dict):
        for k in ("cycle", "id", "value", "name"):
            if isinstance(v.get(k), (str, int)):
                return str(v[k])
        vals = [x for x in v.values() if isinstance(x, (str, int))]
        return str(vals[0]) if len(vals) == 1 else None
    return None if v is None else str(v)


def _same(got, want) -> bool:
    return got is not None and (str(got) == str(want) or _same_cycle(got, want))


class Defs:
    def __init__(self, api: Api):
        self.api, self.cache = api, {}

    def commands(self, cap: str) -> dict:
        if cap not in self.cache:
            try:
                self.cache[cap] = self.api.get(f"/capabilities/{cap}/1").get("commands") or {}
            except urllib.error.HTTPError:
                self.cache[cap] = {}
        return self.cache[cap]


def _options(main: dict, defs: Defs) -> list[tuple[str, str, str, list[str]]]:
    """[(capability, attribute, set_command, [значения])] — только то, что машина сама описала."""
    out = []
    for cap in sorted(main):
        if any(p.lower() in cap.lower() for p in SKIP_CAP_PARTS):
            continue
        if re.fullmatch(r"samsungce\.(washer|dryer)Cycle", cap):   # программы идут отдельным шагом
            continue
        attrs = main.get(cap) or {}
        for sup_name, sup in sorted(attrs.items()):
            if not sup_name.startswith("supported"):
                continue
            values = (sup or {}).get("value")
            if not isinstance(values, list) or len(values) < 2:
                continue
            target = sup_name[len("supported"):]
            target = target[0].lower() + target[1:]
            if target not in attrs:
                continue
            setter = "set" + target[0].upper() + target[1:]
            if setter in FORBIDDEN_COMMANDS or setter not in defs.commands(cap):
                print(f"  [пропуск] {cap}.{target}: команды {setter} в определении нет")
                continue
            codes = [c for c in (_code(v) for v in values) if c]
            out.append((cap, target, setter, codes))
    return out


def _walk_values(api: Api, dev_id: str, cap: str, attr: str, setter: str, codes: list[str], orig) -> dict:
    order = [c for c in codes if not _same(orig, c)] + [c for c in codes if _same(orig, c)]
    rows = []
    for code in order:
        t0 = time.monotonic()
        try:
            st = api.command(dev_id, cap, setter, [code])
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:160]
            print(f"    ОТК {code:22} HTTP {e.code} {body}")
            rows.append({"value": code, "ok": False, "http": e.code})
            continue
        time.sleep(SETTLE_S)
        got = _val(_read(api, dev_id), cap, attr)
        if not _same(got, code):
            time.sleep(RETRY_READ_S)
            got = _val(_read(api, dev_id), cap, attr)
        ok = _same(got, code)
        dt = time.monotonic() - t0
        rows.append({"value": code, "ok": ok, "read": got, "status": st, "s": round(dt, 1)})
        print(f"    {'OK ' if ok else 'НЕТ'} {code:22} ответ={st:9} чтение={got}  ({dt:.1f} с)")
    orig_code = next((c for c in codes if _same(orig, c)), None)
    if orig_code and rows and not _same(rows[-1].get("read"), orig_code):
        api.command(dev_id, cap, setter, [orig_code])
        time.sleep(SETTLE_S)
    n_ok = sum(r["ok"] for r in rows)
    print(f"  [ИТОГ] {cap}.{attr}: {n_ok}/{len(rows)}; исходное {orig}")
    return {"capability": cap, "attribute": attr, "command": setter, "original": orig,
            "ok": n_ok, "total": len(rows), "rows": rows}


def walk(api: Api, dev: dict, defs: Defs, do_cycles: bool, do_options: bool) -> dict:
    dev_id, name = dev["deviceId"], dev.get("label") or dev.get("name")
    kind = KINDS[(dev.get("ocf") or {}).get("ocfDeviceType")]
    print(f"=== {name} ({kind}) ===")
    main = _read(api, dev_id)
    sw, ms = _val(main, "switch", "switch"), _val(main, f"{kind}OperatingState", "machineState")
    print(f"  исходно: switch={sw} machineState={ms}")
    rep = {"name": name, "kind": kind, "switch": sw, "machineState": ms, "groups": []}
    if ms in ("run", "pause"):
        print("  СТОП: машина в работе — не трогаю")
        rep["skipped"] = "running"
        return rep
    if sw != "on":
        print("  СТОП: машина выключена. Включите кнопкой (удалённое включение без Smart Control не работает).")
        rep["skipped"] = "off"
        return rep

    if do_cycles:
        info = _cycle_info(api, main)
        if info:
            cap, attr, setter, codes = info
            print(f"  --- программы ({len(codes)}) ---")
            rep["groups"].append(_walk_values(api, dev_id, cap, attr, setter, codes, _val(main, cap, attr)))
            main = _read(api, dev_id)
    if do_options:
        opts = _options(main, defs)
        print(f"  --- доп. функции текущей программы: {len(opts)} ---")
        for cap, attr, setter, codes in opts:
            print(f"  {cap}.{attr} ({len(codes)}): {setter}")
            rep["groups"].append(_walk_values(api, dev_id, cap, attr, setter, codes, _val(main, cap, attr)))
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=("washer", "dryer"))
    ap.add_argument("--no-cycles", action="store_true")
    ap.add_argument("--no-options", action="store_true")
    a = ap.parse_args()
    token = os.environ.get("OKO_SMARTTHINGS_TOKEN", "").strip()
    if not token:
        raise SystemExit("СТОП: нет OKO_SMARTTHINGS_TOKEN")
    api = Api(token)
    defs = Defs(api)
    t0 = time.monotonic()
    try:
        devs = [d for d in (api.get("/devices").get("items") or [])
                if KINDS.get((d.get("ocf") or {}).get("ocfDeviceType"))
                and (not a.only or KINDS[d["ocf"]["ocfDeviceType"]] == a.only)]
    except urllib.error.HTTPError as e:
        print(f"[ЛОГИКА] HTTP {e.code}" + (": токен неверен или просрочен" if e.code == 401 else ""))
        return 3
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[ТРАНСПОРТ] облако SmartThings недоступно: {type(e).__name__}: {e}")
        return 2

    reports = []
    for d in devs:
        try:
            reports.append(walk(api, d, defs, not a.no_cycles, not a.no_options))
        except urllib.error.HTTPError as e:
            print(f"  [ЛОГИКА] HTTP {e.code}: {e.reason}")
            reports.append({"name": d.get("label"), "error": f"HTTP {e.code}"})
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  [ТРАНСПОРТ] {type(e).__name__}: {e}")
            reports.append({"name": d.get("label"), "error": type(e).__name__})

    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    out = Path(base) / "oko" / f"st_walk_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n[ИТОГ]")
    bad = 0
    for r in reports:
        if r.get("skipped") or r.get("error"):
            print(f"  {r.get('name')}: {r.get('skipped') or r.get('error')}")
            bad += 1
            continue
        for g in r["groups"]:
            print(f"  {r['name'][:20]:20} {g['capability'] + '.' + g['attribute']:55} {g['ok']}/{g['total']}")
            bad += g["ok"] != g["total"]
    print(f"[облако] запросов {Api.calls}, ожидание по лимиту {Api.limited_s:.0f} с, всего {time.monotonic() - t0:.0f} с")
    print(f"[сохранено] {out}")
    print("Машины остаются включёнными — выключите кнопкой (без Smart Control удалённо не выключаются).")
    return 0 if not bad else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(7)
