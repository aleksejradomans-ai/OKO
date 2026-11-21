"""weakening_guard.py -- сторож ослабления тестов: снимок сравнивается с базой (родитель снимка), по AST.

Тестовый файл -- test_*.py или *_test.py вне tools/archive/. Находки (каждая -- красный, если нет строки в списке
разрешений):
  deleted_file      -- тестовый файл базы исчез (тот же blob под другим именем -- переименование, не находка);
  removed_test      -- функция test_* (в т.ч. метод класса) базы исчезла и нет функции с тем же именем ни в одном
                       тестовом файле снимка (перенос между файлами -- не находка);
  fewer_asserts     -- в функции стало меньше проверок: assert, вызовы self.assert* / *.assert_*, pytest.raises /
                       pytest.warns / pytest.fail;
  new_skip          -- в функции (или на уровне файла: pytestmark, importorskip) стало больше пропусков:
                       pytest.mark.skip / skipif / xfail, pytest.skip / xfail / importorskip, unittest.skip*;
  wider_tolerance   -- допуск шире: rel= / abs= у pytest.approx, rel_tol= / abs_tol= у math.isclose, timeout= у вызовов
                       и pytest.mark.timeout(N) -- максимум числовых констант функции вырос (новый допуск в функции без
                       допусков -- новая проверка, не ослабление).
Ключ находки -- `<вид>:<путь>::<функция>` (на уровне файла -- `::<module>`). Список разрешений
(.github/test_weakening_allow.txt): строка = `<ключ> | <причина> | <YYYY-MM-DD> | <код согласия Q1>`; '#' --
комментарий; плохая строка -- ошибка (красный). Только стандартная библиотека."""
from __future__ import annotations

import ast
import datetime as dt
import re
from typing import Iterable, Optional

KINDS = ("deleted_file", "removed_test", "fewer_asserts", "new_skip", "wider_tolerance")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_KEY_RE = re.compile(r"^(" + "|".join(KINDS) + r"):[^\s:|][^\s|]*::[A-Za-z_<][A-Za-z0-9_.<>]*$")
SKIP_ATTRS = {("mark", "skip"), ("mark", "skipif"), ("mark", "xfail"), ("pytest", "skip"), ("pytest", "xfail"),
              ("pytest", "importorskip"), ("unittest", "skip"), ("unittest", "skipIf"), ("unittest", "skipUnless"),
              ("unittest", "expectedFailure")}
ASSERT_CALLS = {("pytest", "raises"), ("pytest", "warns"), ("pytest", "fail"), ("pytest", "deprecated_call")}
TOL_KW = {"approx": ("rel", "abs"), "isclose": ("rel_tol", "abs_tol")}


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    if p.startswith("tools/archive/") or "/tools/archive/" in p:
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


def _num(n) -> Optional[float]:
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
        return float(n.value)
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
        v = _num(n.operand)
        return None if v is None else -v
    if isinstance(n, ast.BinOp):                                    # 1e-6 * 10, 2 ** -10 ...
        a, b = _num(n.left), _num(n.right)
        if a is None or b is None:
            return None
        try:
            return float(eval(compile(ast.Expression(n), "<c>", "eval"), {"__builtins__": {}}, {}))  # noqa: S307 (константы)
        except Exception:
            return None
    return None


class _Stats:
    __slots__ = ("asserts", "skips", "tol")

    def __init__(self):
        self.asserts = 0
        self.skips = 0
        self.tol = {}

    def add_tol(self, kind: str, v: float):
        self.tol[kind] = max(self.tol.get(kind, v), v)


def _scan_node(n, st: _Stats) -> None:
    """Счёт проверок / пропусков / допусков внутри узла (без вложенных функций -- у них своя статистика)."""
    for x in ast.walk(n):
        if isinstance(x, ast.Assert):
            st.asserts += 1
        elif isinstance(x, ast.Call):
            c = _chain(x.func)
            tail = tuple(c[-2:]) if len(c) >= 2 else tuple(c)
            last = c[-1] if c else ""
            if tail in ASSERT_CALLS or (len(c) >= 2 and (last.startswith("assert") or last.startswith("assert_"))):
                st.asserts += 1
            if tail in SKIP_ATTRS or (len(c) >= 2 and tuple(c[-2:]) in SKIP_ATTRS):
                st.skips += 1
            if last in TOL_KW:
                for kw in x.keywords:
                    if kw.arg in TOL_KW[last]:
                        v = _num(kw.value)
                        if v is not None:
                            st.add_tol(f"{last}.{kw.arg}", v)
            for kw in x.keywords:
                if kw.arg == "timeout":
                    v = _num(kw.value)
                    if v is not None:
                        st.add_tol("timeout", v)
            if tail == ("mark", "timeout") and x.args:
                v = _num(x.args[0])
                if v is not None:
                    st.add_tol("timeout", v)


def _skip_decorators(fn) -> int:
    k = 0
    for d in fn.decorator_list:
        c = _chain(d.func if isinstance(d, ast.Call) else d)
        if len(c) >= 2 and tuple(c[-2:]) in SKIP_ATTRS:
            k += 1
    return k


