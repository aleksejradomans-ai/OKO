"""robo_say.py — заставить Roborock издать звук по локальному каналу (проверка записи, 28.10.2026).

Разрешены ТОЛЬКО безвредные звуковые команды (робот не трогается с места):
  find_me            — «я здесь» голосом робота (текущий голосовой пакет)
  test_sound_volume  — тестовый звук на текущей громкости
Любая другая команда отвергается до отправки.

Доказательство — то, что Оператор УСЛЫШАЛ звук. Ответ робота "ok" сам по себе ничего не доказывает
(урок телевизора: код 200 на всё подряд).

Запуск:  python robo_say.py --host 192.168.50.80 [--cmd find_me|test_sound_volume]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from robo_local_status import _default_store, _pick_device, open_local

ALLOWED = ("find_me", "test_sound_volume")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--cmd", default="find_me", help="|".join(ALLOWED))
    ap.add_argument("--name", default=None)
    ap.add_argument("--store", type=Path, default=_default_store())
    a = ap.parse_args()
    if a.cmd not in ALLOWED:
        print(f"СТОП: команда {a.cmd!r} не из разрешённых {ALLOWED}")
        return 3

    dev = _pick_device(a.store, a.name)
    print(f"[устройство] {dev.get('name')}  {dev.get('model')}  -> {a.host}:58867")
    opened = await open_local(a.host, dev)
    if opened is None:
        return 2
    ch, rpc, RoborockException = opened
    try:
        res = await rpc.send_command(a.cmd)
        print(f"[ОТВЕТ]  {a.cmd}: {json.dumps(res, ensure_ascii=False)}")
        print("[ПРОВЕРКА] ответ робота — не доказательство. Звук был слышен? (да/нет)")
        return 0
    except RoborockException as e:
        print(f"[ОТКАЗ]  {a.cmd}: {type(e).__name__}: {e}")
        return 1
    finally:
        ch.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
