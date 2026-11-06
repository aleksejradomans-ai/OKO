"""office_text.py — текст из Word (.docx) и Excel (.xlsx) для конвейера знаний Ока (28.10.2026).

Используется knowledge_indexer.process_single так же, как oko_rag/loader.load_document для PDF:
вход — путь к файлу, выход — обычный текст. Ничего не пишет и не меняет.

Word: абзацы и таблицы В ПОРЯДКЕ документа (не «сначала все абзацы, потом таблицы»: иначе подпись к таблице
оторвётся от таблицы), ячейки таблицы через « | »; плюс колонтитулы, если в них есть текст.
Excel: каждый лист отдельным блоком «## Лист: имя», строки — значения через табуляцию, пустые строки пропускаются;
формулы — их ПОСЛЕДНИЕ вычисленные значения (data_only=True), как их видит человек в Excel.
Старые двоичные .doc / .xls здесь не читаются — это другой формат (их нужно пересохранить в .docx / .xlsx).

Ошибка формата (не zip, повреждён, защищён паролем) -> UnreadableOffice: для индексатора это «нечитаемый вход»,
файл уходит в archived/rejected, как битый PDF, а не роняет обработку.
"""
from __future__ import annotations

import pathlib
import zipfile

SUPPORTED = {".docx", ".xlsx"}
MAX_XLSX_ROWS_PER_SHEET = 20000      # защита от «бесконечного» листа: дальше — пометка, не молчаливая обрезка


class UnreadableOffice(ValueError):
    """Файл Word/Excel не удаётся прочитать (повреждён, не тот формат, пароль)."""


def _docx_text(path: pathlib.Path) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        d = docx.Document(str(path))
    except Exception as e:  # PackageNotFoundError, KeyError, BadZipFile — всё это «не читается»
        raise UnreadableOffice(f"{type(e).__name__}: {e}") from e

    out: list[str] = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            t = Paragraph(child, d).text.strip()
            if t:
                out.append(t)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                cells = []
                for c in row.cells:
                    ct = " ".join(c.text.split())
                    if not cells or cells[-1] != ct:      # объединённые ячейки python-docx повторяет — убираем повтор
                        cells.append(ct)
                if any(cells):
                    out.append(" | ".join(cells))
    for i, sec in enumerate(d.sections):
        for part, label in ((sec.header, "Верхний колонтитул"), (sec.footer, "Нижний колонтитул")):
            try:
                t = "\n".join(p.text.strip() for p in part.paragraphs if p.text.strip())
            except Exception:
                t = ""
            if t:
                out.append(f"[{label} {i + 1}] {t}")
    return "\n".join(out)


def _xlsx_text(path: pathlib.Path) -> str:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:  # InvalidFileException, BadZipFile, KeyError
        raise UnreadableOffice(f"{type(e).__name__}: {e}") from e
    out: list[str] = []
    try:
        for ws in wb.worksheets:
            rows: list[str] = []
            for n, row in enumerate(ws.iter_rows(values_only=True), 1):
                if n > MAX_XLSX_ROWS_PER_SHEET:
                    rows.append(f"[… лист обрезан: больше {MAX_XLSX_ROWS_PER_SHEET} строк]")
                    break
                vals = ["" if v is None else str(v) for v in row]
                while vals and vals[-1] == "":
                    vals.pop()
                if any(vals):
                    rows.append("\t".join(vals))
            if rows:
                out.append(f"## Лист: {ws.title}")
                out.extend(rows)
    finally:
        wb.close()
    return "\n".join(out)


def extract(path: str | pathlib.Path) -> str:
    """Текст документа. Пустая строка — документ читается, но текста в нём нет (решает вызывающий код)."""
    p = pathlib.Path(path)
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise UnreadableOffice(f"формат {ext} не поддерживается (нужен .docx или .xlsx)")
    if not zipfile.is_zipfile(p):
        raise UnreadableOffice("не zip-контейнер Office Open XML (старый .doc/.xls с новым расширением или повреждён)")
    return _docx_text(p) if ext == ".docx" else _xlsx_text(p)
