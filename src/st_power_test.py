"""st_power_test.py — медленный замер вкл/выкл стиральной и сушильной Samsung через облако (калибровка, 28.10.2026).

Почему медленно: 28.10 облако SmartThings отвечало 429 уже после ~10 запросов и требовало ждать 43–60 с.
Быстрый опрос мешал отличить «команда не сработала» от «чтение не прошло». Здесь каждое действие —
одна команда и чтения с отметками времени через фиксированные интервалы (по умолчанию 10 и 30 с).
Бюджет: ~3 запроса на действие, 2 действия на машину.

Для каждой машины (если она не в работе):
  исходно выключена:  switch on  -> чтения -> switch off -> чтения
  исходно включена:   switch off -> чтения -> switch on  -> чтения -> switch off -> чтения
Программы не запускаются (команды machineState не отправляются). Ответ COMPLETED — не доказательство.

Запуск:  OKO_SMARTTHINGS_TOKEN в окружении; python st_power_test.py [--only washer|dryer] [--checks 10,30]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error

from st_mode_walk import KINDS, Api, _read, _val


def _act(api: Api, dev_id: str, kind: str, cmd: str, checks: list[float]) -> bool:
    t0 = time.monotonic()
    print(f"  {time.strftime('%H:%M:%S')}  switch {cmd} -> {api.command(dev_id, 'switch', cmd, [])}")
    seen = None
    for at in checks:
        time.sleep(max(0.0, at - (time.monotonic() - t0)))
        m = _read(api, dev_id)
        seen = _val(m, "switch", "switch")
        print(f"  {time.strftime('%H:%M:%S')}  +{time.monotonic() - t0:5.1f} с  switch={seen}"
              f"  machineState={_val(m, f'{kind}OperatingState', 'machineState')}"
              f"  remoteControl={_val(m, 'remoteControlStatus', 'remoteControlEnabled')}")
        if seen == cmd:
            print(f"  [ЗАМЕР] {cmd}: подтверждено чтением за ≤{at:.0f} с")
            return True
    print(f"  [ЛОГИКА] {cmd}: облако приняло, машина не перешла за {checks[-1]:.0f} с")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=("washer", "dryer"))
    ap.add_argument("--checks", default="10,30", help="когда читать после команды, секунды через запятую")
    a = ap.parse_args()
    checks = sorted(float(x) for x in a.checks.split(",") if x.strip())
    token = os.environ.get("OKO_SMARTTHINGS_TOKEN", "").strip()
    if not token:
        raise SystemExit("СТОП: нет OKO_SMARTTHINGS_TOKEN")
    api = Api(token)
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

    results = {}
    for d in devs:
        dev_id, name = d["deviceId"], d.get("label") or d.get("name")
        kind = KINDS[d["ocf"]["ocfDeviceType"]]
        print(f"=== {name} ({kind}) ===")
        try:
            m = _read(api, dev_id)
            sw0, ms = _val(m, "switch", "switch"), _val(m, f"{kind}OperatingState", "machineState")
            print(f"  исходно: switch={sw0} machineState={ms}"
                  f" remoteControl={_val(m, 'remoteControlStatus', 'remoteControlEnabled')}")
            if ms in ("run", "pause"):
                print("  СТОП: машина в работе — не трогаю")
                results[name] = "в работе"
                continue
            plan = ["off", "on", "off"] if sw0 == "on" else ["on", "off"]
            res = []
            for cmd in plan:
                ok = _act(api, dev_id, kind, cmd, checks)
                res.append(f"{cmd}={'да' if ok else 'НЕТ'}")
                if not ok:
                    # контрольное условие: следующий шаг начинался бы не из того состояния и ничего бы не доказал
                    print("  остаток плана пропущен: машина не перешла в нужное состояние")
                    break
            results[name] = " ".join(res)
        except urllib.error.HTTPError as e:
            results[name] = f"HTTP {e.code}"
            print(f"  [ЛОГИКА] HTTP {e.code}: {e.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            results[name] = "транспорт"
            print(f"  [ТРАНСПОРТ] {type(e).__name__}: {e}")
    print("\n[ИТОГ]")
    for k, v in results.items():
        print(f"  {k:24} {v}")
    print(f"[облако] запросов {Api.calls}, ожидание по лимиту {Api.limited_s:.0f} с")
    return 0 if all("НЕТ" not in v and "HTTP" not in v for v in results.values()) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(7)
