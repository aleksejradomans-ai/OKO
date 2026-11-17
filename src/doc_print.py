"""doc_print.py — печать документов Ока на сетевой принтер по IPP, без Microsoft Office (28.10.2026).

Цепочка (каждое звено измерено):
  .docx/.xlsx -> PDF        office_render (python-docx/openpyxl -> HTML -> PyMuPDF Story), Office не нужен:
                            Office из-под службы LocalSystem измерен 28.10 — не работает;
  .pdf        -> JPEG       каждая страница -> лист A4 300 dpi, оттенки серого (принтер не берёт PDF, только JPEG);
  JPEG        -> IPP        ОДНО задание на страницу (JPEG одностраничный), monochrome — если принтер его объявил.
Адрес принтера — OKO_PRINTER_HOST (в .env службы). Не задан -> честный отказ.

Из бота: документ с подписью «печать…» (или «распечатай», «print», «gtxfnm» в чужой раскладке) -> print_file();
либо файл, а следом отдельное сообщение «печать» (в течение 10 мин). «печать 2-3» — только эти страницы. Без --go в CLI — только подготовка, JPEG для просмотра глазами.

CLI:  python doc_print.py "C:\\путь\\файл.docx" [--host 192.168.x.x] [--go] [--pages 1-2] [--draft]
"""
from __future__ import annotations

import argparse
import io
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import ipp_probe

OUT = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "oko" / "print_out"
DPI = 300
A4_PT = (595.0, 842.0)
MAX_PAGES = 30                       # защита от случайной печати толстого документа целиком
SUPPORTED = {".pdf", ".docx", ".xlsx"}
JOB_STATES = {3: "ожидает", 4: "удержано", 5: "печатается", 6: "остановлено", 7: "отменено", 8: "прервано",
              9: "напечатано"}
TRANSPORT_ERRORS = (urllib.error.URLError, TimeoutError, OSError, ValueError)

# Первое слово подписи/сообщения: «печать», «распечатай», «напечатай», «print»; «gtxfnm»/«hfcgtxfnfq» — то же
# в английской раскладке. Дальше — что угодно («печать.», «печать файла»); номера страниц ищутся во всём тексте.
# 28.10, живой замер: строгое «только слово» пропустило подпись Оператора — люди пишут свободно.
_PRINT_HEAD = re.compile(r"^\W*(печат|распечат|напечат|print|gtxfn|hfcgtxfn|yfgtxfn)\w*\b", re.IGNORECASE)
_PAGES = re.compile(r"\d+(?:\s*[-–]\s*\d+)?")


class PrintRefused(Exception):
    """Печать невозможна по понятной причине (нет принтера, формат, страницы) — текст для человека."""


def wants_print(caption: str | None) -> tuple[bool, str | None]:
    """Подпись к документу или отдельное сообщение -> (печатать?, диапазон страниц «1-2,4» или None)."""
    text = caption or ""
    m = _PRINT_HEAD.match(text)
    if not m:
        return False, None
    pages = [re.sub(r"\s+", "", g).replace("–", "-") for g in _PAGES.findall(text[m.end():])]
    return True, (",".join(pages) or None)


def printer_host() -> str | None:
    return os.getenv("OKO_PRINTER_HOST", "").strip() or None


def parse_pages(spec: str | None, total: int) -> list[int]:
    """'1-3,5' -> [0,1,2,4] (нумерация с 1, как в Word). None -> все."""
    if not spec:
        return list(range(total))
    out: list[int] = []
    for part in spec.split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        try:
            lo, hi = int(a), int(b or a)
        except ValueError:
            raise PrintRefused(f"не понял страницы «{part}»") from None
        if lo < 1 or hi > total or lo > hi:
            raise PrintRefused(f"страницы {part} вне 1–{total}")
        out.extend(range(lo - 1, hi))
    return sorted(set(out))


def _pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        import fitz
        return fitz


