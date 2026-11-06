"""st_mode_walk.py — стиральная/сушильная Samsung: включить, перебрать программы, выключить (замер записи, 28.10.2026).

Что делает для каждой машины (washer, dryer):
  1. читает состояние; если машина РАБОТАЕТ (machineState=run/pause) — не трогает её;
  2. если дистанционное управление выключено (remoteControlStatus) — не трогает, говорит, что включить на машине;
  3. switch on -> пауза -> СВЕРКА чтением;
  4. перебирает ВСЕ программы из supportedCycles: команда выбора -> пауза -> сверка чтением;
  5. возвращает исходную программу, switch off, сверка.
ПРОГРАММУ НЕ ЗАПУСКАЕТ: команды machineState (run) не отправляются никогда — пустая машина не заливает воду и не греет.
Имя команды выбора программы и её аргумента берётся из определения capability в API, а не угадывается.
В конце (и аварийно: сбой, Ctrl+C) — всегда switch off: до машины в работе скрипт не доходит, значит она простаивала.
У Samsung дистанционное управление обычно включается только на ВКЛЮЧЁННОЙ машине (кнопка Smart Control),
поэтому типичный путь: включить кнопкой -> Smart Control -> скрипт -> он выключит сам.

Ответ API "ACCEPTED" ничего не доказывает (урок телевизора) — доказательство только в следующем чтении.
Токену нужны права на управление устройствами (x:devices), не только чтение.
Коды: 0 всё подтверждено, 1 часть программ не подтверждена, 3 права/токен, 4 в работе/нет remoteControl,
      5 не включилась / нет capability, 6 не выключилась.
Запуск:  OKO_SMARTTHINGS_TOKEN в окружении; python st_mode_walk.py [--only washer|dryer] [--delay 3] [--force] [--power-cycle]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.smartthings.com/v1"
TIMEOUT_S = 15
KINDS = {"oic.d.washer": "washer", "oic.d.dryer": "dryer"}
FORBIDDEN_COMMANDS = {"setMachineState", "start", "run", "pause", "stop"}
MAX_429 = 4
POLL_S = 1.0            # чаще — упираемся в лимит облака (замер 28.10)
FIRST_READ_S = 0.8      # замер 28.10: программа применяется за 0.2–1.2 с


class Api:
    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    calls = 0
    limited_s = 0.0

    def _req(self, method: str, path: str, body: dict | None = None):
        """28.10: облако режет частые запросы (429, «attempt 10 limit 10 - retry in N millis»).
        Ждём сколько сказано и повторяем, до MAX_429 раз; время ожидания копится в limited_s."""
        data = json.dumps(body).encode() if body is not None else None
        hdr = dict(self.h, **({"Content-Type": "application/json"} if data else {}))
        for attempt in range(MAX_429 + 1):
            req = urllib.request.Request(API + path, data=data, headers=hdr, method=method)
            Api.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                    raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == MAX_429:
                    raise
                body_txt = e.read().decode("utf-8", "replace")
                m = re.search(r"retry in (\d+) millis", body_txt)
                wait = int(m.group(1)) / 1000 + 0.5 if m else float(e.headers.get("Retry-After") or 5)
                Api.limited_s += wait
                print(f"      [ЛИМИТ] 429 на {method} — жду {wait:.1f} с")
                time.sleep(wait)
        raise RuntimeError("unreachable")

    def get(self, path: str):
        return self._req("GET", path)

    def command(self, dev_id: str, capability: str, command: str, args: list) -> str:
        if command in FORBIDDEN_COMMANDS:
            raise RuntimeError(f"запрещённая команда {command}")
        res = self._req("POST", f"/devices/{dev_id}/commands",
                        {"commands": [{"component": "main", "capability": capability,
                                       "command": command, "arguments": args}]})
        return ",".join(r.get("status", "?") for r in res.get("results", [])) or "?"


def _main(status: dict) -> dict:
    return (status.get("components") or {}).get("main") or {}


def _val(main: dict, cap: str, attr: str):
    return ((main.get(cap) or {}).get(attr) or {}).get("value")


def _cycle_info(api: Api, main: dict):
    """(capability, attribute, set_command, [коды программ]) или None."""
    for cap in sorted(main):
        m = re.fullmatch(r"samsungce\.(washer|dryer)Cycle", cap)
        if not m:
            continue
        attr = f"{m.group(1)}Cycle"
        supported = _val(main, cap, "supportedCycles") or []
        codes = []
        for s in supported:
            code = s.get("cycle") if isinstance(s, dict) else s
            if code:
                codes.append(str(code))
        definition = api.get(f"/capabilities/{cap}/1")
        cmds = definition.get("commands") or {}
        setters = [c for c in cmds if c.lower().startswith("set") and c not in FORBIDDEN_COMMANDS]
        exact = "set" + attr[0].upper() + attr[1:]          # washerCycle -> setWasherCycle
        if exact in setters:
            setters = [exact]                                # 28.10: у стиральной есть ещё setCycleType
        if len(setters) != 1:
            print(f"  [ЛОГИКА] {cap}: команд выбора {setters} — ожидалась одна, перебор пропущен")
            return None
        return cap, attr, setters[0], codes
    return None


def _read(api: Api, dev_id: str) -> dict:
    return _main(api.get(f"/devices/{dev_id}/status"))


def _same_cycle(got, code) -> bool:
    """28.10: supportedCycles даёт код "51", а чтение — "Table_03_Course_51"."""
    if got is None or code is None:
        return False
    g, c = str(got).upper(), str(code).upper()
    return g == c or g.endswith("_" + c)


def _wait_for(api: Api, dev_id: str, get, pred, timeout: float, step: float = POLL_S):
    """Опрашивать, пока pred(значение) не станет истинным или не выйдет timeout. -> (значение, секунды)."""
    t0 = time.monotonic()
    time.sleep(FIRST_READ_S)
    while True:
        v = get(_read(api, dev_id))
        if pred(v) or time.monotonic() - t0 >= timeout:
            return v, time.monotonic() - t0
        time.sleep(step)


def walk(api: Api, dev: dict, delay: float, force: bool = False, power_cycle: bool = False) -> int:
    dev_id, name = dev["deviceId"], dev.get("label") or dev.get("name")
    kind = KINDS[(dev.get("ocf") or {}).get("ocfDeviceType")]
    print(f"=== {name} ({kind}) ===")
    main = _read(api, dev_id)
    ms = _val(main, f"{kind}OperatingState", "machineState")
    remote = _val(main, "remoteControlStatus", "remoteControlEnabled")
    sw0 = _val(main, "switch", "switch")
    print(f"  исходно: switch={sw0} machineState={ms} remoteControl={remote}")
    if ms in ("run", "pause"):
        print("  СТОП: машина в работе — не трогаю")
        return 4
    if str(remote).lower() not in ("true", "1") and force:
        print("  [ПРЕДУПРЕЖДЕНИЕ] remoteControl=false, --force: команды отправляются для замера реакции облака."
              " Успех = только подтверждение ЧТЕНИЕМ, не ответ ACCEPTED.")
    elif str(remote).lower() not in ("true", "1"):
        print("  СТОП: дистанционное управление выключено. На машине: включить и нажать Smart Control"
              " (значок Wi-Fi/«пульт»), затем повторить.")
        return 4

    rc = 0
    try:
        if power_cycle and sw0 == "on":
            print(f"  [ВКЛ/ВЫКЛ] switch off -> {api.command(dev_id, 'switch', 'off', [])}")
            sw, dt = _wait_for(api, dev_id, lambda m: _val(m, "switch", "switch"), lambda v: v == "off", 10.0)
            print(f"  [СВЕРКА] switch={sw} за {dt:.1f} с")
            if sw != "off":
                print("  [ЛОГИКА] выключение не подтверждено — проверка включения невозможна")
                return 6
            sw0 = "off"
        if sw0 != "on":
            print(f"  switch on -> {api.command(dev_id, 'switch', 'on', [])}")
            sw, dt = _wait_for(api, dev_id, lambda m: _val(m, "switch", "switch"), lambda v: v == "on", 15.0)
            main = _read(api, dev_id)
            print(f"  [СВЕРКА] switch={sw} за {dt:.1f} с")
            if sw != "on":
                print("  [ЛОГИКА] команда принята, но машина не включилась — дальше не иду")
                return 5

        info = _cycle_info(api, main)
        if info is None:
            print("  [ЛОГИКА] capability выбора программы не найдена")
            rc = 5
        else:
            cap, attr, setter, codes = info
            orig = _val(main, cap, attr)
            print(f"  программы ({len(codes)}): команда {cap}.{setter}; исходная {orig}")
            ok = 0
            getter = lambda m: _val(m, cap, attr)  # noqa: E731
            # первой ставим программу, отличную от исходной: иначе «совпало» ничего не доказывает
            order = [c for c in codes if not _same_cycle(orig, c)] + [c for c in codes if _same_cycle(orig, c)]
            for code in order:
                st = api.command(dev_id, cap, setter, [code])
                got, dt = _wait_for(api, dev_id, getter, lambda v, c=code: _same_cycle(v, c), delay)
                hit = _same_cycle(got, code)
                ok += hit
                print(f"    {'OK ' if hit else 'НЕТ'} {code:8} ответ={st:9} чтение={got}  ({dt:.1f} с)")
            print(f"  [ИТОГ] подтверждено чтением {ok}/{len(codes)}")
            orig_code = next((c for c in codes if _same_cycle(orig, c)), None)
            if orig_code:
                api.command(dev_id, cap, setter, [orig_code])
                got, _ = _wait_for(api, dev_id, getter, lambda v: _same_cycle(v, orig_code), delay)
                print(f"  исходная программа возвращена: {got}")
            if ok != len(codes):
                rc = 1
    finally:
        if True:   # машина простаивала (иначе выход выше) — выключаем в любом случае
            try:
                print(f"  switch off -> {api.command(dev_id, 'switch', 'off', [])}")
                sw, dt = _wait_for(api, dev_id, lambda m: _val(m, "switch", "switch"), lambda v: v == "off",
                                   max(delay, 8.0))
                print(f"  [СВЕРКА] switch={sw} за {dt:.1f} с"
                      + ("" if sw == "off" else "  [ЛОГИКА] выключение принято облаком, машина осталась включённой"))
                if sw != "off":
                    rc = max(rc, 6)
            except Exception as e:  # noqa: BLE001 — аварийная ветка должна сказать, что не вышло
                print(f"  [АВАРИЯ] switch off не прошёл: {type(e).__name__}: {e} — выключите машину кнопкой")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=("washer", "dryer"))
    ap.add_argument("--delay", type=float, default=3.0, help="предел ожидания сверки, с (опрос каждые 0.4 с)")
    ap.add_argument("--force", action="store_true", help="не останавливаться на remoteControl=false (замер реакции облака)")
    ap.add_argument("--power-cycle", action="store_true", help="включённую машину сначала выключить и включить командой (замер вкл/выкл)")
    a = ap.parse_args()
    token = os.environ.get("OKO_SMARTTHINGS_TOKEN", "").strip()
    if not token:
        raise SystemExit("СТОП: нет OKO_SMARTTHINGS_TOKEN")
    api = Api(token)
    try:
        devs = [d for d in (api.get("/devices").get("items") or [])
                if KINDS.get((d.get("ocf") or {}).get("ocfDeviceType")) and
                (not a.only or KINDS[d["ocf"]["ocfDeviceType"]] == a.only)]
    except urllib.error.HTTPError as e:
        print(f"[ЛОГИКА] HTTP {e.code}" + (": токен неверен или просрочен" if e.code == 401 else ""))
        return 3
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[ТРАНСПОРТ] облако SmartThings недоступно: {type(e).__name__}: {e}")
        return 2
    if not devs:
        print("СТОП: стиральная/сушильная машина в аккаунте не найдены")
        return 4

    worst = 0
    for d in devs:
        try:
            rc = walk(api, d, a.delay, a.force, a.power_cycle)
        except urllib.error.HTTPError as e:
            msg = "у токена нет прав на управление (нужен x:devices)" if e.code == 403 else e.reason
            try:
                body = e.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                body = ""
            print(f"  [ЛОГИКА] HTTP {e.code}: {msg}  тело ответа: {body}")
            rc = 3
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  [ТРАНСПОРТ] {type(e).__name__}: {e}")
            rc = 2
        worst = max(worst, rc)
    print(f"[облако] запросов {Api.calls}, ожидание по лимиту {Api.limited_s:.0f} с")
    return worst


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(7)
