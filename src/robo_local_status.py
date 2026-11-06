"""robo_local_status.py — чтение состояния Roborock ТОЛЬКО по локальной сети (диагностика, 28.10.2026).

Канал: TCP 58867, ключ из файла, сохранённого robo_cloud_inventory.py. Облако не используется:
если локальный канал не поднимется, скрипт так и скажет, а не уйдёт тихо в облако.
Команды только читающие: get_status, get_current_sound, get_sound_volume, get_network_info.

Зависимость: python-roborock (проверено на 7.13.0).
Запуск:  python robo_local_status.py --host 192.168.50.80
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

READ_ONLY = ("get_status", "get_current_sound", "get_sound_volume", "get_network_info")


def _default_store() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "oko" / "roborock.json"


def _pick_device(store: Path, name: str | None) -> dict:
    if not store.exists():
        raise SystemExit(f"СТОП: нет {store} — сначала robo_cloud_inventory.py")
    devs = json.loads(store.read_text(encoding="utf-8")).get("devices") or []
    if name:
        devs = [d for d in devs if d.get("name") == name]
    if len(devs) != 1:
        raise SystemExit(f"СТОП: подходящих устройств {len(devs)}; уточните --name")
    d = devs[0]
    if not d.get("local_key") or not d.get("duid"):
        raise SystemExit("СТОП: в файле нет local_key или duid")
    return d


async def open_local(host: str, dev: dict):
    """Поднять ТОЛЬКО локальный канал. Возвращает (channel, rpc, RoborockException) или None при отказе транспорта."""
    try:
        from roborock.devices.rpc.v1_channel import RpcChannel, RpcStrategy
        from roborock.devices.transport.local_channel import LocalChannel
        from roborock.exceptions import RoborockException
        from roborock.protocols.v1_protocol import decode_rpc_response
        from roborock.roborock_message import RoborockMessageProtocol
        from roborock.util import RoborockLoggerAdapter
    except ImportError as e:
        raise SystemExit(f"СТОП: python-roborock не тот или не установлен ({e})")

    ch = LocalChannel(host, dev["local_key"], dev["duid"])
    try:
        await asyncio.wait_for(ch.connect(), timeout=15)
    except (RoborockException, TimeoutError, OSError) as e:
        print(f"[ТРАНСПОРТ] локальный канал не поднят: {type(e).__name__}: {e}")
        return None
    print(f"[ТРАНСПОРТ] канал поднят, протокол {ch.protocol_version}")
    strategy = RpcStrategy(
        name="local",
        channel=ch,
        encoder=lambda req: req.encode_message(RoborockMessageProtocol.GENERAL_REQUEST, version=ch.protocol_version),
        decoder=decode_rpc_response,
    )
    return ch, RpcChannel(lambda: [strategy], RoborockLoggerAdapter(duid=dev["duid"], name="oko")), RoborockException


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--name", default=None, help="имя устройства, если их несколько")
    ap.add_argument("--store", type=Path, default=_default_store())
    a = ap.parse_args()

    dev = _pick_device(a.store, a.name)
    print(f"[устройство] {dev.get('name')}  {dev.get('model')}  fw={dev.get('fv')}  -> {a.host}:58867")
    opened = await open_local(a.host, dev)
    if opened is None:
        return 2
    ch, rpc, RoborockException = opened

    rc = 0
    for cmd in READ_ONLY:
        try:
            res = await rpc.send_command(cmd)
            print(f"[ДАННЫЕ] {cmd}: {json.dumps(res, ensure_ascii=False)}")
        except RoborockException as e:
            print(f"[ОТКАЗ]  {cmd}: {type(e).__name__}: {e}")
            rc = 1
    ch.close()
    return rc


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
