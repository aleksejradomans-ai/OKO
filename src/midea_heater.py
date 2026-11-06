"""midea_heater.py — бойлер Midea E2 (V3): ключ один раз из облака, дальше чтение ТОЛЬКО локально (28.10.2026).

Режимы:
  --fetch-key  Обнаружение по LAN (UDP 6445), затем ключ проверяется ЛОКАЛЬНЫМ рукопожатием:
               1) общий ключ библиотеки midea-local — без облака;
               2) если не принят — вход в облако ВАШЕГО приложения Midea (--cloud, по умолчанию SmartHome = MSmartHome),
                  ключи по id устройства. Пароль спрашивается без эха и НЕ сохраняется.
               Сохраняется (в %LOCALAPPDATA%\\oko\\midea.json, вне репозитория) только ключ, который открыл канал.
               28.10: встроенный вход NetHome Plus (msmart-ng) вернул 9999 — устройство не в этом облаке.
  (по умолчанию) Чтение состояния по TCP 6444 с сохранённым ключом. Облако не используется.

Только чтение: нагрев, температура, режимы не меняются.
Зависимости: pip install msmart-ng midea-local  (проверено: msmart-ng 2026.9.0, midea-local 14.0.0).
Запуск:  python midea_heater.py --host 192.168.50.150 --fetch-key
         python midea_heater.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

HEATER_TYPE = 0xE2


def _store() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "oko" / "midea.json"


def _make_device(cfg: dict):
    from midealocal.device import ProtocolVersion
    from midealocal.devices.e2 import MideaE2Device
    return MideaE2Device(
        name=cfg.get("name") or "boiler", device_id=int(cfg["id"]), ip_address=cfg["ip"], port=int(cfg["port"]),
        token=cfg["token"], key=cfg["key"], device_protocol=ProtocolVersion(int(cfg["version"])),
        model=cfg.get("model") or "", subtype=0, customize="",
    )


async def _read(cfg: dict) -> tuple[bool, dict]:
    dev = _make_device(cfg)
    try:
        ok = await asyncio.wait_for(dev.connect(check_protocol=True), timeout=30)
        return ok, (dict(dev.attributes) if ok else {})
    except TimeoutError:
        return False, {}
    finally:
        await dev.close()


async def _try_keys(base: dict, candidates: list[tuple[str, str, str]]) -> int | None:
    """Проверить пары (метка, token, key) локальным рукопожатием; первую принятую сохранить."""
    for label, token, key in candidates:
        cfg = dict(base, token=token, key=key)
        ok, attrs = await _read(cfg)
        print(f"[ПРОВЕРКА] {label}: локальное рукопожатие и чтение -> {'УСПЕХ' if ok else 'отказ'}")
        if ok:
            p = _store()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(dict(cfg, key_source=label), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[сохранено] {p}")
            _print_attrs(attrs)
            return 0
    return None


async def fetch_key(host: str, cloud_name: str, no_cloud: bool, no_prompt: bool = False) -> int:
    try:
        from msmart.discover import Discover
        from midealocal.cloud import DEFAULT_KEYS, get_midea_cloud
    except ImportError as e:
        raise SystemExit(f"СТОП: нет пакета ({e}); pip install msmart-ng midea-local")

    dev = await Discover.discover_single(host, auto_connect=False, discovery_packets=3)
    if dev is None:
        print(f"[ТРАНСПОРТ] {host}: ответа на обнаружение UDP 6445 нет")
        return 2
    if int(dev.type) != HEATER_TYPE:
        print(f"СТОП: тип 0x{int(dev.type):02X}, ожидался 0x{HEATER_TYPE:02X}")
        return 4
    print(f"[LAN] {host}:{dev.port} тип 0x{int(dev.type):02X} V{dev.version} id={dev.id}")
    base = {"name": dev.name, "ip": host, "port": dev.port, "id": dev.id, "version": dev.version, "sn": dev.sn}

    # шаг 1: общий ключ библиотеки — без облака вообще
    defaults = [(f"default_{m}", v["token"], v["key"]) for m, v in DEFAULT_KEYS.items()]
    if (rc := await _try_keys(base, defaults)) is not None:
        return rc
    if no_cloud:
        print("[ИТОГ] общий ключ не принят, облако запрещено (--no-cloud)")
        return 5

    # шаг 2: ключ из облака ВАШЕГО приложения Midea по вашему аккаунту (пароль не сохраняется)
    import getpass
    import aiohttp
    # из окружения процесса (блок PowerShell кладёт их туда через Read-Host -AsSecureString и сразу убирает);
    # getpass в консоли Windows не принимает вставку и часто выглядит как «ввод не работает»
    # --no-prompt: блок PowerShell уже спросил; пустое значение = отказ, а не второй интерактивный запрос.
    # (PowerShell при присваивании пустой строки УДАЛЯЕТ переменную окружения, поэтому «пусто» и «нет» неотличимы.)
    account = os.environ.get("OKO_MIDEA_ACCOUNT", "").strip()
    password = os.environ.get("OKO_MIDEA_PASSWORD", "")
    if not no_prompt:
        account = account or input(f"Аккаунт приложения ({cloud_name}), почта: ").strip()
        password = password or getpass.getpass("Пароль (не отображается, не сохраняется): ")
    if not account or not password:
        print("СТОП: пустой аккаунт или пароль")
        return 3
    async with aiohttp.ClientSession() as session:
        cloud = get_midea_cloud(cloud_name, session, account, password)
        try:
            logged = await cloud.login()
        except Exception as e:  # noqa: BLE001 — библиотека бросает разнородные исключения
            print(f"[ОБЛАКО] вход: {type(e).__name__}: {e}")
            logged = False
        if not logged:
            print(f"[ОБЛАКО] {cloud_name}: вход не выполнен")
            return 3
        print(f"[ОБЛАКО] {cloud_name}: вход выполнен")
        try:
            keys = await cloud.get_cloud_keys(int(dev.id))
        except Exception as e:  # noqa: BLE001
            print(f"[ОБЛАКО] ключи: {type(e).__name__}: {e}")
            return 3
    print(f"[ОБЛАКО] пар ключей получено: {len(keys)}")
    cands = [(f"{cloud_name}_method{m}", v["token"], v["key"]) for m, v in sorted(keys.items())]
    if (rc := await _try_keys(base, cands)) is not None:
        return rc
    print("[ИТОГ] ни один ключ не открыл локальный канал — ничего не сохранено")
    return 5


def _print_attrs(attrs: dict) -> None:
    print("[ДАННЫЕ]")
    for k in sorted(attrs, key=str):
        print(f"  {str(k):28} {attrs[k]}")


async def read_only() -> int:
    p = _store()
    if not p.exists():
        raise SystemExit(f"СТОП: нет {p} — сначала --fetch-key")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    ok, attrs = await _read(cfg)
    if not ok:
        print(f"[ТРАНСПОРТ] {cfg['ip']}:{cfg['port']}: канал не поднят или ключ не принят")
        return 2
    print(f"[LAN] {cfg['ip']}:{cfg['port']} канал поднят, облако не использовалось")
    _print_attrs(attrs)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=None)
    ap.add_argument("--fetch-key", action="store_true")
    ap.add_argument("--cloud", default="SmartHome",
                    help="облако вашего приложения: SmartHome (= MSmartHome), 'NetHome Plus', 'Midea Air', 'Ariston Clima'")
    ap.add_argument("--no-cloud", action="store_true", help="только общий ключ, без облака")
    ap.add_argument("--no-prompt", action="store_true", help="не спрашивать интерактивно; брать OKO_MIDEA_ACCOUNT/PASSWORD")
    a = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if a.fetch_key:
        if not a.host:
            raise SystemExit("СТОП: для --fetch-key нужен --host")
        return asyncio.run(fetch_key(a.host, a.cloud, a.no_cloud, a.no_prompt))
    return asyncio.run(read_only())


if __name__ == "__main__":
    sys.exit(main())
