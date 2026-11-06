"""Тесты печати без Office (28.10.2026): office_render (docx/xlsx -> PDF) и doc_print (PDF -> JPEG -> IPP).
Принтер подменён: проверяется, ЧТО уходит на принтер (одно задание на страницу, monochrome, JPEG), и честные
отказы (нет принтера, не берёт JPEG, остановлен, страницы вне диапазона, мусор вместо документа)."""
import io
import struct

import pytest

docx = pytest.importorskip("docx")
openpyxl = pytest.importorskip("openpyxl")
pytest.importorskip("PIL")
try:
    import pymupdf as M
except ImportError:
    M = pytest.importorskip("fitz")

import doc_print as D
import office_render as R
import office_text


def _txt(page) -> str:
    """Текст страницы; неразрывный пробел -> обычный (Arial в Windows отдаёт пробел как U+00A0 — замер 28.10)."""
    return page.get_text().replace("\xa0", " ")


def _font_or_skip():
    try:
        R._fonts()
    except office_text.UnreadableOffice:
        pytest.skip("нет TTF-шрифта с кириллицей в этой среде")


# ---------- office_render ----------
def test_docx_to_pdf_text_styles_table_pagebreak(tmp_path):
    _font_or_skip()
    from docx.enum.text import WD_BREAK
    d = docx.Document()
    d.add_heading("Заявление", level=1)
    p = d.add_paragraph()
    p.add_run("Valsts ieņēmumu ").bold = True
    p.add_run("dienests")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Имя", "Сумма"
    d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    d.add_paragraph("Вторая страница")
    src, dst = tmp_path / "a.docx", tmp_path / "a.pdf"
    d.save(src)
    assert R.to_pdf(src, dst) == 2
    with M.open(str(dst)) as pdf:
        p1, p2 = _txt(pdf[0]), _txt(pdf[1])
    assert "Заявление" in p1 and "Valsts ieņēmumu" in p1 and "Сумма" in p1
    assert "Вторая страница" in p2 and "Вторая" not in p1


def test_xlsx_each_sheet_new_page(tmp_path):
    _font_or_skip()
    wb = openpyxl.Workbook()
    wb.active.title = "Расходы"
    wb.active.append(["Дата", "Сумма"])
    wb.create_sheet("Второй").append(["x", 1])
    wb.create_sheet("Пустой")
    src, dst = tmp_path / "t.xlsx", tmp_path / "t.pdf"
    wb.save(src)
    assert R.to_pdf(src, dst) == 2
    with M.open(str(dst)) as pdf:
        assert "Лист: Расходы" in _txt(pdf[0]) and "Лист: Второй" in _txt(pdf[1])


def test_render_garbage_is_unreadable(tmp_path):
    p = tmp_path / "bad.docx"
    p.write_bytes(b"not a zip")
    with pytest.raises(office_text.UnreadableOffice):
        R.to_pdf(p, tmp_path / "x.pdf")


# ---------- подпись к документу ----------
@pytest.mark.parametrize("cap,want", [
    ("печать", (True, None)), ("Распечатай", (True, None)), ("print 2-3", (True, "2-3")),
    ("печать 1, 3", (True, "1,3")), ("gtxfnm", (True, None)), ("напечатай 2", (True, "2")),
    ("Печать.", (True, None)), ("печать файла", (True, None)), ("распечатай страницы 2 – 4", (True, "2-4")),
    ("  печать!", (True, None)),
    (None, (False, None)), ("", (False, None)), ("сохрани в базу", (False, None)),
    ("не печатай", (False, None)), ("что такое печать", (False, None)),
])
def test_wants_print(cap, want):
    assert D.wants_print(cap) == want


def test_parse_pages():
    assert D.parse_pages(None, 3) == [0, 1, 2]
    assert D.parse_pages("3,1-2", 3) == [0, 1, 2]
    for bad in ("0", "4", "2-1", "x"):
        with pytest.raises(D.PrintRefused):
            D.parse_pages(bad, 3)


def test_landscape_page_fits_a4(tmp_path):
    pdf = M.open()
    pdf.new_page(width=842, height=595).insert_text((72, 72), "wide")
    p = tmp_path / "l.pdf"
    pdf.save(str(p))
    from PIL import Image
    (j,) = D.page_jpegs(p)
    assert Image.open(io.BytesIO(j)).size == (round(595 / 72 * D.DPI), round(842 / 72 * D.DPI))  # лист A4 книжный


