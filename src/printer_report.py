"""printer_report.py — «виртуальный лист» сетевой информации принтера: то же, что печатный отчёт
«Network Information / Wireless LAN», но получено по сети, без бумаги (только чтение, 28.10.2026).

Источники — стандартные, не фирменные:
  IPP  (порт 631): модель, прошивка, UUID, адреса, состояние, предупреждения;
  SNMP (UDP 161, public): системные поля, серийный номер, СЧЁТЧИК СТРАНИЦ, интерфейсы и MAC, IP-адреса.
Чего здесь НЕТ и почему: уровень сигнала Wi-Fi и параметры беспроводной сети в стандартных таблицах SNMP/IPP
не описаны — если они есть на бумажном листе, их надо искать в фирменных данных Canon (отдельный шаг).
Отчёт пишется в %LOCALAPPDATA%\\oko\\printer_report_<время>.txt — чтобы сравнить с бумажным листом построчно.

Запуск:  python printer_report.py --host 192.168.50.41
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

import ipp_probe
import printer_sources_probe as P

SCALARS = [
    ("1.3.6.1.2.1.1.1.0", "описание системы (sysDescr)"),
    ("1.3.6.1.2.1.1.5.0", "имя (sysName)"),
    ("1.3.6.1.2.1.1.6.0", "место (sysLocation)"),
    ("1.3.6.1.2.1.1.3.0", "время работы, 1/100 с (sysUpTime)"),
    ("1.3.6.1.2.1.25.3.2.1.3.1", "устройство (hrDeviceDescr)"),
    ("1.3.6.1.2.1.43.5.1.1.17.1", "серийный номер (prtGeneralSerialNumber)"),
    ("1.3.6.1.2.1.43.10.2.1.4.1.1", "СТРАНИЦ НАПЕЧАТАНО ВСЕГО (prtMarkerLifeCount)"),
]
WALKS = [
    ("1.3.6.1.2.1.2.2.1.2", "интерфейс"),
    ("1.3.6.1.2.1.2.2.1.6", "MAC интерфейса"),
    ("1.3.6.1.2.1.4.20.1.1", "IP-адрес"),
    ("1.3.6.1.2.1.4.20.1.3", "маска"),
]
IPP_FIELDS = ("printer-make-and-model", "printer-firmware-name", "printer-firmware-string-version",
              "printer-firmware-version", "printer-uuid", "printer-device-id", "printer-uri-supported",
              "printer-more-info", "printer-dns-sd-name", "printer-geo-location", "printer-state",
              "printer-state-reasons", "printer-alert-description", "printer-up-time",
              "printer-config-change-date-time", "printer-current-time", "printer-name",
              "media-ready", "media-col-ready")


def _snmp(host: str, oid: str, tag: int, timeout: float = 2.0) -> tuple[str, int, bytes, int]:
    """Один запрос SNMP v1 (0xA0 GET / 0xA1 GETNEXT). -> (oid ответа, тег значения, сырые байты, error-status)."""
    pdu = P._tlv(tag, P._int(3) + P._int(0) + P._int(0) + P._tlv(0x30, P._tlv(0x30, P._oid(oid) + b"\x05\x00")))
    msg = P._tlv(0x30, P._int(0) + P._tlv(0x04, b"public") + pdu)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(msg, (host, 161))
        data, _ = s.recvfrom(65535)
    _, seq, _ = P._read(data, 0)
    i = 0
    for _ in range(2):
        _, _, i = P._read(seq, i)
    _, pdu_b, _ = P._read(seq, i)
    j = 0
    _, _, j = P._read(pdu_b, j)
    _, err, j = P._read(pdu_b, j)
    _, _, j = P._read(pdu_b, j)
    _, vbl, _ = P._read(pdu_b, j)
    _, vb, _ = P._read(vbl, 0)
    _, o, k = P._read(vb, 0)
    t, v, _ = P._read(vb, k)
    return P._dec_oid(o), t, v, int.from_bytes(err, "big")


def _decode(t: int, v: bytes, mac: bool = False):
    if t == 0x02:
        return int.from_bytes(v, "big", signed=True)
    if t in (0x41, 0x42, 0x43, 0x46):                        # counter, gauge, timeticks, counter64
        return int.from_bytes(v, "big")
    if t == 0x40:                                            # IpAddress
        return ".".join(map(str, v))
    if t == 0x04:
        if mac or any(b < 32 and b not in (9, 10, 13) for b in v):
            return ":".join(f"{b:02x}" for b in v)           # двоичное (MAC) — не декодировать как текст
        return v.decode("utf-8", "replace")
    return None                                              # noSuchObject/noSuchInstance/endOfMibView


def snmp_get(host: str, oid: str):
    _, t, v, err = _snmp(host, oid, 0xA0)
    return None if err else _decode(t, v)


def snmp_walk(host: str, base: str, limit: int = 50, mac: bool = False) -> list[tuple[str, object]]:
    out, oid = [], base
    for _ in range(limit):
        try:
            oid, t, v, err = _snmp(host, oid, 0xA1)
        except (socket.timeout, OSError):
            break
        if err or not oid.startswith(base + "."):
            break
        out.append((oid[len(base) + 1:], _decode(t, v, mac)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    a = ap.parse_args()
    lines = [f"ВИРТУАЛЬНЫЙ ОТЧЁТ ПРИНТЕРА {a.host}  ({time.strftime('%Y-%m-%d %H:%M')})", ""]

    lines.append("== IPP ==")
    ipp_probe.WANT = ("all",)
    try:
        _, _, at = ipp_probe.query(a.host)
        for f in IPP_FIELDS:
            if f in at:
                lines.append(f"  {f:36} {at[f][0] if len(at[f]) == 1 else at[f]}")
    except RuntimeError as e:
        lines.append(f"  [ТРАНСПОРТ/ПРОТОКОЛ] {e}")

    lines.append("== SNMP ==")
    for oid, name in SCALARS:
        try:
            v = snmp_get(a.host, oid)
        except (socket.timeout, OSError) as e:
            v = f"нет ответа ({type(e).__name__})"
        if name.startswith("время") and isinstance(v, int):
            v = f"{v} ({v // 360000} ч {v // 6000 % 60} мин)"
        lines.append(f"  {name:44} {v if v is not None else '—'}")
    for base, name in WALKS:
        for idx, val in snmp_walk(a.host, base, mac=name.startswith("MAC")):
            lines.append(f"  {name:20} [{idx}] {val}")

    text = "\n".join(lines)
    print(text)
    out = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")) / "oko"
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"printer_report_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    p.write_text(text, encoding="utf-8")
    print(f"\n[сохранено] {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
