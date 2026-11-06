"""office_render.py — Word/Excel -> PDF БЕЗ Microsoft Office (28.10.2026).

Решение Оператора: Око само читает и печатает документы, без Word/Excel на машине.
Office из-под службы (LocalSystem) измерен — не работает (Documents.Open -> None, Excel: нет доступа к файлу).
Поэтому вёрстку делает сама система: python-docx/openpyxl -> простой HTML -> PyMuPDF Story -> PDF A4.
Работает в среде службы (python-docx, openpyxl, pymupdf уже там), без COM, без активации, без профиля SYSTEM.

Что сохраняется: абзацы и таблицы в порядке документа, заголовки (стили Heading/Title/Заголовок),
жирный/курсив/подчёркнутый, выравнивание абзаца, разрывы страниц, колонтитулы (первая секция — сверху/снизу
текста), листы Excel как таблицы. Чего нет: картинки, точные шрифты/отступы/межстрочные интервалы Word,
объединённые ячейки отрисовываются одной ячейкой, формулы — последние сохранённые значения.
Это «чистая печать содержимого», не копия вёрстки Word.

Шрифт: нужен TTF с кириллицей и латышскими буквами (базовый Helvetica PDF их не содержит):
Windows — Arial из C:\\Windows\\Fonts, Linux — DejaVu Sans. Нет шрифта -> явная ошибка, не «квадратики».
"""
from __future__ import annotations

import html
import pathlib

import office_text

A4 = (595.0, 842.0)
MARGIN = 50.0                 # ~1,8 см
FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/ariali.ttf",
     "C:/Windows/Fonts/arialbi.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"),
]
MAX_XLSX_ROWS = 2000          # печать: больше — пометка (бумага, не индекс)
PAGE_BREAK = "\f"


def _pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        import fitz
        return fitz


def _fonts() -> tuple[str, str, str, str]:
    for cand in FONT_CANDIDATES:
        if all(pathlib.Path(p).is_file() for p in cand):
            return cand
    raise office_text.UnreadableOffice("нет TTF-шрифта с кириллицей (Arial/DejaVu) — печать невозможна")


CSS = """
@font-face {font-family: doc; src: url(r.ttf);}
@font-face {font-family: doc; src: url(b.ttf); font-weight: bold;}
@font-face {font-family: doc; src: url(i.ttf); font-style: italic;}
@font-face {font-family: doc; src: url(bi.ttf); font-weight: bold; font-style: italic;}
* {font-family: doc; font-size: 11pt;}
p {margin: 0 0 5pt 0;}
h1 {font-size: 16pt; margin: 6pt 0 6pt 0;}
h2 {font-size: 14pt; margin: 6pt 0 4pt 0;}
h3 {font-size: 12pt; margin: 4pt 0 3pt 0;}
table {border-collapse: collapse; margin: 3pt 0 6pt 0;}
td {border: 0.5pt solid black; padding: 2pt 4pt; font-size: 10pt; vertical-align: top;}
.hf {font-size: 9pt; color: #444444;}
.sheet {font-size: 13pt; font-weight: bold; margin: 4pt 0 4pt 0;}
"""


def _runs_html(par) -> str:
    out = []
    for r in par.runs:
        t = html.escape(r.text)
        if not t:
            continue
        if r.bold:
            t = f"<b>{t}</b>"
        if r.italic:
            t = f"<i>{t}</i>"
        if r.underline:
            t = f"<u>{t}</u>"
        out.append(t)
    s = "".join(out)
    return s if s.strip() else html.escape(par.text)


def _heading_level(par) -> int:
    name = (par.style.name if par.style is not None else "") or ""
    low = name.lower()
    if low == "title":
        return 1
    for key in ("heading", "заголовок"):
        if low.startswith(key):
            tail = low[len(key):].strip()
            return min(3, int(tail)) if tail.isdigit() else 1
    return 0


_ALIGN = {0: "left", 1: "center", 2: "right", 3: "justify"}