def analyze(source: str) -> dict:
    """Исходник тестового файла -> {qualname: {asserts, skips, tol}}; '<module>' -- уровень файла
    (pytestmark, importorskip вне функций). SyntaxError -> {'<syntax_error>': ...}."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"<syntax_error>": {"asserts": 0, "skips": 0, "tol": {}}}
    out = {}
    mod = _Stats()

    def fn_stats(fn) -> _Stats:
        st = _Stats()
        body = ast.Module(body=list(fn.body), type_ignores=[])
        _scan_node(body, st)
        st.skips += _skip_decorators(fn)
        for d in fn.decorator_list:                          # @pytest.mark.timeout(N) на функции
            if isinstance(d, ast.Call) and tuple(_chain(d.func)[-2:]) == ("mark", "timeout") and d.args:
                v = _num(d.args[0])
                if v is not None:
                    st.add_tol("timeout", v)
        return st

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                st = fn_stats(node)
                out[node.name] = {"asserts": st.asserts, "skips": st.skips, "tol": st.tol}
        elif isinstance(node, ast.ClassDef):
            cls_skips = _skip_decorators(node)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                    st = fn_stats(sub)
                    out[f"{node.name}.{sub.name}"] = {"asserts": st.asserts, "skips": st.skips + cls_skips, "tol": st.tol}
        else:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                tg = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in tg):
                    v = node.value
                    elts = v.elts if isinstance(v, (ast.List, ast.Tuple)) else [v]
                    for e in elts:
                        c = _chain(e.func if isinstance(e, ast.Call) else e)
                        if len(c) >= 2 and tuple(c[-2:]) in SKIP_ATTRS:
                            mod.skips += 1
                    continue
            st = _Stats()
            _scan_node(node, st)
            mod.skips += st.skips
    out["<module>"] = {"asserts": 0, "skips": mod.skips, "tol": {}}
    return out


def compare(base: dict, head: dict) -> list:
    """base / head: {path: (blob_id, source)} тестовых файлов -> отсортированный список ключей находок."""
    found = []
    head_blobs = {b for b, _ in head.values()}
    head_an = {p: analyze(s) for p, (_, s) in head.items()}
    head_names = {q.rsplit(".", 1)[-1] for an in head_an.values() for q in an if not q.startswith("<")}
    for path, (blob, src) in sorted(base.items()):
        if path not in head:
            if blob not in head_blobs:
                found.append(f"deleted_file:{path}::<module>")
            continue
        if head[path][0] == blob:
            continue
        a, b = analyze(src), head_an[path]
        for q, s in a.items():
            if q == "<syntax_error>":
                continue
            if q not in b:
                if q != "<module>" and q.rsplit(".", 1)[-1] not in head_names:
                    found.append(f"removed_test:{path}::{q}")
                continue
            t = b[q]
            if t["asserts"] < s["asserts"]:
                found.append(f"fewer_asserts:{path}::{q}")
            if t["skips"] > s["skips"]:
                found.append(f"new_skip:{path}::{q}")
            if any(k in s["tol"] and v > s["tol"][k] for k, v in t["tol"].items()):
                found.append(f"wider_tolerance:{path}::{q}")
        if b.get("<module>", {}).get("skips", 0) > a.get("<module>", {}).get("skips", 0) and \
                f"new_skip:{path}::<module>" not in found:
            found.append(f"new_skip:{path}::<module>")
        if "<syntax_error>" in b and "<syntax_error>" not in a:
            found.append(f"removed_test:{path}::<module>")
    return sorted(set(found))


def parse_allow(lines: Iterable[str]) -> tuple:
    """Строки списка разрешений -> ({ключ: (причина, дата, код)}, [ошибки 'line N: ...'])."""
    allow, errs = {}, []
    for i, raw in enumerate(lines, start=1):
        ln = str(raw).strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [x.strip() for x in ln.split("|")]
        if len(parts) != 4:
            errs.append(f"line {i}: 4 поля через '|': ключ | причина | дата | код согласия")
            continue
        key, why, day, code = parts
        if not _KEY_RE.match(key):
            errs.append(f"line {i}: ключ вида <вид>:<путь>::<функция>")
            continue
        if not why:
            errs.append(f"line {i}: пустая причина")
            continue
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            errs.append(f"line {i}: дата YYYY-MM-DD")
            continue
        if not _CODE_RE.match(code):
            errs.append(f"line {i}: код согласия [a-z][a-z0-9_]")
            continue
        allow[key] = (why, day, code)
    return allow, errs


def judge(findings: list, allow: dict) -> dict:
    """-> {ok, red: [ключи без разрешения], allowed: [...], unused_allow: [...]}."""
    red = [f for f in findings if f not in allow]
    return {"ok": not red, "red": red, "allowed": [f for f in findings if f in allow],
            "unused_allow": sorted(k for k in allow if k not in findings)}
