"""Тесты office_text (28.10.2026): Word/Excel -> текст, порядок абзацев и таблиц, честный отказ на мусоре."""
import pytest

docx = pytest.importorskip("docx")
openpyxl = pytest.importorskip("openpyxl")

import office_text as O


def _make_docx(path):
    d = docx.Document()
    d.sections[0].header.paragraphs[0].text = "Шапка документа"
    d.add_paragraph("Заголовок перед таблицей")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Имя", "Сумма"
    t.cell(1, 0).text, t.cell(1, 1).text = "Andris", "120"
    d.add_paragraph("Текст после таблицы — Latviešu: ņēū")
    d.save(path)


def test_docx_order_tables_and_header(tmp_path):
    p = tmp_path / "a.docx"
    _make_docx(p)
    lines = O.extract(p).splitlines()
    assert lines[:4] == ["Заголовок перед таблицей", "Имя | Сумма", "Andris | 120",
                         "Текст после таблицы — Latviešu: ņēū"]
    assert "[Верхний колонтитул 1] Шапка документа" in lines


def test_docx_merged_cells_not_duplicated(tmp_path):
    d = docx.Document()
    t = d.add_table(rows=1, cols=3)
    a = t.cell(0, 0).merge(t.cell(0, 1))
    a.text = "Объединено"
    t.cell(0, 2).text = "Отдельно"
    p = tmp_path / "m.docx"
    d.save(p)
    assert O.extract(p).splitlines()[0] == "Объединено | Отдельно"


def test_xlsx_sheets_rows_and_empty_rows_skipped(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Расходы"
    ws.append(["Дата", "Сумма", None])
    ws.append([None, None, None])
    ws.append(["28.10", 15.5])
    wb.create_sheet("Пустой")
    p = tmp_path / "b.xlsx"
    wb.save(p)
    assert O.extract(p) == "## Лист: Расходы\nДата\tСумма\n28.10\t15.5"


@pytest.mark.parametrize("name,data", [
    ("bad.docx", b"this is not a zip"),
    ("old.xlsx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100),   # заголовок старого двоичного формата
])
def test_garbage_is_unreadable_not_crash(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    with pytest.raises(O.UnreadableOffice):
        O.extract(p)


def test_zip_but_not_office(tmp_path):
    import zipfile
    p = tmp_path / "z.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(O.UnreadableOffice):
        O.extract(p)


def test_unsupported_extension(tmp_path):
    p = tmp_path / "a.doc"
    p.write_bytes(b"x")
    with pytest.raises(O.UnreadableOffice):
        O.extract(p)
