"""clock_guard.py -- сторож живых часов в тестах (AST): тест, читающий живые часы без заморозки, -- красный.

Живые часы: datetime.now / today / utcnow, date.today, time.time / time_ns / monotonic / monotonic_ns / perf_counter
(в т.ч. через псевдонимы импорта: `import datetime as dt`, `from time import time as now`, `from datetime import
datetime`). Вызов считается замороженным, если функция теста (или её модуль-фикстура) подменяет часы:
monkeypatch.setattr(...) / mock.patch(...) с целью из имён часов ('time', 'now', 'today', 'monotonic', ...,
'time.time', 'datetime.datetime' ...) или использует фикстуру заморозки FREEZE_FIXTURES.
Единица учёта -- `<путь>::<функция>` (вне функций -- `::<module>`) и число вызовов. Исключения -- список
(.github/live_clock_allow.txt): строка `<путь>::<функция> | <число вызовов> | <причина> | <YYYY-MM-DD>`; вызовов
стало больше, чем в строке, -- красный; строка без нарушения (вызов убран или заморожен) -- устаревшая (красный:
список держится точным); строка для файла, которого нет в дереве (приватный файл вне публичного среза), --
не учитывается. Только стандартная библиотека."""
from __future__ import annotations

import ast
import datetime as dt
import re
from pathlib import Path
from typing import Iterable, Optional

CLOCKS = {("datetime", "now"), ("datetime", "today"), ("datetime", "utcnow"), ("date", "today"), ("time", "time"),
          ("time", "time_ns"), ("time", "monotonic"), ("time", "monotonic_ns"), ("time", "perf_counter")}
CLOCK_NAMES = {"time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "now", "today", "utcnow", "datetime",
               "date"}
FREEZE_FIXTURES = {"frozen_clock", "freezer", "fixed_now", "fake_clock"}
SKIP_DIRS = ("tools/archive/", ".git/", "eval/")
_KEY_RE = re.compile(r"^[^\s|:][^\s|]*::[A-Za-z_<][A-Za-z0-9_.<>]*$")


def is_test_path(rel: str) -> bool:
    p = rel.replace("\\", "/")
    if any(p.startswith(d) or f"/{d}" in p for d in SKIP_DIRS) or "__pycache__" in p:
        return False
    base = p.rsplit("/", 1)[-1]
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


def _chain(n) -> list:
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
    return list(reversed(parts))


def _aliases(tree) -> dict:
    """Имя в модуле -> каноническая цепочка: dt -> datetime; now -> time.time; datetime -> datetime.datetime."""
    al = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in ("time", "datetime"):
                    al[a.asname or a.name] = [a.name]
        elif isinstance(n, ast.ImportFrom) and n.module in ("time", "datetime"):
            for a in n.names:
                al[a.asname or a.name] = [n.module, a.name]
    return al


def _is_clock(call: ast.Call, al: dict) -> bool:
    c = _chain(call.func)
    if not c:
        return False
    if c[0] in al:
        c = al[c[0]] + c[1:]
    return len(c) >= 2 and (c[-2], c[-1]) in CLOCKS


def _freezes(fn) -> bool:
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    if params & FREEZE_FIXTURES:
        return True
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            c = _chain(n.func)
            if c and c[-1] in ("setattr", "patch", "object") and ("monkeypatch" in c or "mock" in c or "patch" in c
                                                                     or "mp" in c):
                for a in list(n.args[:2]) + [k.value for k in n.keywords]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        tail = a.value.rsplit(".", 1)[-1]
                        if tail in CLOCK_NAMES:
                            return True
    return False


def scan_source(src: str) -> dict:
    """Исходник -> {функция | '<module>': число незамороженных вызовов живых часов}."""
    tree = ast.parse(src)
    al = _aliases(tree)
    out = {}

    def visit(node, fn_name: Optional[str], frozen: bool):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = ch.name if fn_name is None else f"{fn_name}.{ch.name}"
                visit(ch, name, frozen or _freezes(ch))
            elif isinstance(ch, ast.ClassDef):
                visit(ch, ch.name if fn_name is None else f"{fn_name}.{ch.name}", frozen)
            else:
                if isinstance(ch, ast.Call) and _is_clock(ch, al) and not frozen:
                    k = fn_name or "<module>"
                    out[k] = out.get(k, 0) + 1
                visit(ch, fn_name, frozen)

    visit(tree, None, False)
    return out


SCAN_DIRS = ("src", "tools", "scripts")      # тестовые каталоги проекта + тестовые файлы корня (не обмен / данные)


def _candidates(root: Path):
    yield from sorted(p for p in root.glob("*.py") if p.is_file())
    for d in SCAN_DIRS:
        if (root / d).is_dir():
            yield from sorted((root / d).rglob("*.py"))


def scan_tree(root: Path) -> dict:
    """{'<путь>::<функция>': число} по тестовым файлам корня и каталогов SCAN_DIRS."""
    root = Path(root)
    res = {}
    for p in _candidates(root):
        rel = p.relative_to(root).as_posix()
        if not is_test_path(rel):
            continue
        try:
            found = scan_source(p.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            res[f"{rel}::<syntax_error>"] = 1
            continue
        for fn, n in found.items():
            res[f"{rel}::{fn}"] = n
    return res


def parse_allow(lines: Iterable[str]) -> tuple:
    """-> ({ключ: (число, причина, дата)}, [ошибки])."""
    allow, errs = {}, []
    for i, raw in enumerate(lines, start=1):
        ln = str(raw).strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [x.strip() for x in ln.split("|")]
        if len(parts) != 4:
            errs.append(f"line {i}: 4 поля: <путь>::<функция> | <число> | <причина> | <дата>")
            continue
        key, n, why, day = parts
        if not _KEY_RE.match(key) or not n.isdigit() or int(n) < 1 or not why:
            errs.append(f"line {i}: ключ / число >= 1 / причина")
            continue
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            errs.append(f"line {i}: дата YYYY-MM-DD")
            continue
        if key in allow:
            errs.append(f"line {i}: повтор ключа")
            continue
        allow[key] = (int(n), why, day)
    return allow, errs


def judge(found: dict, allow: dict, root: Optional[Path] = None) -> dict:
    """-> {ok, red: [(ключ, найдено, разрешено)], stale: [ключ], allowed: n}. Строка для отсутствующего файла
    (root задан) -- не учитывается."""
    red, stale, ok_n = [], [], 0
    for k, n in sorted(found.items()):
        lim = allow.get(k, (0,))[0]
        if n > lim:
            red.append((k, n, lim))
        else:
            ok_n += 1
    for k, (n, _w, _d) in sorted(allow.items()):
        if k in found:
            continue
        path = k.split("::", 1)[0]
        if root is not None and not (Path(root) / path).is_file():
            continue
        stale.append(k)
    return {"ok": not red and not stale, "red": red, "stale": stale, "allowed": ok_n}
