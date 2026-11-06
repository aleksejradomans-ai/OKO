"""robo_cloud_inventory.py — опись устройств Roborock из аккаунта Оператора (диагностика, 28.10.2026).

Что делает: вход в аккаунт Roborock по коду из письма, чтение списка устройств.
Печатает модель, имя, прошивку, онлайн и ЗАМАСКИРОВАННЫЙ локальный ключ.
Полный ключ и данные сессии сохраняются в файл ВНЕ репозитория (по умолчанию
%LOCALAPPDATA%\\oko\\roborock.json), чтобы они не попали ни в git, ни в публикацию.

Ничего не отправляет роботу и ничего не меняет в аккаунте. Только чтение.
Зависимость: pip install python-roborock  (проверено на 7.13.0).

Запуск:  python robo_cloud_inventory.py --email you@example.com
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _default_store() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "oko" / "roborock.json"


def _mask(secret: str | None) -> str:
    if not secret:
        return "<нет>"
    if len(secret) <= 4:
        return "*" * len(secret)
    return f"{secret[:2]}{'*' * (len(secret) - 4)}{secret[-2:]} (len={len(secret)})"


async def _login(client, email: str):
    from roborock.exceptions import RoborockException

    try:
        await client.request_code_v4()
        v4 = True
    except RoborockException as e:
        print(f"[вход] v4 не принят ({type(e).__name__}: {e}); пробую старый способ")
        await client.request_code()
        v4 = False
    print(f"[вход] код отправлен на {email}")
    code = input("Код из письма: ").strip()
    if not code:
        raise SystemExit("СТОП: пустой код")
    if v4:
        try:
            return await client.code_login_v4(code)
        except RoborockException as e:
            print(f"[вход] code_login_v4 отказал ({type(e).__name__}: {e}); пробую code_login")
    return await client.code_login(code)


async def _home(client, user_data):
    from roborock.exceptions import RoborockException

    for name in ("get_home_data_v3", "get_home_data_v2", "get_home_data"):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        try:
            return name, await fn(user_data)
        except RoborockException as e:
            print(f"[дом] {name}: {type(e).__name__}: {e}")
    raise SystemExit("СТОП: ни один вариант get_home_data не ответил")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--email", required=True, help="почта аккаунта Roborock")
    ap.add_argument("--store", type=Path, default=_default_store(), help="куда сохранить ключи (вне репозитория)")
    a = ap.parse_args()

    store = a.store.resolve()
    if (Path("C:/oko").exists() and str(store).lower().startswith("c:\\oko")):
        raise SystemExit(f"СТОП: {store} внутри C:\\oko — ключи не кладутся в репозиторий")

    try:
        from roborock.web_api import RoborockApiClient
    except ImportError:
        raise SystemExit("СТОП: нет пакета python-roborock (pip install python-roborock)")

    client = RoborockApiClient(username=a.email)
    user_data = await _login(client, a.email)
    via, home = await _home(client, user_data)

    models = {p.id: (p.model, p.name) for p in (home.products or [])}
    devices = list(home.devices or []) + list(home.received_devices or [])
    print(f"\n[дом] '{home.name}' через {via}; устройств: {len(devices)}")

    saved = []
    for d in devices:
        model, pname = models.get(d.product_id, ("?", "?"))
        print("-" * 60)
        print(f"имя        {d.name}")
        print(f"модель     {model}  ({pname})")
        print(f"duid       {d.duid}")
        print(f"прошивка   fv={d.fv}  pv={d.pv}")
        print(f"онлайн     {d.online}")
        print(f"local_key  {_mask(d.local_key)}")
        saved.append({"name": d.name, "duid": d.duid, "model": model, "product_name": pname,
                      "fv": d.fv, "pv": d.pv, "local_key": d.local_key})

    store.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.with_suffix(".tmp")
    tmp.write_text(json.dumps({"email": a.email, "user_data": user_data.as_dict(), "devices": saved},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(store)
    print("-" * 60)
    print(f"[сохранено] {store}  (полные ключи — только там)")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