# ---------- IPP (принтер подменён) ----------
class FakePrinter:
    def __init__(self, formats=("image/jpeg",), state=3, finals=9, color=("monochrome", "color")):
        self.formats, self.state, self.finals, self.color = list(formats), state, finals, list(color)
        self.jobs: list[bytes] = []
        self.next_id = 100

    def __call__(self, url, body, timeout=60):
        op = struct.unpack(">H", body[2:4])[0]
        if op == 0x000B:
            return 0, {"document-format-supported": self.formats, "print-color-mode-supported": self.color,
                       "print-quality-supported": [3, 4, 5], "printer-state": [self.state],
                       "printer-state-reasons": ["none"]}
        if op == 0x0002:
            self.jobs.append(body)
            self.next_id += 1
            return 0, {"job-id": [self.next_id], "job-state": [3]}
        if op == 0x0009:
            return 0, {"job-state": [self.finals]}
        raise AssertionError(op)


def _pdf(tmp_path, n):
    pdf = M.open()
    for k in range(n):
        pdf.new_page().insert_text((72, 72), f"page {k + 1}")
    p = tmp_path / "doc.pdf"
    pdf.save(str(p))
    return p


def test_print_one_job_per_page_monochrome_jpeg(tmp_path):
    fp = FakePrinter()
    r = D.print_file(_pdf(tmp_path, 3), host="10.0.0.9", pages="1,3", post=fp, sleep=lambda s: None,
                     workdir=tmp_path / "out")
    assert r.ok and "Напечатано: 2 стр." in r.text
    assert len(fp.jobs) == 2
    for body in fp.jobs:
        assert b"print-color-mode" in body and b"monochrome" in body and b"image/jpeg" in body
        assert b"\xff\xd8\xff" in body                     # сам JPEG в теле задания
    assert sorted(x.name for x in (tmp_path / "out").glob("*.jpg")) == ["doc_p1.jpg", "doc_p3.jpg"]


def test_print_no_host_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("OKO_PRINTER_HOST", raising=False)
    r = D.print_file(_pdf(tmp_path, 1), post=FakePrinter())
    assert not r.ok and "OKO_PRINTER_HOST" in r.text


def test_printer_without_jpeg_or_stopped_refuses(tmp_path):
    for fp, word in ((FakePrinter(formats=("application/pdf",)), "JPEG"), (FakePrinter(state=5), "остановлен")):
        r = D.print_file(_pdf(tmp_path, 1), host="h", post=fp, sleep=lambda s: None, workdir=tmp_path / "o")
        assert not r.ok and word in r.text and fp.jobs == []


def test_printer_unreachable_is_transport_message(tmp_path):
    def dead(*a, **k):
        raise TimeoutError("timed out")
    r = D.print_file(_pdf(tmp_path, 1), host="h", post=dead)
    assert not r.ok and "не отвечает" in r.text


def test_aborted_job_reported(tmp_path):
    r = D.print_file(_pdf(tmp_path, 1), host="h", post=FakePrinter(finals=8), sleep=lambda s: None,
                     workdir=tmp_path / "o")
    assert not r.ok and "прервано" in r.text


def test_dry_run_sends_nothing(tmp_path):
    fp = FakePrinter()
    r = D.print_file(_pdf(tmp_path, 2), host="h", post=fp, go=False, workdir=tmp_path / "o")
    assert r.ok and fp.jobs == [] and "без печати" in r.text


def test_too_many_pages_refused(tmp_path):
    fp = FakePrinter()
    r = D.print_file(_pdf(tmp_path, D.MAX_PAGES + 1), host="h", post=fp, workdir=tmp_path / "o")
    assert not r.ok and "печать 1-5" in r.text and fp.jobs == []


def test_garbage_docx_refused_not_crash(tmp_path):
    p = tmp_path / "fake.docx"
    p.write_bytes(b"zzz")
    fp = FakePrinter()
    r = D.print_file(p, host="h", post=fp, workdir=tmp_path / "o")
    assert not r.ok and "не читается" in r.text and fp.jobs == []


def test_report_hides_intake_id(tmp_path):
    p = _pdf(tmp_path, 1).rename(tmp_path / "48b9c841__Отчёт.pdf")
    r = D.print_file(p, host="h", post=FakePrinter(), sleep=lambda s: None, workdir=tmp_path / "o")
    assert r.ok and "Отчёт.pdf" in r.text and "48b9c841" not in r.text
