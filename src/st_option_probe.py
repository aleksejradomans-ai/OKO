"""st_option_probe.py — одна функция Samsung, сверка ТРЁХ уровней: ответ облака / чтение облака / дисплей машины (28.10.2026).

Зачем: полный прогон 28.10 показал по чтению облака отжим 7/7 и полоскания 6/6, а Оператор видел, что на дисплеях
«не всё и не везде срабатывало». Значит, чтение облака — не доказательство состояния машины.
Здесь после каждого значения скрипт спрашивает Оператора, что на дисплее, и пишет все три уровня в журнал.

Только выбранная функция; питание и запуск не трогаются. Исходное значение возвращается.
Журнал: %LOCALAPPDATA%\\oko\\st_option_probe.jsonl (дописывается).

Запуск (OKO_SMARTTHINGS_TOKEN в окружении):
  python st_option_probe.py --dev washer --cap custom.washerSpinLevel --values 400,800,1200
  python st_option_probe.py --dev washer --cap custom.washerRinseCycles --values 1,3
  python st_option_probe.py --dev dryer  --cap custom.dryerDryLevel --values 1,3
Атрибут и команда выводятся из имени capability (custom.washerSpinLevel -> washerSpinLevel / setWasherSpinLevel)
и проверяются по описанию capability в API.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
from pathlib import Path

from st_full_walk import RETRY_READ_S, SETTLE_S, Defs, _same
from st_mode_walk import FORBIDDEN_COMMANDS, KINDS, Api, _read, _val


YES = {"1", "+", "y", "yes", "da", "d", "ok", "да", "д", "ок", "lf", "l"}          # lf/l — «да»/«д» не в той раскладке
NO = {"0", "-", "n", "no", "net", "nyet", "niet", "нет", "н", "ytn", "ne", "не"}     # ytn — «нет» не в той раскладке
UNKNOWN = {"", "?", "x", "skip", "nevidno", "не видно", "ytdblyj"}


def _ask(prompt: str) -> str:
    """Да/нет латиницей, кириллицей, в неверной раскладке или цифрой. 28.10: «da» не принималось — исправлено."""
    while True:
        a = input(prompt).strip().lower().rstrip(".!")
        if a in YES:
            return "yes"
        if a in NO:
            return "no"
        if a in UNKNOWN:
            return "unknown"
        print("  ответ: da / net / ? (или 1 / 0)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dev", required=True, choices=("washer", "dryer"))
    ap.add_argument("--cap", required=True, help="например custom.washerSpinLevel")
    ap.add_argument("--values", required=True, help="значения через запятую, как в supported-списке")
    ap.add_argument("--cycle", default=None, help="сначала поставить эту программу (код, напр. 27); в конце вернуть исходную")
    a = ap.parse_args()
    token = os.environ.get("OKO_SMARTTHINGS_TOKEN", "").strip()
    if not token:
        raise SystemExit("СТОП: нет OKO_SMARTTHINGS_TOKEN")

    short = a.cap.split(".", 1)[-1]
    attr = short[0].lower() + short[1:]
    setter = "set" + short[0].upper() + short[1:]
    values = [v.strip() for v in a.values.split(",") if v.strip()]
    api, defs = Api(token), None
    try:
        dev = next((d for d in api.get("/devices").get("items") or []
                    if KINDS.get((d.get("ocf") or {}).get("ocfDeviceType")) == a.dev), None)
        if dev is None:
            print(f"СТОП: {a.dev} в аккаунте не найден")
            return 4
        defs = Defs(api)
        dev_id = dev["deviceId"]
        main_ = _read(api, dev_id)
        # имя атрибута не всегда повторяет имя capability (28.10: samsungce.dryerDryingTime -> dryingTime / setDryingTime):
        # если прямого нет — берём единственную пару «X + supportedX» внутри этой capability
        cap_attrs = main_.get(a.cap) or {}
        if attr not in cap_attrs:
            pairs = [n[len("supported"):] for n in cap_attrs
                     if n.startswith("supported") and (n[9:10].lower() + n[10:]) in cap_attrs]
            if len(pairs) != 1:
                print(f"СТОП: в {a.cap} не найден однозначный атрибут; есть: {sorted(cap_attrs)}")
                return 4
            short = pairs[0]
            attr = short[0].lower() + short[1:]
            setter = "set" + short
        if setter in FORBIDDEN_COMMANDS or setter not in defs.commands(a.cap):
            print(f"СТОП: в описании {a.cap} нет команды {setter}; есть: {list(defs.commands(a.cap))}")
            return 4
        sw = _val(main_, "switch", "switch")
        ms = _val(main_, f"{a.dev}OperatingState", "machineState")
        orig = _val(main_, a.cap, attr)
        supported = _val(main_, a.cap, "supported" + short[0].upper() + short[1:])
        print(f"[{dev.get('label')}] switch={sw} machineState={ms}")
        print(f"  программа: {_val(main_, f'samsungce.{a.dev}Cycle', f'{a.dev}Cycle')}")
        print(f"  {a.cap}.{attr} сейчас={orig}; допустимые по машине={supported}")
        if ms in ("run", "pause"):
            print("СТОП: машина в работе")
            return 4
        if sw != "on":
            print("СТОП: машина выключена — включите кнопкой")
            return 4
        cyc_cap, cyc_attr = f"samsungce.{a.dev}Cycle", f"{a.dev}Cycle"
        cyc_setter = "set" + cyc_attr[0].upper() + cyc_attr[1:]
        orig_cycle = _val(main_, cyc_cap, cyc_attr)
        if a.cycle:
            api.command(dev_id, cyc_cap, cyc_setter, [a.cycle])
            time.sleep(SETTLE_S)
            main_ = _read(api, dev_id)
            got_c = _val(main_, cyc_cap, cyc_attr)
            print(f"  программа -> {a.cycle}: чтение {got_c}")
            if not _same(got_c, a.cycle):
                print("СТОП: программа не установилась")
                return 5
            if _ask(f"  На дисплее программа сменилась? da / net / ?: ") == "no":
                print("СТОП: дисплей не подтвердил смену программы")
                return 5
            orig = _val(main_, a.cap, attr)
            print(f"  {a.cap}.{attr} в этой программе сейчас={orig}")
    except urllib.error.HTTPError as e:
        print(f"[ЛОГИКА] HTTP {e.code}" + (": токен неверен или просрочен" if e.code == 401 else ""))
        return 3
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[ТРАНСПОРТ] {type(e).__name__}: {e}")
        return 2

    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    log = Path(base) / "oko" / "st_option_probe.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    disagree = 0
    try:
        for v in values:
            st = api.command(dev_id, a.cap, setter, [v])
            time.sleep(SETTLE_S)
            got = _val(_read(api, dev_id), a.cap, attr)
            if not _same(got, v):
                time.sleep(RETRY_READ_S)
                got = _val(_read(api, dev_id), a.cap, attr)
            cloud_ok = _same(got, v)
            print(f"\n  -> {v}: ответ облака={st}; чтение облака={got} ({'совпало' if cloud_ok else 'НЕ совпало'})")
            disp = _ask(f"  На дисплее машины сейчас {v}? da / net / ?: ")
            rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "dev": a.dev, "cap": a.cap, "value": v,
                   "cloud_status": st, "cloud_read": got, "cloud_ok": cloud_ok, "display": disp}
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if disp != "unknown" and (disp == "yes") != cloud_ok:
                disagree += 1
                print("  [РАСХОЖДЕНИЕ] облако и дисплей говорят разное")
    finally:
        if orig is not None:
            try:
                api.command(dev_id, a.cap, setter, [str(orig)])
                time.sleep(SETTLE_S)
                print(f"\n  исходное возвращено: {_val(_read(api, dev_id), a.cap, attr)} (было {orig})")
            except Exception as e:  # noqa: BLE001
                print(f"  [АВАРИЯ] исходное не возвращено: {type(e).__name__}: {e}")
        if a.cycle and orig_cycle:
            try:
                code = str(orig_cycle).rsplit("_", 1)[-1]
                api.command(dev_id, cyc_cap, cyc_setter, [code])
                time.sleep(SETTLE_S)
                print(f"  исходная программа возвращена: {_val(_read(api, dev_id), cyc_cap, cyc_attr)} (было {orig_cycle})")
            except Exception as e:  # noqa: BLE001
                print(f"  [АВАРИЯ] программа не возвращена: {type(e).__name__}: {e}")
    print(f"[журнал] {log}")
    print(f"[облако] запросов {Api.calls}, ожидание по лимиту {Api.limited_s:.0f} с; расхождений облако/дисплей: {disagree}")
    return 0 if not disagree else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(7)
