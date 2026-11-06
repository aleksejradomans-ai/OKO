"""st_pause_test.py — пауза работающей стиральной/сушильной Samsung через облако, со сверкой (замер, 28.10.2026).

Единственное действие — machineState -> pause, и ТОЛЬКО если машина сейчас в run. Возобновления, запуска,
остановки программы нет. Команда и её аргумент проверяются по описанию capability в API, не угадываются.
Сверка: чтения через +5 и +15 с. Ответ облака (ACCEPTED/COMPLETED) не доказательство — 28.10 утром
облако принимало вкл/выкл, а машина их не исполняла (без Smart Control).

Запуск (OKO_SMARTTHINGS_TOKEN в окружении):  python st_pause_test.py [--dev washer|dryer]
Коды: 0 пауза подтверждена чтением, 1 облако приняло, машина не перешла, 3 токен/права, 4 машина не в run / нет команды.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error

from st_mode_walk import KINDS, Api, _read, _val


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dev", default="washer", choices=("washer", "dryer"))
    a = ap.parse_args()
    token = os.environ.get("OKO_SMARTTHINGS_TOKEN", "").strip()
    if not token:
        raise SystemExit("СТОП: нет OKO_SMARTTHINGS_TOKEN")
    api = Api(token)
    cap = f"{a.dev}OperatingState"
    try:
        dev = next((d for d in api.get("/devices").get("items") or []
                    if KINDS.get((d.get("ocf") or {}).get("ocfDeviceType")) == a.dev), None)
        if dev is None:
            print(f"СТОП: {a.dev} не найден")
            return 4
        dev_id = dev["deviceId"]
        m = _read(api, dev_id)
        ms = _val(m, cap, "machineState")
        print(f"[{dev.get('label')}] machineState={ms} remoteControl="
              f"{_val(m, 'remoteControlStatus', 'remoteControlEnabled')}")
        if ms != "run":
            print("СТОП: машина не в run — пауза не нужна")
            return 4
        cmds = (api.get(f"/capabilities/{cap}/1").get("commands") or {})
        if "setMachineState" not in cmds:
            print(f"СТОП: в {cap} нет setMachineState; есть {list(cmds)}")
            return 4
        args = (cmds["setMachineState"].get("arguments") or [{}])[0].get("schema", {})
        print(f"  setMachineState допускает: {args.get('enum', args)}")
        t0 = time.monotonic()
        # Api.command запрещает setMachineState намеренно; здесь — единственный разрешённый вызов, напрямую.
        res = api._req("POST", f"/devices/{dev_id}/commands", {"commands": [
            {"component": "main", "capability": cap, "command": "setMachineState", "arguments": ["pause"]}]})
        st = ",".join(r.get("status", "?") for r in res.get("results", [])) or "?"
        print(f"  pause -> ответ облака {st}")
        for at in (5, 15):
            time.sleep(max(0.0, at - (time.monotonic() - t0)))
            now = _val(_read(api, dev_id), cap, "machineState")
            print(f"  +{at:2d} с  machineState={now}")
            if now == "pause":
                print("[ИТОГ] пауза подтверждена чтением. Проверьте дисплей машины.")
                return 0
        print("[ИТОГ] облако приняло, машина на паузу не перешла — остановите кнопкой.")
        return 1
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        print(f"[ЛОГИКА] HTTP {e.code}: {body}")
        return 3
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[ТРАНСПОРТ] {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
