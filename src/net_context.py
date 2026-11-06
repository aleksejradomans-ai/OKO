"""net_context.py — смена сети ПК -> проверка контура и ОДНА сводка (29.10.2026, решение Оператора: вариант A).

Ноутбук с ботом ездит (дом / рейс), а адреса устройств в разных сетях разные: 29.10 ТВ был в 10.133.133.0/24,
ПК ушёл в домашний Wi-Fi 192.168.50.0/24 — команда ТВ упала раньше, чем кто-либо узнал о смене сети.

Отпечаток сети = IP шлюза + MAC шлюза (SSID не используется: 29.10 `netsh wlan` вернул пустой SSID).
Шлюз — из `route print -4 0.0.0.0` (строки с числами; локализованные заголовки не разбираются),
MAC шлюза — из ARP (tv_control.arp_table).

Смена засчитывается после CONFIRM_POLLS одинаковых опросов подряд (дребезг Wi-Fi). Нет шлюза — «нет сети»,
состояние не меняется. Первый запуск без сохранённого состояния — только запоминание, без сводки.
Смена, случившаяся пока бот был выключен (сон/рейс), — сводка придёт после старта: состояние в data/net_context.json.

Прогон при смене: ТВ (поиск по MAC проактивно), SmartThings (/devices + /status, как бот), Garmin (синхронизация),
принтер (порт IPP 631). Каждый пункт изолирован: сбой одного не обрывает сводку.
Домашние сети: отмеченные Оператором (`net_context.py mark-home`) и те, где ТВ уже находили.
UDP-пинок /24 при поиске ТВ — ТОЛЬКО в домашней сети (чужую сеть в рейсе не сканируем).
Вне дома локальные устройства — «вне дома», не ⚠️.
Модуль не знает о Telegram: poll_once() -> список текстов, отправляет bot.py (как device_watch).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import config

logger = logging.getLogger("oko.net")

STATE_PATH: Path = config.BASE_DIR / "net_context.json"
CONFIRM_POLLS = 2
POLL_S = 60.0
IPP_PORT = 631
_ROUTE_RE = re.compile(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d{1,3}(?:\.\d{1,3}){3})\s+"
                       r"(\d{1,3}(?:\.\d{1,3}){3})\s+(\d+)\s*$", re.M)


# ---------------------------------------------------------------- отпечаток сети

def default_gateway(run: Callable = subprocess.run) -> tuple[str, str] | None:
    """(шлюз, свой IP) маршрута по умолчанию с наименьшей метрикой; нет маршрута — None."""
    kw: dict[str, Any] = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000           # CREATE_NO_WINDOW: служба без консоли
    try:
        out = run(["route", "print", "-4", "0.0.0.0"], capture_output=True, timeout=10, **kw).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("[NET] route print не выполнен: %s", e)
        return None
    text = out.decode("ascii", errors="replace") if isinstance(out, bytes) else str(out or "")
    rows = [(int(metric), gw, ip) for gw, ip, metric in _ROUTE_RE.findall(text)]
    if not rows:
        return None
    _, gw, ip = min(rows)
    return gw, ip


def _tv():
    import tv_control
    return tv_control


def fingerprint(gateway: Callable = default_gateway, arp: Callable | None = None) -> dict | None:
    """{'key','gw','ip','mac'} или None (нет сети / MAC шлюза ещё не в ARP — опрос не в счёт)."""
    g = gateway()
    if not g:
        return None
    gw, ip = g
    tv = _tv()
    rows = (arp or tv.arp_table)()
    mac = next((tv.norm_mac(m) for a, m in rows if a == gw), "")
    if len(mac) != 12:
        return None
    return {"key": f"{gw}/{mac}", "gw": gw, "ip": ip, "mac": mac}


# ---------------------------------------------------------------- состояние

def load_state(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or STATE_PATH).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path | None = None) -> None:
    p = path or STATE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        logger.warning("[NET] состояние не сохранено: %s", e)


def observe(state: dict, fp: dict | None) -> str:
    """'first' | 'same' | 'pending' | 'changed' | 'none'. Меняет state на месте."""
    if fp is None:
        state.pop("pending", None)
        state.pop("pending_n", None)
        return "none"
    key = fp["key"]
    if not state.get("current"):
        state.update(current=key, current_gw=fp["gw"])
        logger.info("[NET] первая сеть запомнена: шлюз %s", fp["gw"])
        return "first"
    if key == state["current"]:
        state.pop("pending", None)
        state.pop("pending_n", None)
        return "same"
    n = state.get("pending_n", 0) + 1 if state.get("pending") == key else 1
    state.update(pending=key, pending_n=n)
    if n < CONFIRM_POLLS:
        return "pending"
    state.pop("pending", None)
    state.pop("pending_n", None)
    state.update(current=key, current_gw=fp["gw"])
    return "changed"


def is_home(state: dict, key: str) -> bool:
    return key in (state.get("home") or [])


def mark_home(state: dict, key: str) -> None:
    home = list(state.get("home") or [])
    if key not in home:
        home.append(key)
    state["home"] = home


# ---------------------------------------------------------------- проверки

def check_tv(fp: dict, state: dict) -> list[str]:
    mac = os.getenv("TV_MAC", "").strip()
    if not mac:
        return []
    tv = _tv()
    home = is_home(state, fp["key"])
    port = int(os.getenv("TV_PORT", str(tv.DEFAULT_PORT)))
    ip = tv.find_host(mac, poke=tv.poke_subnets if home else (lambda: None))
    if ip and tv.port_open(ip, port):
        old = tv.load_host_cache().get("host")
        tv.save_host_cache(mac, ip)
        mark_home(state, fp["key"])               # ТВ здесь — значит сеть домашняя
        return [f"📺 ТВ: на связи, {ip}" + (f" (был {old})" if old and old != ip else "")]
    if home:
        return ["📺 ТВ: в домашней сети не найден — выключен или подключён к другой сети."]
    return ["📺 ТВ: вне дома — недоступен, это нормально."]


def check_smartthings(fp: dict, state: dict) -> list[str]:
    import device_watch as dw
    if not dw.enabled():
        return []
    token, notes = dw.current_token()             # notes — о поломке/восстановлении OAuth: не терять
    if not token:
        return notes + ["🧺 SmartThings: токена нет — уведомления о стирке и сушке не работают."]
    try:
        snaps = dw.fetch_snapshots(token)
    except dw.RateLimited:
        return notes + ["🧺 SmartThings: облако просит подождать (429), проверка пропущена."]
    except dw.AuthFailed:
        return notes + ["⚠️ SmartThings: облако не принимает токен."]
    except dw.Transport as e:
        return notes + [f"⚠️ SmartThings: облако недоступно ({e})."]
    return notes + [f"🧺 SmartThings: облако отвечает, машин: {len(snaps)}."]


def check_garmin(fp: dict, state: dict) -> list[str]:
    import garmin_report
    import garmin_sync
    if not garmin_report.enabled():
        return []
    day = dt.datetime.now(garmin_report.tz()).date().isoformat()
    try:
        garmin_sync.run_sync(day, 2)
    except garmin_sync.GarminAuthNeeded:
        return ["⚠️ Garmin: вход истёк — нужен разовый вход (garmin_sync.py login)."]
    except Exception as e:
        return [f"⚠️ Garmin: синхронизация не прошла ({type(e).__name__})."]
    return ["⌚ Garmin: синхронизация прошла."]


def check_printer(fp: dict, state: dict) -> list[str]:
    import doc_print
    host = doc_print.printer_host()
    if not host:
        return []
    if _tv().port_open(host, IPP_PORT):
        return [f"🖨 Принтер: на связи, {host}"]
    if is_home(state, fp["key"]):
        return [f"🖨 Принтер: не отвечает ({host}) — выключен или сменил адрес."]
    return ["🖨 Принтер: вне дома — недоступен, это нормально."]


CHECKS: tuple[tuple[str, Callable[[dict, dict], list[str]]], ...] = (
    ("ТВ", check_tv), ("SmartThings", check_smartthings), ("Garmin", check_garmin), ("Принтер", check_printer),
)


def run_checks(fp: dict, state: dict, checks=None) -> str:
    """Заголовок — ПОСЛЕ проверок: check_tv может отметить сеть домашней (30.10 08:19 заголовок сказал
    «не отмечена как домашняя», а ТВ в той же сводке нашёлся и сеть стала домашней)."""
    lines: list[str] = []
    for name, fn in (checks if checks is not None else CHECKS):
        try:
            lines += fn(fp, state)
        except Exception as e:                    # пункт изолирован: остальные проверки идут дальше
            logger.exception("[NET] проверка %s упала", name)
            lines.append(f"⚠️ {name}: проверка не выполнена ({type(e).__name__}).")
    head = (f"📶 Сеть сменилась: шлюз {fp['gw']}, мой адрес {fp['ip']} — "
            + ("домашняя." if is_home(state, fp["key"]) else "не отмечена как домашняя."))
    return "\n".join([head] + lines)


# ---------------------------------------------------------------- точка входа

def poll_once(fp_fn: Callable = fingerprint, checks=None, path: Path | None = None) -> list[str]:
    """Для bot.py (в отдельном потоке: здесь блокирующие вызовы). Не бросает."""
    try:
        fp = fp_fn()
    except Exception:
        logger.exception("[NET] отпечаток сети не получен")
        return []
    state = load_state(path)
    before = json.dumps(state, sort_keys=True)
    verdict = observe(state, fp)
    out: list[str] = []
    if verdict == "changed":
        logger.warning("[NET] сеть сменилась: шлюз %s", fp["gw"])
        out.append(run_checks(fp, state, checks))
    if json.dumps(state, sort_keys=True) != before:   # пишем только изменения: опрос раз в минуту
        save_state(state, path)
    return out


def main(argv: list[str] | None = None) -> int:
    """status | mark-home | run — ручная проверка из консоли."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = (argv if argv is not None else sys.argv[1:] or ["status"])[0]
    fp = fingerprint()
    state = load_state()
    if fp is None:
        print("FAIL: нет маршрута по умолчанию или MAC шлюза в ARP")
        return 2
    if cmd == "status":
        print(f"шлюз {fp['gw']}  мой {fp['ip']}  ключ {fp['key']}")
        print(f"домашняя: {is_home(state, fp['key'])}  запомненная: {state.get('current_gw')}  "
              f"домашних сетей: {len(state.get('home') or [])}")
        return 0
    if cmd == "mark-home":
        mark_home(state, fp["key"])
        state.setdefault("current", fp["key"])
        state.setdefault("current_gw", fp["gw"])
        save_state(state)
        print(f"OK: сеть {fp['gw']} отмечена домашней")
        return 0
    if cmd == "run":
        print(run_checks(fp, state))
        save_state(state)
        return 0
    print(f"FAIL: неизвестная команда {cmd!r} (status | mark-home | run)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
