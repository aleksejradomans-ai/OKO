"""ipp_print_test.py — тестовая печать «hello world» по IPP с минимальным расходом чернил (28.10.2026).

Запрос Оператора: напечатать тестовый лист, минимум чёрной краски, надпись «hello world».
Порядок:
  1. Get-Printer-Attributes: какие форматы, цветность и качество принтер РЕАЛЬНО принимает (не угадываем);
  2. документ: одна страница A4, мелкий текст 10 pt, только чёрный; формат — PDF, если принтер его берёт,
     иначе JPEG (нужен Pillow) — иначе честный отказ с перечнем форматов принтера;
  3. Print-Job с print-color-mode=monochrome и print-quality=draft — только если принтер их поддерживает
     (неподдержанный атрибут мог бы молча игнорироваться или дать отказ — передаём лишь то, что он объявил);
  4. Get-Job-Attributes через 3 и 10 с: состояние задания по словам принтера.
Доказательство — лист в лотке, а не ответ «successful-ok».
Без --go выполняются только шаги 1–2 (проверка без печати).

Запуск:  python ipp_print_test.py --host 192.168.50.41 [--go] [--text "hello world"] [--fill]
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
import time
import urllib.error
import urllib.request

import ipp_probe

JOB_STATES = {3: "ожидает", 4: "удержано", 5: "печатается", 6: "остановлено", 7: "отменено", 8: "прервано",
              9: "завершено"}


def _a(tag: int, name: str, value) -> bytes:
    if isinstance(value, int) and tag in (0x21, 0x23):
        v = struct.pack(">i", value)
    elif isinstance(value, bool):
        v = b"\x01" if value else b"\x00"
    else:
        v = str(value).encode()
    n = name.encode()
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(v)) + v


def _op(op: int, uri: str, rid: int, extra_op: bytes = b"", job: bytes = b"") -> bytes:
    b = struct.pack(">BBHI", 2, 0, op, rid) + b"\x01"
    b += _a(0x47, "attributes-charset", "utf-8") + _a(0x48, "attributes-natural-language", "en")
    b += _a(0x45, "printer-uri", uri) + extra_op
    if job:
        b += b"\x02" + job
    return b + b"\x03"


def _post(url: str, body: bytes, timeout: float = 30.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/ipp"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ipp_probe.parse_response(r.read())


def make_pdf(text: str) -> bytes:
    """Минимальный корректный PDF: A4, Helvetica 10 pt, чёрный (0 g), одна строка в верхнем левом углу."""
    esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 10 Tf 0 g 56 780 Td ({esc}) Tj ET".encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offs:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


FONT_PT = 12          # 28.10: встроенный растровый шрифт Pillow дал ~11 px = <1 мм на листе 300 dpi — надпись едва видна
DPI = 300


def _font(px: int):
    from PIL import ImageFont
    for name in ("arial.ttf", "C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=px)   # Pillow >= 10.1
    except TypeError:
        raise SystemExit("СТОП: нет векторного шрифта и Pillow старый — размер надписи не задать")


MARGIN_IN = 0.8      # ~2 см поля


def make_jpeg(text: str, fill: bool = False) -> bytes:
    """fill=False: 12 pt в верхнем левом углу. fill=True: одна строка на всю ширину листа (минус поля),
    по центру по высоте — размер подбирается по фактической ширине текста в этом шрифте."""
    from PIL import Image, ImageDraw          # только если PDF не принимается
    w, h = round(8.27 * DPI), round(11.69 * DPI)                  # A4, оттенки серого
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    margin = round(MARGIN_IN * DPI)
    if not fill:
        draw.text((margin, margin), text, fill=0, font=_font(round(FONT_PT / 72 * DPI)))
    else:
        avail = w - 2 * margin
        lo, hi = 10, h                                           # двоичный поиск размера по ширине
        while lo < hi:
            mid = (lo + hi + 1) // 2
            l, t, r, b = draw.textbbox((0, 0), text, font=_font(mid))
            if r - l <= avail and b - t <= h - 2 * margin:
                lo = mid
            else:
                hi = mid - 1
        font = _font(lo)
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        draw.text(((w - (r - l)) // 2 - l, (h - (b - t)) // 2 - t), text, fill=0, font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, dpi=(DPI, DPI))
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--go", action="store_true", help="действительно печатать")
    ap.add_argument("--text", default="hello world")
    ap.add_argument("--fill", action="store_true", help="надпись на всю ширину листа (больше чернил)")
    a = ap.parse_args()
    url = f"http://{a.host}:631/ipp/print"
    uri = f"ipp://{a.host}:631/ipp/print"

    req_attrs = b"".join(_a(0x44, "requested-attributes" if i == 0 else "", w) for i, w in enumerate(
        ("document-format-supported", "print-color-mode-supported", "print-quality-supported",
         "media-ready", "printer-state", "printer-state-reasons")))
    try:
        st, at = _post(url, _op(0x000B, uri, 1, req_attrs))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        print(f"[ТРАНСПОРТ] {type(e).__name__}: {e}")
        return 2
    fmts = at.get("document-format-supported", [])
    cms = at.get("print-color-mode-supported", [])
    pqs = at.get("print-quality-supported", [])
    print(f"[принтер] форматы: {fmts}")
    print(f"          цветность: {cms}; качество: {pqs}; бумага: {at.get('media-ready')}; "
          f"состояние: {at.get('printer-state')} {at.get('printer-state-reasons')}")
    if (at.get("printer-state") or [None])[0] == 5:
        print("СТОП: принтер в состоянии «остановлен»")
        return 4

    if "application/pdf" in fmts:
        fmt, doc = "application/pdf", make_pdf(a.text)
    elif "image/jpeg" in fmts:
        try:
            fmt, doc = "image/jpeg", make_jpeg(a.text, a.fill)
        except ImportError:
            print("СТОП: принтер берёт JPEG, но Pillow не установлен")
            return 4
    else:
        print("СТОП: принтер не принимает ни PDF, ни JPEG — нужен растровый формат (отдельная работа)")
        return 4
    job = b"" + _a(0x21, "copies", 1)
    used = []
    if "monochrome" in cms:
        job += _a(0x44, "print-color-mode", "monochrome"); used.append("monochrome")
    if 3 in pqs:
        job += _a(0x23, "print-quality", 3); used.append("draft")
    print(f"[документ] {fmt}, {len(doc)} байт, текст «{a.text}», режимы: {', '.join(used) or 'по умолчанию принтера'}")
    if not a.go:
        print("[проверка] печать не выполнялась (нет --go)")
        return 0

    op_extra = _a(0x42, "requesting-user-name", "oko") + _a(0x42, "job-name", "oko test") + \
        _a(0x49, "document-format", fmt)
    try:
        st, res = _post(url, _op(0x0002, uri, 2, op_extra, job) + doc, timeout=60)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        print(f"[ТРАНСПОРТ] при отправке задания: {type(e).__name__}: {e}")
        return 2
    jid = (res.get("job-id") or [None])[0]
    print(f"[Print-Job] status=0x{st:04X} job-id={jid} job-state={res.get('job-state')}"
          f" причины={res.get('job-state-reasons')}")
    if st >= 0x0100 or jid is None:
        print("[ЛОГИКА] принтер не принял задание")
        return 1
    t0 = time.monotonic()
    for at_s in (3, 10):
        time.sleep(max(0.0, at_s - (time.monotonic() - t0)))
        try:
            _, ja = _post(url, _op(0x0009, uri, 3 + at_s, _a(0x21, "job-id", jid)))
            s = (ja.get("job-state") or [None])[0]
            print(f"  +{at_s:2d} с  задание: {JOB_STATES.get(s, s)}; {ja.get('job-state-reasons')}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            print(f"  +{at_s:2d} с  [ТРАНСПОРТ] {type(e).__name__}: {e}")
    print("[ПРОВЕРКА] доказательство — лист в лотке. Вышел ли он и что на нём?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
