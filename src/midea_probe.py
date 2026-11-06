"""midea_probe.py — опознание устройства Midea по локальной сети, БЕЗ облака (диагностика, 28.10.2026).

Шлёт пакет обнаружения Midea на UDP 6445 указанного адреса и расшифровывает ответ:
тип устройства (байт из имени точки доступа вида net_XX_YYYY), версия протокола, серийный номер, порт.
auto_connect=False: к облаку Midea за токеном НЕ обращается (msmart-ng CLI по умолчанию обращается).
Ничего не включает и не меняет.

Типы Midea (по коду в имени): 0xAC кондиционер, 0xCC коммерческий кондиционер,
0xE2 электрический водонагреватель, 0xE3 газовый водонагреватель, 0xCD водонагреватель с тепловым насосом.
msmart-ng умеет управлять только AC/CC; для остальных скрипт даёт опознание, а не управление.

Зависимость: pip install msmart-ng (проверено на 2026.9.0).
Запуск:  python midea_probe.py --host 192.168.50.150
"""
from __future__ import annotations

import argparse
import asyncio
import sys

KNOWN = {0xAC: "кондиционер", 0xCC: "коммерческий кондиционер", 0xE2: "электрический водонагреватель",
         0xE3: "газовый водонагреватель", 0xCD: "водонагреватель с тепловым насосом"}


def _mask(s) -> str:
    s = str(s or "")
    return s if len(s) <= 6 else f"{s[:4]}{'*' * (len(s) - 8)}{s[-4:]}"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--count", type=int, default=3, help="сколько пакетов обнаружения послать")
    a = ap.parse_args()
    try:
        from msmart.discover import Discover
    except ImportError:
        raise SystemExit("СТОП: нет пакета msmart-ng (pip install msmart-ng)")

    try:
        dev = await asyncio.wait_for(
            Discover.discover_single(a.host, auto_connect=False, discovery_packets=a.count), timeout=20)
    except TimeoutError:
        dev = None
    if dev is None:
        print(f"[ТРАНСПОРТ] {a.host}: ответа на обнаружение UDP 6445 нет")
        return 2

    code = int(dev.type)
    print(f"[ОПОЗНАНО] {a.host}:{dev.port}")
    print(f"  тип       0x{code:02X}  {KNOWN.get(code, 'неизвестный тип Midea')}")
    print(f"  имя       {dev.name}")
    print(f"  версия    V{dev.version}" + ("  (для чтения состояния нужен токен/ключ из облака Midea)" if dev.version == 3 else ""))
    print(f"  id        {dev.id}")
    print(f"  sn        {_mask(dev.sn)}")
    print(f"  msmart    управление {'поддерживается' if code in (0xAC, 0xCC) else 'НЕ поддерживается — только опознание'}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
