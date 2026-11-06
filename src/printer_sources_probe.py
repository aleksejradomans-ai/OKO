"""printer_sources_probe.py — все источники уровня чернил принтера, сравнение между собой (только чтение, 28.10.2026).

Повод: IPP (marker-levels) отдал 0 % для обоих картриджей и marker-supply-low-warning, а Оператор видит,
что картриджи полные. Значит, IPP-уровни на этом Canon не отражают реальность — нужно найти, кто отражает.
Источники:
  1. IPP, ВСЕ атрибуты (requested-attributes=all): printer-supply / marker-* / printer-alert / *-low-levels;
  2. SNMP v1 (UDP 161, community public): таблица расходников Printer-MIB 1.3.6.1.2.1.43.11.1.1
     (описание .6, единица .7, максимум .8, уровень .9; -3 = «есть, уровень не измеряется», -2 = неизвестно);
  3. веб-интерфейс (порт 80): строки страниц со словами ink/чернил/%/level.
Сырые ответы — в %LOCALAPPDATA%\\oko\\printer_*.txt. Ничего не печатает и не меняет.

Запуск:  python printer_sources_probe.py --host 192.168.50.41
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path

import ipp_probe

OUT = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")) / "oko"
SUPPLY = "1.3.6.1.2.1.43.11.1.1"
COL = {"6": "описание", "7": "единица", "8": "максимум", "9": "уровень", "4": "класс", "5": "тип"}


# ---------- 1. IPP all ----------
def ipp_all(host: str) -> dict:
    orig = ipp_probe.WANT
    ipp_probe.WANT = ("all",)
    try:
        path, status, attrs = ipp_probe.query(host)
    finally:
        ipp_probe.WANT = orig
    (OUT / "printer_ipp_all.txt").write_text(
        "\n".join(f"{k} = {v}" for k, v in sorted(attrs.items())), encoding="utf-8")
    return attrs


# ---------- 2. SNMP v1 GETNEXT ----------
def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(t: int, v: bytes) -> bytes:
    return bytes([t]) + _len(len(v)) + v


def _int(i: int) -> bytes:
    return _tlv(0x02, i.to_bytes(max(1, (i.bit_length() + 8) // 8), "big", signed=True))


def _oid(o: str) -> bytes:
    p = [int(x) for x in o.split(".")]
    out = bytes([40 * p[0] + p[1]])
    for x in p[2:]:
        enc = [x & 0x7F]
        x >>= 7
        while x:
            enc.insert(0, 0x80 | (x & 0x7F))
            x >>= 7
        out += bytes(enc)
    return _tlv(0x06, out)


def _read(buf: bytes, i: int) -> tuple[int, bytes, int]:
    t = buf[i]
    ln = buf[i + 1]
    i += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(buf[i:i + k], "big")
        i += k
    return t, buf[i:i + ln], i + ln


def _dec_oid(v: bytes) -> str:
    p = [v[0] // 40, v[0] % 40]
    x = 0
    for b in v[1:]:
        x = (x << 7) | (b & 0x7F)
        if not b & 0x80:
            p.append(x)
            x = 0
    return ".".join(map(str, p))


def snmp_getnext(host: str, oid: str, community: str = "public", timeout: float = 2.0):
    pdu = _tlv(0xA1, _int(1) + _int(0) + _int(0) + _tlv(0x30, _tlv(0x30, _oid(oid) + b"\x05\x00")))
    msg = _tlv(0x30, _int(0) + _tlv(0x04, community.encode()) + pdu)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(msg, (host, 161))
        data, _ = s.recvfrom(65535)
    _, seq, _ = _read(data, 0)
    i = 0
    _, _, i = _read(seq, i)                  # version
    _, _, i = _read(seq, i)                  # community
    _, pdu_b, _ = _read(seq, i)
    j = 0
    _, _, j = _read(pdu_b, j)                # request-id
    _, err, j = _read(pdu_b, j)
    _, _, j = _read(pdu_b, j)
    _, vbl, _ = _read(pdu_b, j)
    _, vb, _ = _read(vbl, 0)
    _, o, k = _read(vb, 0)
    t, v, _ = _read(vb, k)
    if t == 0x02:
        val = int.from_bytes(v, "big", signed=True)
    elif t == 0x04:
        val = v.decode("utf-8", "replace")
    else:
        val = v.hex()
    return _dec_oid(o), val, int.from_bytes(err, "big")


def snmp_supplies(host: str, limit: int = 200) -> dict:
    rows: dict[str, dict] = {}
    oid, lines = SUPPLY, []
    for _ in range(limit):
        try:
            oid, val, err = snmp_getnext(host, oid)
        except (socket.timeout, OSError) as e:
            lines.append(f"[ТРАНСПОРТ] {type(e).__name__}: {e}")
            break
        if err or not oid.startswith(SUPPLY + "."):
            break
        lines.append(f"{oid} = {val}")
        rest = oid[len(SUPPLY) + 1:].split(".")
        col, idx = rest[0], ".".join(rest[1:])
        rows.setdefault(idx, {})[COL.get(col, col)] = val
    (OUT / "printer_snmp.txt").write_text("\n".join(lines), encoding="utf-8")
    return rows


# ---------- 3. веб ----------
def web_lines(host: str) -> list[str]:
    hits = []
    for path in ("/", "/index.html", "/status.html", "/rps/", "/m_status.html"):
        try:
            with urllib.request.urlopen(f"http://{host}{path}", timeout=6) as r:
                html = r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
        (OUT / f"printer_web{path.replace('/', '_') or '_'}.txt").write_text(html, encoding="utf-8")
        text = re.sub(r"<[^>]+>", " ", html)
        for ln in re.split(r"[\r\n]+", text):
            ln = " ".join(ln.split())
            if ln and re.search(r"ink|чернил|level|%|remain", ln, re.I):
                hits.append(f"{path}: {ln[:140]}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    print("===== 1. IPP (все атрибуты) =====")
    try:
        at = ipp_all(a.host)
        for k in sorted(at):
            if re.search(r"marker|supply|alert|level|ink|state-reasons", k):
                print(f"  {k} = {at[k]}")
    except RuntimeError as e:
        print(f"  [ТРАНСПОРТ/ПРОТОКОЛ] {e}")

    print("===== 2. SNMP Printer-MIB (расходники) =====")
    rows = snmp_supplies(a.host)
    if rows:
        for idx, r in sorted(rows.items()):
            print(f"  #{idx}: " + "; ".join(f"{k}={v}" for k, v in r.items()))
    else:
        print("  нет ответа или таблица пуста (SNMP выключен на принтере?)")

    print("===== 3. веб-интерфейс =====")
    hits = web_lines(a.host)
    for h in hits[:30]:
        print(f"  {h}")
    if not hits:
        print("  строк про чернила не найдено (страницы, возможно, рисуются скриптом)")
    print(f"\n[сырые ответы] {OUT}\\printer_*.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
