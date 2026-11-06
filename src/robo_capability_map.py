"""robo_capability_map.py — измеренная карта команд чтения Roborock (основа «мануала» для Ока, 28.10.2026).

Перебирает ВСЕ команды get_* из python-roborock, кроме исключённых, по локальному каналу,
и записывает для каждой: принята / отказ / таймаут, задержку и ответ.
Это не описание из документации, а то, что ЭТА модель с ЭТОЙ прошивкой реально отвечает.

Исключены: карты и фото (двоичные блобы, по локальному каналу не ходят), ключи и сервера видеосвязи
(get_device_ice/sdp, get_turn_server, get_random_pkey — секреты, им не место в базе знаний), отладка.
Результат: %LOCALAPPDATA%\\oko\\robo_capability_<время>.json — вне репозитория (там серийный номер).

Запуск:  python robo_capability_map.py --host 192.168.50.80
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from robo_local_status import _default_store, _pick_device, open_local

EXCLUDE = {
    "get_map", "get_map_v1", "get_map_v2", "get_fresh_map", "get_persist_map", "get_dynamic_map_diff",
    "get_clean_record_map", "get_recover_map", "get_recover_maps", "get_photo",
    "get_device_ice", "get_device_sdp", "get_turn_server", "get_random_pkey",
    "get_wash_debug_params", "get_testid",
}
PER_CMD_TIMEOUT_S = 12


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--store", type=Path, default=_default_store())
    a = ap.parse_args()

    from roborock.roborock_typing import RoborockCommand
    cmds = sorted(c.value for c in RoborockCommand if c.value.startswith("get_") and c.value not in EXCLUDE)

    dev = _pick_device(a.store, a.name)
    print(f"[устройство] {dev.get('name')}  {dev.get('model')}  fw={dev.get('fv')}; команд к проверке: {len(cmds)}")
    opened = await open_local(a.host, dev)
    if opened is None:
        return 2
    ch, rpc, E = opened

    rows, counts = [], {"ok": 0, "refused": 0, "timeout": 0}
    try:
        for c in cmds:
            t = time.monotonic()
            try:
                res = await asyncio.wait_for(rpc.send_command(c), timeout=PER_CMD_TIMEOUT_S)
                kind, data = "ok", res
            except E as e:
                msg = str(e)
                kind = "timeout" if "timed out" in msg.lower() else "refused"
                data = f"{type(e).__name__}: {msg}"
            except TimeoutError:
                kind, data = "timeout", "TimeoutError"
            ms = round((time.monotonic() - t) * 1000)
            counts[kind] += 1
            rows.append({"cmd": c, "result": kind, "ms": ms, "data": data})
            short = json.dumps(data, ensure_ascii=False)
            print(f"{kind:8} {ms:5} мс  {c:45} {short[:90]}")
    finally:
        ch.close()

    out = _default_store().parent / f"robo_capability_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"device": {k: dev.get(k) for k in ("name", "model", "fv")},
                               "host": a.host, "counts": counts, "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[итог] принято {counts['ok']}, отказ {counts['refused']}, таймаут {counts['timeout']}")
    print(f"[сохранено] {out}")
    return 0 if counts["ok"] else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
