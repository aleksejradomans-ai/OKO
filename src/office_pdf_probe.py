"""office_pdf_probe.py — замер: Word/Excel -> PDF через установленный Office (COM), 28.10.2026.

Зачем: принтер берёт только JPEG. Путь печати документа: .docx/.xlsx -> PDF (вёрстку делает сам Office,
как при «Печать» из Word) -> страницы PDF в JPEG (PyMuPDF) -> IPP Print-Job. Этот скрипт мерит ПЕРВОЕ звено.
Красная зона: Microsoft не поддерживает автоматизацию Office из служб Windows. Поэтому замер в два шага:
  1) из консоли Оператора (интерактивный сеанс) — работает ли вообще;
  2) тем же скриптом из-под службы (отдельный блок) — работает ли там, где живёт бот.
Ничего не печатает. Исходный файл открывается ТОЛЬКО ДЛЯ ЧТЕНИЯ, PDF пишется в %LOCALAPPDATA%\\oko\\print_probe\\.
Office закрывается в finally; если процесс всё же завис — скрипт печатает это явно (по PID нового WINWORD/EXCEL).

Запуск:  python office_pdf_probe.py "C:\\путь\\файл.docx" ["C:\\путь\\таблица.xlsx" ...]
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

OUT = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "oko" / "print_probe"
WD_EXPORT_PDF = 17          # WdExportFormat.wdExportFormatPDF
XL_TYPE_PDF = 0             # XlFixedFormatType.xlTypePDF


def _pids(image: str) -> set[int]:
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[1].strip('"').isdigit():
            pids.add(int(parts[1].strip('"')))
    return pids


def word_to_pdf(src: Path, dst: Path) -> None:
    import win32com.client
    app = win32com.client.DispatchEx("Word.Application")
    doc = None
    try:
        app.Visible = False
        app.DisplayAlerts = 0
        doc = app.Documents.Open(str(src), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False)
        doc.ExportAsFixedFormat(str(dst), WD_EXPORT_PDF)
    finally:
        if doc is not None:
            doc.Close(0)            # wdDoNotSaveChanges
        app.Quit()


def excel_to_pdf(src: Path, dst: Path) -> None:
    import win32com.client
    app = win32com.client.DispatchEx("Excel.Application")
    wb = None
    try:
        app.Visible = False
        app.DisplayAlerts = False
        wb = app.Workbooks.Open(str(src), UpdateLinks=0, ReadOnly=True, AddToMru=False)
        wb.ExportAsFixedFormat(XL_TYPE_PDF, str(dst))
    finally:
        if wb is not None:
            wb.Close(False)
        app.Quit()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    try:
        import pythoncom
    except ImportError:
        print("СТОП: нет pywin32 (pythoncom) в этой среде")
        return 4
    pythoncom.CoInitialize()
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[сеанс] пользователь={os.environ.get('USERNAME')} сеанс={os.environ.get('SESSIONNAME', '—')} "
          f"python={sys.executable}")
    rc = 0
    try:
        for arg in sys.argv[1:]:
            src = Path(arg).resolve()
            ext = src.suffix.lower()
            if not src.is_file() or ext not in (".docx", ".xlsx"):
                print(f"[пропуск] {src}: нет файла или не .docx/.xlsx")
                rc = 1
                continue
            image, conv = ("WINWORD.EXE", word_to_pdf) if ext == ".docx" else ("EXCEL.EXE", excel_to_pdf)
            dst = OUT / (src.stem + ".pdf")
            if dst.exists():
                dst.unlink()
            before = _pids(image)
            t0 = time.monotonic()
            try:
                conv(src, dst)
                err = None
            except Exception as e:      # COM-ошибки разнотипны (pywintypes.com_error и др.)
                err = f"{type(e).__name__}: {e}"
            dt = time.monotonic() - t0
            time.sleep(2)
            hung = _pids(image) - before
            if err or not dst.exists():
                print(f"[ОШИБКА] {src.name}: {err or 'PDF не появился'}  ({dt:.1f} с)")
                rc = 1
            else:
                pages = "?"
                try:
                    import fitz
                    with fitz.open(dst) as pdf:
                        pages = pdf.page_count
                except Exception as e:
                    pages = f"не прочитан ({type(e).__name__})"
                print(f"[OK] {src.name} -> {dst}  {dst.stat().st_size} байт, страниц: {pages}, {dt:.1f} с")
            if hung:
                print(f"[ВНИМАНИЕ] после закрытия остался процесс {image}, PID {sorted(hung)} — Office завис")
                rc = 1
    finally:
        pythoncom.CoUninitialize()
    return rc


if __name__ == "__main__":
    sys.exit(main())