def to_pdf(src: Path, workdir: Path) -> Path:
    ext = src.suffix.lower()
    if ext == ".pdf":
        return src
    if ext not in SUPPORTED:
        raise PrintRefused(f"{src.suffix} не печатаю (умею .docx, .xlsx, .pdf)")
    import office_render
    import office_text
    workdir.mkdir(parents=True, exist_ok=True)
    dst = workdir / (src.stem + ".pdf")
    try:
        office_render.to_pdf(src, dst)
    except office_text.UnreadableOffice as e:
        raise PrintRefused(f"файл не читается: {e}") from e
    return dst


def page_jpegs(pdf: Path, pages: list[int] | None = None) -> list[bytes]:
    """Страница -> лист A4 300 dpi, оттенки серого; вписывается с сохранением пропорций, по центру;
    альбомная страница поворачивается (таблица не обрезается)."""
    from PIL import Image
    M = _pymupdf()
    W, H = round(A4_PT[0] / 72 * DPI), round(A4_PT[1] / 72 * DPI)
    out = []
    with M.open(str(pdf)) as doc:
        for i in (range(doc.page_count) if pages is None else pages):
            page = doc[i]
            if page.rect.width > page.rect.height:
                page.set_rotation((page.rotation + 90) % 360)
            r = page.rect
            zoom = min(W / r.width, H / r.height)
            pix = page.get_pixmap(matrix=M.Matrix(zoom, zoom), colorspace=M.csGRAY, alpha=False)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            sheet = Image.new("L", (W, H), 255)
            sheet.paste(img, ((W - pix.width) // 2, (H - pix.height) // 2))
            buf = io.BytesIO()
            sheet.save(buf, "JPEG", quality=90, dpi=(DPI, DPI))
            out.append(buf.getvalue())
    return out


# ---------- IPP ----------
def _a(tag: int, name: str, value) -> bytes:
    v = struct.pack(">i", value) if tag in (0x21, 0x23) else str(value).encode()
    n = name.encode()
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(v)) + v


def _op(op: int, uri: str, rid: int, extra: bytes = b"", job: bytes = b"") -> bytes:
    b = struct.pack(">BBHI", 2, 0, op, rid) + b"\x01"
    b += _a(0x47, "attributes-charset", "utf-8") + _a(0x48, "attributes-natural-language", "en")
    b += _a(0x45, "printer-uri", uri) + extra
    if job:
        b += b"\x02" + job
    return b + b"\x03"


def _http_post(url: str, body: bytes, timeout: float = 60.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/ipp"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ipp_probe.parse_response(r.read())


@dataclass
class PrintResult:
    ok: bool
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def print_file(src: str | Path, host: str | None = None, pages: str | None = None, draft: bool = False,
               go: bool = True, post: Callable[..., tuple[int, dict]] = _http_post,
               wait_s: float | None = None, workdir: Path | None = None,
               sleep: Callable[[float], None] = time.sleep) -> PrintResult:
    """Документ -> принтер. Не бросает на штатных отказах: ok=False и понятный текст."""
    src = Path(src)
    name = re.sub(r"^[0-9a-f]{8}__", "", src.name)      # служебный номер doc_intake человеку не показывается
    res = PrintResult(ok=False)
    host = host or printer_host()
    if not host:
        res.lines.append("🖨 Принтер не настроен: в .env нет OKO_PRINTER_HOST.")
        return res
    url, uri = f"http://{host}:631/ipp/print", f"ipp://{host}:631/ipp/print"
    workdir = workdir or OUT
    try:
        req = b"".join(_a(0x44, "requested-attributes" if i == 0 else "", w) for i, w in enumerate(
            ("document-format-supported", "print-color-mode-supported", "print-quality-supported",
             "printer-state", "printer-state-reasons")))
        try:
            _, at = post(url, _op(0x000B, uri, 1, req), timeout=10)
        except TRANSPORT_ERRORS as e:
            res.lines.append(f"🖨 Принтер {host} не отвечает ({type(e).__name__}). Включён? В сети?")
            return res
        if "image/jpeg" not in at.get("document-format-supported", []):
            raise PrintRefused(f"принтер не принимает JPEG: {at.get('document-format-supported')}")
        if (at.get("printer-state") or [None])[0] == 5:
            raise PrintRefused(f"принтер остановлен: {', '.join(map(str, at.get('printer-state-reasons', [])))}")

        pdf = to_pdf(src, workdir)
        M = _pymupdf()
        with M.open(str(pdf)) as d:
            total = d.page_count
        idx = parse_pages(pages, total)
        if len(idx) > MAX_PAGES:
            raise PrintRefused(f"{len(idx)} стр. — больше {MAX_PAGES}; укажи страницы: «печать 1-5»")
        jpegs = page_jpegs(pdf, idx)
        workdir.mkdir(parents=True, exist_ok=True)
        for i, j in zip(idx, jpegs):
            (workdir / f"{src.stem}_p{i + 1}.jpg").write_bytes(j)
        res.lines.append(f"🖨 {name}: {total} стр. в документе, печатаю {len(idx)} "
                         f"({', '.join(str(i + 1) for i in idx)}).")

        job = _a(0x21, "copies", 1)
        if "monochrome" in at.get("print-color-mode-supported", []):
            job += _a(0x44, "print-color-mode", "monochrome")
        if draft and 3 in at.get("print-quality-supported", []):
            job += _a(0x23, "print-quality", 3)
        if not go:
            res.lines.append(f"Проверка без печати: страницы-картинки в {workdir}")
            res.ok = True
            return res

        ids: dict[int, int] = {}
        for i, j in zip(idx, jpegs):
            extra = _a(0x42, "requesting-user-name", "oko") + _a(0x42, "job-name", f"{src.stem} p{i + 1}") + \
                _a(0x49, "document-format", "image/jpeg")
            try:
                st, r = post(url, _op(0x0002, uri, 10 + i, extra, job) + j, timeout=60)
            except TRANSPORT_ERRORS as e:
                res.lines.append(f"⚠️ стр. {i + 1}: связь с принтером оборвалась ({type(e).__name__}); "
                                 f"дальше не отправлял.")
                return res
            jid = (r.get("job-id") or [None])[0]
            if st >= 0x0100 or jid is None:
                res.lines.append(f"⚠️ стр. {i + 1}: принтер отказал (status 0x{st:04X}); дальше не отправлял.")
                return res
            ids[i] = jid

        deadline = time.monotonic() + (wait_s if wait_s is not None else 30 + 20 * len(ids))
        done: dict[int, int] = {}
        while len(done) < len(ids) and time.monotonic() < deadline:
            sleep(4)
            for i, jid in ids.items():
                if i in done:
                    continue
                try:
                    _, ja = post(url, _op(0x0009, uri, 500 + i, _a(0x21, "job-id", jid)), timeout=10)
                except TRANSPORT_ERRORS:
                    continue
                s = (ja.get("job-state") or [None])[0]
                if s in (7, 8, 9):
                    done[i] = s
        printed = [i for i, s in done.items() if s == 9]
        bad = {i: s for i, s in done.items() if s != 9}
        if len(printed) == len(ids):
            res.lines.append(f"✅ Напечатано: {len(printed)} стр.")
            res.ok = True
        else:
            for i, s in sorted(bad.items()):
                res.lines.append(f"⚠️ стр. {i + 1}: {JOB_STATES.get(s, s)}")
            waiting = [i + 1 for i in ids if i not in done]
            if waiting:
                res.lines.append(f"⏳ Принтер принял, но ещё не отчитался: стр. {', '.join(map(str, waiting))}.")
            res.ok = not bad
        return res
    except PrintRefused as e:
        res.lines.append(f"🖨 {name}: {e}")
        return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--host")
    ap.add_argument("--go", action="store_true", help="действительно печатать")
    ap.add_argument("--pages", help="например 1-2,4 (по умолчанию все)")
    ap.add_argument("--draft", action="store_true", help="черновое качество (меньше чернил)")
    a = ap.parse_args()
    src = Path(a.file).resolve()
    if not src.is_file():
        print(f"СТОП: нет файла {src}")
        return 2
    r = print_file(src, host=a.host, pages=a.pages, draft=a.draft, go=a.go)
    print(r.text)
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
