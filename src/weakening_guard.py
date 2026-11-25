"""weakening_guard.py -- сторож ослабления тестов: снимок сравнивается с базой (в CI -- опубликованный коммит, B33 v2 C3), по AST.

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
  B33 H2.1: `assert` с константой (`assert True`, `assert 1`, `assert "x"`, `assert x or True`, непустой кортеж)
  проверкой не считается -- замена проверки на такую = fewer_asserts.
Ключ находки -- `<вид>:<путь>::<функция>` (на уровне файла -- `::<module>`).
  B33 v2 C4 (B32m D2): removed_test и по СОБРАННЫМ node ID (pytest --collect-only базы и снимка, compare_cases):
  исчезнувший вариант parametrize -- removed_test:<путь>::<функция[параметры]>.
  B33 v2 попытка 2 (ревизор G2): node ID обходится подменой значения при том же id (`"111"` ->
  `pytest.param("abc", id="111")`), поэтому по AST сравниваются и ЗНАЧЕНИЯ parametrize:
  changed_param     -- у функции (или её класса) из @pytest.mark.parametrize исчезло значение базы (литерал элемента;
                       у pytest.param -- позиционные аргументы и marks=, без id=); списки по имени модуля и их
                       сложение раскрываются, иное выражение сравнивается текстом целиком.

Охраняемая зона (B33 H2.2 / H2.3; любой файл, не только тестовый): правка без строки согласия -- красный.
  guarded_change    -- файл зоны добавлен, изменён или удалён: conftest.py (любой), pytest.ini, docs/control_map.csv,
                       config/net_direct_registry.json, .github/workflows/*, src/weakening_guard.py,
                       tools/weakening_guard.py, tools/verify_control_map.py, src/test_ci_gate.py,
                       tools/guard_mutation_smoke.py;
  map_test_removed  -- из колонки tests строки docs/control_map.csv исчез node ID (или строка целиком);
  map_new_known_gap -- у строки карты появился known_gap (было пусто или строки не было);
  (B33 v2: + tools/impl_mutation_smoke.py)
  map_pass_drop     -- в docs/CONTROL_MAP.md («Статус по JUnit обеих платформ CI: PASS N») N стало меньше (или
                       строки нет).
Ключ находки зоны -- `<вид>:<путь>::<что>@<blob12 файла в снимке | deleted>`: разрешение действует только на эту
версию файла (следующая правка того же файла -- новая находка).

Строка согласия (B33 H2.4; .github/test_weakening_allow.txt -- ослабления тестов, .github/guarded_change_allow.txt --
охраняемая зона): `<ключ> | <причина> | <YYYY-MM-DD> | <код согласия> | <entry_sha16>`, где entry_sha16 -- 16 hex
поля entry_sha16 записи журнала согласий (src/consent.py). '#' -- комментарий; плохая строка, повтор ключа или
повтор entry_sha16 -- ошибка (красный). ЗДЕСЬ (в CI) проверяются только формат и уникальность: журнала согласий в CI
нет; сверка entry_sha16 с журналом -- на ПК (push_*_stage, tools/consent_lines.py). Только стандартная
библиотека."""
from __future__ import annotations

import ast
import datetime as dt
import re
from typing import Iterable, Optional

KINDS = ("deleted_file", "removed_test", "fewer_asserts", "new_skip", "wider_tolerance", "changed_param")
GUARD_KINDS = ("guarded_change", "map_test_removed", "map_new_known_gap", "map_pass_drop")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_KEY_RE = re.compile(r"^(" + "|".join(KINDS) + r"):[^\s:|][^\s|]*::[A-Za-z_<][A-Za-z0-9_.<>]*(\[[^\s|]*\])?$")
_GKEY_RE = re.compile(r"^(" + "|".join(GUARD_KINDS) + r"):[^\s:|@][^\s|@]*::[A-Za-z_<][A-Za-z0-9_.<>-]*@([0-9a-f]{12}|deleted)$")
_SHA16_RE = re.compile(r"^[0-9a-f]{16}$")
# B33 H2.2: охраняемая зона -- явный список (conftest.py -- в любом каталоге; .github/workflows/ -- весь каталог)
GUARDED_FILES = ("pytest.ini", "docs/control_map.csv", "config/net_direct_registry.json", "src/weakening_guard.py",
                 "tools/weakening_guard.py", "tools/verify_control_map.py", "src/test_ci_gate.py",
                 "tools/guard_mutation_smoke.py", "tools/impl_mutation_smoke.py")
GUARDED_PREFIXES = (".github/workflows/",)
MAP_CSV = "docs/control_map.csv"
MAP_MD = "docs/CONTROL_MAP.md"
_PASS_RE = re.compile(r"^Статус по JUnit обеих платформ CI: PASS (\d+)\b", re.M)  # non-ascii-data: line of docs/CONTROL_MAP.md
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


