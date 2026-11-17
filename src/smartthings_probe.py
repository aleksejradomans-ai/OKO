"""smartthings_probe.py — опись устройств SmartThings (Samsung) из аккаунта Оператора (диагностика, 28.10.2026).

Только чтение: GET /v1/devices и GET /v1/devices/{id}/status. Ничего не включает и не меняет.
Канал — облако SmartThings: у бытовой техники Samsung локального API нет, поэтому сетевой отказ
здесь означает «нет интернета или облака», а не «нет машины в сети».

Токен: Personal Access Token (account.smartthings.com/tokens, права только на чтение устройств).
Берётся из переменной OKO_SMARTTHINGS_TOKEN, иначе из %LOCALAPPDATA%\\oko\\smartthings_token.txt,
иначе спрашивается без эха. --save кладёт введённый токен в этот файл (вне репозитория).

Запуск:  python smartthings_probe.py [--save]
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.smartthings.com/v1"
TIMEOUT_S = 15
# атрибуты, по которым видно «что делает машина» — печатаются отдельно
KEY_HINTS = ("machineState", "JobState", "jobState", "operatingState", "completionTime", "remainingTime",
             "progress", "switch")


def _token_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "oko" / "smartthings_token.txt"


def _get_token(save: bool) -> str:
    t = os.environ.get("OKO_SMARTTHINGS_TOKEN", "").strip()
    if t:
        return t
    f = _token_file()
    if f.exists():
        t = f.read_text(encoding="utf-8").strip()
        if t:
            return t
    t = getpass.getpass("SmartThings token (ввод не отображается): ").strip()
    if not t:
        raise SystemExit("СТОП: пустой токен")
    if save:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(t, encoding="utf-8")
        print(f"[токен] сохранён в {f}")
    return t


def _get(path: str, token: str):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def _interesting(status: dict) -> list[str]:
    out = []
    main = (status.get("components") or {}).get("main") or {}
    for cap, attrs in sorted(main.items()):
        for attr, val in sorted((attrs or {}).items()):
            if any(h.lower() in attr.lower() for h in KEY_HINTS):
                out.append(f"{cap}.{attr} = {json.dumps((val or {}).get('value'), ensure_ascii=False)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--save", action="store_true", help="сохранить введённый токен вне репозитория")
    a = ap.parse_args()
    token = _get_token(a.save)

    try:
        devs = _get("/devices", token).get("items") or []
    except urllib.error.HTTPError as e:
        print(f"[ЛОГИКА] HTTP {e.code}: {'токен неверен или без прав' if e.code in (401, 403) else e.reason}")
        return 3
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[ТРАНСПОРТ] облако SmartThings недоступно: {type(e).__name__}: {e}")
        return 2

    print(f"[аккаунт] устройств: {len(devs)}")
    rc = 0
    for d in devs:
        print("-" * 70)
        print(f"имя        {d.get('label') or d.get('name')}")
        print(f"id         {d.get('deviceId')}")
        print(f"тип        {(d.get('ocf') or {}).get('ocfDeviceType')}  {d.get('manufacturerName')}"
              f"  модель={(d.get('ocf') or {}).get('modelNumber')}")
        try:
            st = _get(f"/devices/{d.get('deviceId')}/status", token)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"статус     [ОТКАЗ] {type(e).__name__}: {e}")
            rc = 1
            continue
        lines = _interesting(st)
        for ln in lines or ["(ключевых атрибутов нет)"]:
            print(f"  {ln}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