def docx_html(path: pathlib.Path) -> list[str]:
    """-> список HTML-страниц-фрагментов (разрыв страницы Word = новый фрагмент)."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    try:
        d = docx.Document(str(path))
    except Exception as e:
        raise office_text.UnreadableOffice(f"{type(e).__name__}: {e}") from e

    def hf(part) -> str:
        try:
            t = "<br>".join(html.escape(p.text.strip()) for p in part.paragraphs if p.text.strip())
        except Exception:
            t = ""
        return f'<p class="hf">{t}</p>' if t else ""

    sec = d.sections[0] if d.sections else None
    header = hf(sec.header) if sec is not None else ""
    footer = hf(sec.footer) if sec is not None else ""
    pages: list[list[str]] = [[header] if header else []]
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            par = Paragraph(child, d)
            breaks = child.xpath('.//w:br[@w:type="page"]')
            body = _runs_html(par)
            if body.strip():
                lvl = _heading_level(par)
                al = _ALIGN.get(par.alignment if isinstance(par.alignment, int) else
                                getattr(par.alignment, "value", None), "")
                style = f' style="text-align:{al}"' if al and al != "left" else ""
                pages[-1].append(f"<h{lvl}{style}>{body}</h{lvl}>" if lvl else f"<p{style}>{body}</p>")
            elif not breaks:
                pages[-1].append("<p>&#160;</p>")       # пустой абзац Word = вертикальный отступ
            if breaks:
                pages.append([])
        elif tag == "tbl":
            rows = []
            for row in Table(child, d).rows:
                cells, prev = [], None
                for c in row.cells:
                    if c._tc is prev:                    # объединённая ячейка — python-docx повторяет её
                        continue
                    prev = c._tc
                    cells.append("<td>" + "<br>".join(html.escape(p.text) for p in c.paragraphs) + "</td>")
                rows.append("<tr>" + "".join(cells) + "</tr>")
            pages[-1].append("<table>" + "".join(rows) + "</table>")
    if footer:
        pages[-1].append(footer)
    return ["".join(p) for p in pages if any(x.strip() for x in p)] or [""]


def xlsx_html(path: pathlib.Path) -> list[str]:
    """Каждый непустой лист — отдельный фрагмент (с новой страницы)."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        raise office_text.UnreadableOffice(f"{type(e).__name__}: {e}") from e
    out = []
    try:
        for ws in wb.worksheets:
            rows, width = [], 0
            for n, row in enumerate(ws.iter_rows(values_only=True), 1):
                if n > MAX_XLSX_ROWS:
                    rows.append(None)
                    break
                vals = ["" if v is None else str(v) for v in row]
                while vals and vals[-1] == "":
                    vals.pop()
                if any(vals):
                    rows.append(vals)
                    width = max(width, len(vals))
            if not rows:
                continue
            body = []
            for vals in rows:
                if vals is None:
                    body.append(f'<tr><td colspan="{width}">… лист обрезан: больше {MAX_XLSX_ROWS} строк</td></tr>')
                    continue
                vals = vals + [""] * (width - len(vals))
                body.append("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in vals) + "</tr>")
            out.append(f'<p class="sheet">Лист: {html.escape(ws.title)}</p><table>{"".join(body)}</table>')
    finally:
        wb.close()
    return out or [""]


def html_pages_to_pdf(fragments: list[str], dst: pathlib.Path) -> int:
    """Фрагменты -> PDF A4; каждый фрагмент начинается с новой страницы. -> число страниц."""
    M = _pymupdf()
    r, b, i, bi = _fonts()
    arch = M.Archive()
    for name, p in (("r.ttf", r), ("b.ttf", b), ("i.ttf", i), ("bi.ttf", bi)):
        arch.add(pathlib.Path(p).read_bytes(), name)
    mediabox = M.Rect(0, 0, *A4)
    where = mediabox + (MARGIN, MARGIN, -MARGIN, -MARGIN)
    writer = M.DocumentWriter(str(dst))
    pages = 0
    try:
        for frag in fragments:
            story = M.Story(html=f"<body>{frag or '&#160;'}</body>", user_css=CSS, archive=arch)
            more = True
            while more:
                dev = writer.begin_page(mediabox)
                more, _ = story.place(where)
                story.draw(dev)
                writer.end_page()
                pages += 1
    finally:
        writer.close()
    return pages


def to_pdf(src: str | pathlib.Path, dst: str | pathlib.Path) -> int:
    """Документ -> PDF без Office. -> число страниц. Ошибка формата -> office_text.UnreadableOffice."""
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    ext = src.suffix.lower()
    if ext not in office_text.SUPPORTED:
        raise office_text.UnreadableOffice(f"формат {ext} не поддерживается (нужен .docx или .xlsx)")
    import zipfile
    if not zipfile.is_zipfile(src):
        raise office_text.UnreadableOffice("не zip-контейнер Office Open XML")
    frags = docx_html(src) if ext == ".docx" else xlsx_html(src)
    return html_pages_to_pdf(frags, dst)