def is_guarded_path(path: str) -> bool:
    p = path.replace("\\", "/")
    return p.rsplit("/", 1)[-1] == "conftest.py" or p in GUARDED_FILES or p.startswith(GUARDED_PREFIXES)


def _always_true(n) -> bool:
    """Условие assert, истинное при любых данных: истинная константа, непустой литерал кортежа / списка / множества /
    словаря, `a or <всегда истина>`, `<всегда истина> and <всегда истина>`, `not <ложная константа>`."""
    if isinstance(n, ast.Constant):
        return bool(n.value)
    if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
        return len(n.elts) > 0
    if isinstance(n, ast.Dict):
        return len(n.keys) > 0
    if isinstance(n, ast.BoolOp):
        if isinstance(n.op, ast.Or):
            return any(_always_true(v) for v in n.values)
        return all(_always_true(v) for v in n.values)
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        return isinstance(n.operand, ast.Constant) and not bool(n.operand.value)
    return False


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
            if not _always_true(x.test):                         # B33 H2.1: assert True / 1 / "x" / x or True -- не проверка
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


def _module_consts(tree) -> dict:
    """Имя модуля -> выражение (только простое присваивание `NAME = ...` верхнего уровня)."""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            out[n.targets[0].id] = n.value
    return out


def _param_elts(e, consts: dict, depth: int = 0) -> list:
    if depth < 8:
        if isinstance(e, (ast.List, ast.Tuple)):
            return list(e.elts)
        if isinstance(e, ast.Name) and e.id in consts:
            return _param_elts(consts[e.id], consts, depth + 1)
        if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
            return _param_elts(e.left, consts, depth + 1) + _param_elts(e.right, consts, depth + 1)
    return [ast.Call(func=ast.Name(id="<expr>", ctx=ast.Load()), args=[e], keywords=[])]


def _param_value(e) -> str:
    """Значение одного случая parametrize: pytest.param(*args, marks=...) -> args + marks (id не входит)."""
    if isinstance(e, ast.Call) and _chain(e.func)[-1:] == ["param"]:
        marks = [kw.value for kw in e.keywords if kw.arg == "marks"]
        v = ast.unparse(e.args[0]) if len(e.args) == 1 else ast.unparse(ast.Tuple(elts=list(e.args), ctx=ast.Load()))
        return v + ("" if not marks else " marks=" + ast.unparse(marks[0]))
    return ast.unparse(e)


def _param_values(fn, consts: dict) -> list:
    """Значения всех @pytest.mark.parametrize функции / класса: ['<argnames>=<значение>', ...] (мультимножество)."""
    out = []
    for d in fn.decorator_list:
        if isinstance(d, ast.Call) and tuple(_chain(d.func)[-2:]) == ("mark", "parametrize"):
            names = d.args[0] if d.args else next((kw.value for kw in d.keywords if kw.arg == "argnames"), None)
            vals = d.args[1] if len(d.args) > 1 else next((kw.value for kw in d.keywords if kw.arg == "argvalues"), None)
            if vals is None:
                continue
            nm = ast.unparse(names) if names is not None else "?"
            out += [f"{nm}={_param_value(x)}" for x in _param_elts(vals, consts)]
    return sorted(out)


