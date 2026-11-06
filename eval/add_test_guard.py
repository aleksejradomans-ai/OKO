import ast, sys
from pathlib import Path
GUARD = [
    "",
    "# --- Защита боевых данных ---",
    "# Тесты НИКОГДА не должны писать в рабочий BASE_DIR (и тем более удалять файлы",
    "# в нём). config.py вызывает load_dotenv(), который не перезаписывает уже",
    "# заданные переменные, а conftest загружается раньше любого тестового модуля.",
    "import os as _guard_os, tempfile as _guard_tempfile",
    "_guard_os.environ['AI_BASE_DIR'] = _guard_tempfile.mkdtemp(prefix='oko_pytest_')",
    "",
]
for name in sys.argv[1:]:
    p = Path(name)
    raw = p.read_bytes(); bom = raw.startswith(b"\xef\xbb\xbf")
    s = raw.decode("utf-8-sig"); nl = "\r\n" if "\r\n" in s else "\n"
    lines = s.replace("\r\n", "\n").split("\n")
    if any("_guard_os.environ" in l for l in lines):
        print(f"{p}: уже есть"); continue
    tree = ast.parse("\n".join(lines))
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    at = imports[-1].end_lineno if imports else 0
    new = lines[:at] + GUARD + lines[at:]
    ast.parse("\n".join(new))
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + nl.join(new).encode("utf-8"))
    print(f"{p}: защита после строки {at}")