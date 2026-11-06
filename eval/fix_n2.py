import ast, py_compile, shutil, sys
from pathlib import Path
p = Path(sys.argv[1])
raw = p.read_bytes(); bom = raw.startswith(b"\xef\xbb\xbf")
s = raw.decode("utf-8-sig"); nl = "\r\n" if "\r\n" in s else "\n"; s = s.replace("\r\n", "\n")
MARK = "    # --- Game watcher candidate approval (games.txt) ---\n"
END = "    # --- Approve / Reject ---\n"
if s.count(MARK) != 2:
    print(f"[skip/ABORT] блоков game watcher: {s.count(MARK)} (ожидалось 2)"); sys.exit(0 if s.count(MARK) == 1 else 1)
a = s.index(MARK); b = s.index(MARK, a + 1); e = s.index(END, b)
code = lambda t: ast.dump(ast.parse("if 1:\n" + t))
if code(s[a:b]) != code(s[b:e]):
    print("[ABORT] блоки различаются по коду - удалять вслепую нельзя"); sys.exit(1)
removed = s[b:e].count("\n")
s = s[:b] + s[e:]
ast.parse(s)
shutil.copy2(p, p.with_suffix(".py.pren2"))
p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + s.replace("\n", nl).encode("utf-8"))
py_compile.compile(str(p), doraise=True)
print(f"[DONE] удалён дубликат ({removed} строк), код блоков идентичен")