"""st_name_walk.py — словарь программ Samsung ОТ ОПЕРАТОРА: код -> название, которое видно на дисплее (28.10.2026).

Принцип: бот говорит словами Оператора, а не кодами вендора. Но слова берутся только из наблюдения,
не придумываются: внешняя модель 28.10 приписала 1C=«Хлопок» и 21=«Быстрая» без единого замера.
Скрипт ставит программу по коду, сверяет чтением облака, и Оператор вводит, что написано на дисплее
(любой раскладкой и транслитом — хранится как введено + нормализованная форма + фонетический ключ).

Ввод: название / пусто = пропустить / «stop» (или «стоп», «cnjg») = закончить.
Уже названные программы пропускаются (можно прерывать и продолжать); --redo — пройти заново.
В конце возвращается исходная программа. Питание и запуск не трогаются.
Словарь: %LOCALAPPDATA%\\oko\\st_names.json  {kind: {code: {"name", "normalized", "key", "t"}}}

Запуск (OKO_SMARTTHINGS_TOKEN в окружении):  python st_name_walk.py --dev washer|dryer [--redo]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
from pathlib import Path

from st_full_walk import RETRY_READ_S, SETTLE_S, _same
from st_mode_walk import KINDS, Api, _read, _val

STOP = {"stop", "стоп", "cnjg", "q", "quit", "exit", "выход"}


def _norm(text: str) -> tuple[str, str]:
    """(нормализованный текст, фонетический ключ) через layout_fix, если он есть в src."""
    try:
        import layout_fix
        n = layout_fix.normalize(text).text
        k = layout_fix.phonetic_key(n) if hasattr(layout_fix, "phonetic_key") else ""
        return n, k
    except Exception:  # noqa: BLE001 — словарь важнее нормализации
        return text, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dev", required=True, choices=("washer", "dryer"))
    ap.add_argument("--redo", action="store_true")
    a = ap.parse_args()
    token = os.environ.get("OKO_SMARTTHINGS_TOKEN", "").strip()
    if not token:
        raise SystemExit("СТОП: нет OKO_SMARTTHINGS_TOKEN")

    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")) / "oko"
    store = base / "st_names.json"
    names = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    mine = names.setdefault(a.dev, {})

    api = Api(token)
    cap, attr = f"samsungce.{a.dev}Cycle", f"{a.dev}Cycle"
    setter = "set" + attr[0].upper() + attr[1:]
    try:
        dev = next((d for d in api.get("/devices").get("items") or []
                    if KINDS.get((d.get("ocf") or {}).get("ocfDeviceType")) == a.dev), None)
        if dev is None:
            print(f"СТОП: {a.dev} в аккаунте не найден")
            return 4
        dev_id = dev["deviceId"]
        m = _read(api, dev_id)
        sw, ms = _val(m, "switch", "switch"), _val(m, f"{a.dev}OperatingState", "machineState")
        orig = _val(m, cap, attr)
        codes = [c.get("cycle") for c in (_val(m, cap, "supportedCycles") or []) if isinstance(c, dict) and c.get("cycle")]
        print(f"[{dev.get('label')}] switch={sw} machineState={ms} программа={orig}; программ {len(codes)}, уже названо {len(mine)}")
        if ms in ("run", "pause"):
            print("СТОП: машина в работе")
            return 4
        if sw != "on":
            print("СТОП: машина выключена — включите кнопкой")
            return 4
    except urllib.error.HTTPError as e:
        print(f"[ЛОГИКА] HTTP {e.code}" + (": токен неверен или просрочен" if e.code == 401 else ""))
        return 3
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[ТРАНСПОРТ] {type(e).__name__}: {e}")
        return 2

    todo = [c for c in codes if a.redo or c not in mine]
    done = 0
    try:
        for i, code in enumerate(todo, 1):
            api.command(dev_id, cap, setter, [code])
            time.sleep(SETTLE_S)
            got = _val(_read(api, dev_id), cap, attr)
            if not _same(got, code):
                time.sleep(RETRY_READ_S)
                got = _val(_read(api, dev_id), cap, attr)
            if not _same(got, code):
                print(f"  [{i}/{len(todo)}] {code}: облако не подтвердило ({got}) — пропуск")
                continue
            ans = input(f"  [{i}/{len(todo)}] код {code}. Название на дисплее (пусто = пропуск, stop = конец): ").strip()
            if ans.lower() in STOP:
                break
            if not ans:
                continue
            n, k = _norm(ans)
            mine[code] = {"name": ans, "normalized": n, "key": k, "t": time.strftime("%Y-%m-%dT%H:%M:%S")}
            base.mkdir(parents=True, exist_ok=True)
            tmp = store.with_suffix(".tmp")
            tmp.write_text(json.dumps(names, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(store)          # после каждого ответа: прерывание ничего не теряет
            done += 1
            print(f"      сохранено: {code} -> «{n}»" + (f"  (введено: {ans})" if n != ans else ""))
    finally:
        if orig:
            try:
                api.command(dev_id, cap, setter, [str(orig).rsplit("_", 1)[-1]])
                time.sleep(SETTLE_S)
                print(f"\n  исходная программа возвращена: {_val(_read(api, dev_id), cap, attr)} (было {orig})")
            except Exception as e:  # noqa: BLE001
                print(f"  [АВАРИЯ] программа не возвращена: {type(e).__name__}: {e}")

    print(f"\n[словарь] {store}: {a.dev} — названо {len(mine)} из {len(codes)} (в этом прогоне {done})")
    for code in codes:
        if code in mine:
            print(f"  {code:4} {mine[code]['normalized']}")
    print(f"[облако] запросов {Api.calls}, ожидание по лимиту {Api.limited_s:.0f} с")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(7)