def analyze(source: str) -> dict:
    """Исходник тестового файла -> {qualname: {asserts, skips, tol}}; '<module>' -- уровень файла
    (pytestmark, importorskip вне функций). SyntaxError -> {'<syntax_error>': ...}."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"<syntax_error>": {"asserts": 0, "skips": 0, "tol": {}}}
    out = {}
    mod = _Stats()
    consts = _module_consts(tree)

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
                out[node.name] = {"asserts": st.asserts, "skips": st.skips, "tol": st.tol, "params": _param_values(node, consts)}
        elif isinstance(node, ast.ClassDef):
            cls_skips = _skip_decorators(node)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                    st = fn_stats(sub)
                    out[f"{node.name}.{sub.name}"] = {"asserts": st.asserts, "skips": st.skips + cls_skips, "tol": st.tol,
                                                      "params": sorted(_param_values(sub, consts) + _param_values(node, consts))}
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
            left = list(t.get("params", []))                   # ревизор G2: значение базы исчезло (мультимножество)
            for v in s.get("params", []):
                if v in left:
                    left.remove(v)
                else:
                    found.append(f"changed_param:{path}::{q}")
                    break
        if b.get("<module>", {}).get("skips", 0) > a.get("<module>", {}).get("skips", 0) and \
                f"new_skip:{path}::<module>" not in found:
            found.append(f"new_skip:{path}::<module>")
        if "<syntax_error>" in b and "<syntax_error>" not in a:
            found.append(f"removed_test:{path}::<module>")
    return sorted(set(found))


def _csv_rows(text: str) -> dict:
    """docs/control_map.csv -> {id: (множество node ID колонки tests, known_gap)}; не разбирается -> {}."""
    import csv
    import io
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except csv.Error:
        return {}
    out = {}
    for r in rows:
        rid = (r.get("id") or "").strip()
        if rid:
            out[rid] = ({t.strip() for t in (r.get("tests") or "").split(";") if t.strip()}, (r.get("known_gap") or "").strip())
    return out


def _pass_count(text: str) -> Optional[int]:
    m = _PASS_RE.search(text or "")
    return int(m.group(1)) if m else None


def compare_guarded(base: dict, head: dict, read) -> list:
    """Охраняемая зона. base / head: {путь: blob} ВСЕХ файлов ревизии; read(blob) -> текст. -> ключи находок."""
    found = []

    def tag(path):
        return head[path][:12] if path in head else "deleted"
    for path in sorted(set(base) | set(head)):
        if is_guarded_path(path) and base.get(path) != head.get(path):
            found.append(f"guarded_change:{path}::<module>@{tag(path)}")
    if base.get(MAP_CSV) and base.get(MAP_CSV) != head.get(MAP_CSV):
        old = _csv_rows(read(base[MAP_CSV]))
        new = _csv_rows(read(head[MAP_CSV])) if MAP_CSV in head else {}
        for rid, (tests, gap) in sorted(old.items()):
            if tests - new.get(rid, (set(), ""))[0]:
                found.append(f"map_test_removed:{MAP_CSV}::{rid}@{tag(MAP_CSV)}")
        for rid, (tests, gap) in sorted(new.items()):
            if gap and not old.get(rid, (set(), ""))[1]:
                found.append(f"map_new_known_gap:{MAP_CSV}::{rid}@{tag(MAP_CSV)}")
    if base.get(MAP_MD) and base.get(MAP_MD) != head.get(MAP_MD):
        was = _pass_count(read(base[MAP_MD]))
        now = _pass_count(read(head[MAP_MD])) if MAP_MD in head else None
        if was is not None and (now is None or now < was):
            found.append(f"map_pass_drop:{MAP_MD}::<module>@{tag(MAP_MD)}")
    return sorted(set(found))


def compare_cases(base_ids: Iterable[str], head_ids: Iterable[str]) -> list:
    """B33 v2 C4 (B32m D2): собранные pytest node ID (`путь::[Класс::]функция[параметры]`) базы против снимка.
    Исчезнувший случай -- removed_test:<путь>::<функция[параметры]> (класс через точку), если такого же
    «функция[параметры]» нет ни в одном файле снимка (перенос между файлами -- не находка)."""
    head = set(head_ids)
    tails = {i.split("::", 1)[1].replace("::", ".") for i in head if "::" in i}
    tails |= {t.rsplit(".", 1)[-1] for t in tails}
    found = []
    for i in sorted(set(base_ids) - head):
        if "::" not in i:
            continue
        path, rest = i.split("::", 1)
        q = rest.replace("::", ".")
        if q in tails or q.rsplit(".", 1)[-1] in tails:
            continue
        found.append(f"removed_test:{path}::{q}")
    return sorted(set(found))


def parse_allow(lines: Iterable[str]) -> tuple:
    """Строки списка разрешений -> ({ключ: (причина, дата, код, entry_sha16)}, [ошибки 'line N: ...']).
    B33 H2.4: пятое поле -- entry_sha16 записи журнала согласий; повтор ключа или entry_sha16 -- ошибка."""
    allow, errs, seen16 = {}, [], set()
    for i, raw in enumerate(lines, start=1):
        ln = str(raw).strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [x.strip() for x in ln.split("|")]
        if len(parts) != 5:
            errs.append(f"line {i}: 5 fields separated by '|': key | reason | date | consent code | entry_sha16")
            continue
        key, why, day, code, e16 = parts
        if not (_KEY_RE.match(key) or _GKEY_RE.match(key)):
            errs.append(f"line {i}: key of the form <kind>:<path>::<function> (zone: <kind>:<path>::<what>@<blob12|deleted>)")
            continue
        if not why:
            errs.append(f"line {i}: empty reason")
            continue
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            errs.append(f"line {i}: date YYYY-MM-DD")
            continue
        if not _CODE_RE.match(code):
            errs.append(f"line {i}: consent code [a-z][a-z0-9_]")
            continue
        if not _SHA16_RE.match(e16):
            errs.append(f"line {i}: entry_sha16 -- 16 lower-case hex of a consent journal entry")
            continue
        if key in allow:
            errs.append(f"line {i}: duplicate key")
            continue
        if e16 in seen16:
            errs.append(f"line {i}: duplicate entry_sha16 (one journal entry -- one consent line)")
            continue
        seen16.add(e16)
        allow[key] = (why, day, code, e16)
    return allow, errs


def judge(findings: list, allow: dict) -> dict:
    """-> {ok, red: [ключи без разрешения], allowed: [...], unused_allow: [...]}."""
    red = [f for f in findings if f not in allow]
    return {"ok": not red, "red": red, "allowed": [f for f in findings if f in allow],
            "unused_allow": sorted(k for k in allow if k not in findings)}
