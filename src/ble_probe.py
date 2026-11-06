"""ble_probe.py — замер Bluetooth LE на этом ПК: что видно рядом и можно ли читать пульс с часов (29.10.2026).

Только чтение. Два режима:
  scan (по умолчанию): 15 с поиска; печатает устройства, уровень сигнала (RSSI) и объявленные сервисы.
     Стандартный сервис пульса Bluetooth — 0x180D (Heart Rate). Garmin отдаёт его, ТОЛЬКО когда на часах
     включена трансляция пульса (Broadcast Heart Rate); в обычном режиме часы говорят лишь со своим
     приложением по закрытому протоколу — и это не взламывается.
  --hr АДРЕС: подписка на 0x2A37 (Heart Rate Measurement) на 30 с, печать уд/мин по стандарту Bluetooth SIG
     (флаг формата 8/16 бит в первом байте).
Нужна библиотека bleak (в среде службы её ставит блок установки).

Запуск:  python ble_probe.py              # поиск
         python ble_probe.py --hr AA:BB:CC:DD:EE:FF [--seconds 30]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"


def parse_hr(data: bytes) -> int:
    """Heart Rate Measurement: бит 0 флага — формат значения (0: uint8, 1: uint16 LE)."""
    if not data:
        raise ValueError("пустой пакет")
    return int.from_bytes(data[1:3], "little") if data[0] & 0x01 else data[1]


async def scan(seconds: float) -> int:
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    if not found:
        print("ничего не найдено (Bluetooth выключен на ПК или рядом нет вещающих устройств)")
        return 1
    rows = []
    for addr, (dev, adv) in found.items():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        hr = HR_SERVICE in uuids
        rows.append((hr, adv.rssi or -999, addr, dev.name or adv.local_name or "", uuids))
    for hr, rssi, addr, name, uuids in sorted(rows, key=lambda r: (not r[0], -r[1])):
        mark = "  <== ПУЛЬС (0x180D)" if hr else ""
        short = ", ".join(u[4:8] for u in uuids[:6])
        print(f"{addr}  RSSI {rssi:>4}  {name[:28]:28}  сервисы: {short}{mark}")
    if not any(r[0] for r in rows):
        print("\nпульсового сервиса не видно: включи на часах трансляцию пульса и повтори поиск")
    return 0


async def heart_rate(address: str, seconds: float) -> int:
    from bleak import BleakClient
    got: list[tuple[float, int]] = []
    t0 = time.monotonic()

    def on_data(_handle, data: bytearray):
        bpm = parse_hr(bytes(data))
        got.append((time.monotonic() - t0, bpm))
        print(f"  +{got[-1][0]:5.1f} с  {bpm} уд/мин")

    async with BleakClient(address, timeout=20.0) as client:
        await client.start_notify(HR_MEASUREMENT, on_data)
        await asyncio.sleep(seconds)
        await client.stop_notify(HR_MEASUREMENT)
    if not got:
        print("подключился, но значений не пришло")
        return 1
    vals = [b for _, b in got]
    print(f"итого {len(vals)} значений за {seconds:.0f} с: мин {min(vals)}, макс {max(vals)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hr", metavar="АДРЕС")
    ap.add_argument("--seconds", type=float, default=None)
    a = ap.parse_args()
    try:
        import bleak  # noqa: F401
    except ImportError:
        print("СТОП: нет библиотеки bleak в этой среде")
        return 4
    try:
        if a.hr:
            return asyncio.run(heart_rate(a.hr, a.seconds or 30.0))
        return asyncio.run(scan(a.seconds or 15.0))
    except Exception as e:  # ошибки WinRT/Bluetooth разнотипны
        print(f"[BLUETOOTH] {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
