"""ipp_probe.py — состояние принтера по IPP (порт 631), только чтение, без драйверов и библиотек (28.10.2026).

Шлёт одну операцию Get-Printer-Attributes (RFC 8011) и печатает: модель, состояние, причины состояния,
расходники (marker-names / marker-levels / marker-colors / marker-types). Ничего не печатает и не меняет.
Путь IPP у разных моделей разный — пробуются по очереди: /ipp/print, /ipp, /ipp/printer, /.

Уровни marker-levels: 0..100 — проценты; -1 — неизвестно; -2 — неизвестно, -3 — «есть, но мало/не измеряется»
(RFC 3805/8011). Отрицательные значения печатаются как есть, без пересчёта в проценты.

Запуск:  python ipp_probe.py --host 192.168.50.41
"""
from __future__ import annotations

import argparse
import struct
import sys
import urllib.error
import urllib.request

PATHS = ("/ipp/print", "/ipp", "/ipp/printer", "/")
WANT = ("printer-make-and-model", "printer-info", "printer-location", "printer-state", "printer-state-reasons",
        "printer-state-message", "marker-names", "marker-levels", "marker-colors", "marker-types",
        "printer-is-accepting-jobs", "queued-job-count")
STATES = {3: "готов (idle)", 4: "печатает (processing)", 5: "остановлен (stopped)"}


def _attr(tag: int, name: str, value: bytes) -> bytes:
    n = name.encode()
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(value)) + value


def build_request(uri: str, req_id: int = 1) -> bytes:
    body = struct.pack(">BBHI", 2, 0, 0x000B, req_id)           # IPP 2.0, Get-Printer-Attributes
    body += b"\x01"                                              # operation-attributes-tag
    body += _attr(0x47, "attributes-charset", b"utf-8")
    body += _attr(0x48, "attributes-natural-language", b"en")
    body += _attr(0x45, "printer-uri", uri.encode())
    first = True
    for w in WANT:                                               # 1setOf keyword: имя только у первого
        body += _attr(0x44, "requested-attributes" if first else "", w.encode())
        first = False
    return body + b"\x03"                                        # end-of-attributes


def parse_response(data: bytes) -> tuple[int, dict[str, list]]:
    if len(data) < 8:
        raise ValueError("короткий ответ")
    status = struct.unpack(">H", data[2:4])[0]
    i, attrs, last = 8, {}, None
    while i < len(data):
        tag = data[i]
        i += 1
        if tag == 0x03:
            break
        if tag < 0x10:                                           # delimiter: новая группа
            continue
        nlen = struct.unpack(">H", data[i:i + 2])[0]; i += 2
        name = data[i:i + nlen].decode("utf-8", "replace"); i += nlen
        vlen = struct.unpack(">H", data[i:i + 2])[0]; i += 2
        raw = data[i:i + vlen]; i += vlen
        if tag in (0x21, 0x23):                                  # integer, enum
            val = struct.unpack(">i", raw)[0] if vlen == 4 else raw
        elif tag == 0x22:                                        # boolean
            val = bool(raw[0]) if raw else None
        else:
            val = raw.decode("utf-8", "replace")
        if name:
            last = name
            attrs.setdefault(name, []).append(val)
        elif last:
            attrs[last].append(val)
    return status, attrs


def query(host: str, port: int = 631, timeout: float = 8.0) -> tuple[str, int, dict]:
    last_err = None
    for path in PATHS:
        uri = f"ipp://{host}:{port}{path}"
        req = urllib.request.Request(f"http://{host}:{port}{path}", data=build_request(uri),
                                     headers={"Content-Type": "application/ipp"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, attrs = parse_response(r.read())
            if status < 0x0100 and attrs:                         # successful-ok*
                return path, status, attrs
            last_err = f"{path}: IPP status 0x{status:04X}"
        except urllib.error.HTTPError as e:
            last_err = f"{path}: HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, struct.error) as e:
            last_err = f"{path}: {type(e).__name__}: {e}"
    raise RuntimeError(last_err or "нет ответа")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    a = ap.parse_args()
    try:
        path, status, attrs = query(a.host)
    except RuntimeError as e:
        print(f"[ТРАНСПОРТ/ПРОТОКОЛ] IPP не ответил: {e}")
        return 2
    g = lambda k: attrs.get(k, [])                                # noqa: E731
    print(f"[IPP] {a.host}:631{path}  status=0x{status:04X}")
    print(f"  модель     {', '.join(map(str, g('printer-make-and-model'))) or '-'}")
    print(f"  имя/место  {', '.join(map(str, g('printer-info') + g('printer-location'))) or '-'}")
    st = (g("printer-state") or [None])[0]
    print(f"  состояние  {STATES.get(st, st)}; причины: {', '.join(map(str, g('printer-state-reasons'))) or '-'}")
    if g("printer-state-message"):
        print(f"  сообщение  {g('printer-state-message')[0]}")
    names, levels = g("marker-names"), g("marker-levels")
    colors, types = g("marker-colors"), g("marker-types")
    if names:
        print("  расходники:")
        for k, n in enumerate(names):
            lv = levels[k] if k < len(levels) else "?"
            lv_s = f"{lv}%" if isinstance(lv, int) and 0 <= lv <= 100 else f"{lv} (не проценты)"
            col = colors[k] if k < len(colors) else ""
            typ = types[k] if k < len(types) else ""
            print(f"    {n:28} {lv_s:16} {typ} {col}")
    else:
        print("  расходники: принтер их не сообщил")
    other = sorted(set(attrs) - set(WANT) - {"attributes-charset", "attributes-natural-language"})
    if other:
        print(f"  прочие атрибуты в ответе: {', '.join(other)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
