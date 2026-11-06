import ast, py_compile
from pathlib import Path
p = Path(r"C:\oko\src\test_bot.py")
raw = p.read_bytes(); bom = raw.startswith(b"\xef\xbb\xbf")
s = raw.decode("utf-8-sig"); nl = "\r\n" if "\r\n" in s else "\n"; s = s.replace("\r\n", "\n")
OLD = "    bot.setup_sessions_table()\n"
if s.count(OLD) != 1:
    raise SystemExit(f"[ABORT] вызов найден {s.count(OLD)} раз (ожидался 1)")
s = s.replace(OLD, "    # R1: таблица sessions удалена; тестам нужен только существующий файл БД.\n"
                   "    import sqlite3\n"
                   "    sqlite3.connect(db_path).close()\n")
s = s.replace("через setup_sessions_table", "в фикстуре isolated_db")
ast.parse(s)
p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + s.replace("\n", nl).encode("utf-8"))
py_compile.compile(str(p), doraise=True)
print("test_bot.py: ok")