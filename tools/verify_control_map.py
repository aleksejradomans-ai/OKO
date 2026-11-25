"""Верификатор карты доказуемости: docs/control_map.csv против docs/INVARIANTS.md, кода src/ и отчётов JUnit.

  python tools/verify_control_map.py [--root .] [--csv docs/control_map.csv] [--invariants docs/INVARIANTS.md]
      --junit windows=pytest_src.xml --junit windows=pytest_extra.xml --junit linux=... [--platforms windows,linux]
      [--write-md <файл>] [--check-md docs/CONTROL_MAP.md] [--generated-on <sha>] [--base-rev HEAD~1]

Что проверяется (любое нарушение -> код возврата 1):
  1. множества INV-* в csv и в INVARIANTS.md совпадают 1-в-1 (без повторов);
  2. файл и символ из колонки implementation существуют (символ ищется по AST: класс / функция / метод, без импорта);
     файл -- только в корнях IMPL_ROOTS (src/, oko_rag/);
  3. каждый node ID из колонки tests (evidence != manual) имеет в JUnit строгий PASS на КАЖДОЙ платформе из --platforms.
     FAIL / ERROR / SKIP / XFAIL / XPASS / отсутствие в отчёте -- не PASS. XPASS без strict в JUnit не отличим от PASS,
     поэтому тест с меткой xfail (декоратор, pytestmark, pytest.param(marks=...), вызов pytest.xfail) или из
     .github/known_xfail.txt подтверждающим быть не может. SKIP допустим, только если known_gap называет этот тест
     (имя функции) -> статус GAP;
  4. evidence из {behavior, integration, source-scan, manual}; scope не пуст; manual -- без тестов и с known_gap;
     тест с evidence behavior / integration не читает исходник (ast.parse, inspect.getsource, read_text / open над
     '*.py' или __file__ -- в самом тесте и в функциях / константах его модуля), иначе это source-scan;
  5. docs/kernel_codes.txt против единого реестра кодов (src/governor.py: YELLOW_CODES / RED_CODES / SPLIT_CODES, по AST);
     нет реестра -> строка GAP «code registry missing, check not done», это не PASS;
  6. разметка зон: у каждого INV в INVARIANTS.md ровно одна зона (красная / рабочая), строка «что сломается» в
     docs/control_map_plain.csv для каждого INV;
  7. --check-md: свежая карта (без строки «сгенерирован на ...») совпадает с файлом, иначе «карта устарела».
  8. B33 H1.3: .github/workflows тронут относительно --base-rev -> нарушение, если для КАЖДОГО тронутого файла в
     ревизии HEAD нет строки согласия охраняемой зоны (--guarded-allow, формат src/weakening_guard.py: ключ
     `guarded_change:<путь>::<module>@<blob12 файла в HEAD | deleted>` | причина | дата | код | entry_sha16).
     Проверяется только формат строки; сверка entry_sha16 с журналом согласий -- на ПК. Базы нет -- строка
     «сравнение невозможно», не нарушение (экспорт снимка без git) -- КРОМЕ --require-base (CI).
  9. B33 v2 C3 (B32m D1): --base-rev auto -- база дельты = git merge-base HEAD refs/remotes/origin/oko-live (последний
     опубликованный коммит, а не HEAD~1); --require-base -- базу определить нельзя -> нарушение (в CI).
Один отчёт вместо двух платформ (--platforms не покрыт) -> печатается «проверено на 1 платформе из 2», статус PASS не
выдаётся (вместо него PARTIAL), карта не пишется и не сверяется. Режима «collect-only» нет: учитываются только
исполненные тесты из JUnit.
В конце stdout -- блок приёмки (не больше 15 строк). Код возврата: 0 -- нарушений нет; 1 -- есть; 2 -- ошибка вызова.
Только stdlib; ничего не пишет, кроме файла --write-md."""
from __future__ import annotations

import argparse
import ast
import csv
import io
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

COLUMNS = ["id", "implementation", "tests", "evidence", "scope", "known_gap"]
EVIDENCE = ("behavior", "integration", "source-scan", "manual")
PLATFORMS = ("windows", "linux")
ZONES = {"красная": "red", "рабочая": "work"}  # non-ascii-data: zone words of docs/INVARIANTS.md
INV_RX = re.compile(r"\*\*(INV-\d{2})\b")
ZONE_ROW_RX = re.compile(r"^\|\s*(INV-\d{2})\s*\|\s*(красная|рабочая)\s*\|")  # non-ascii-data: docs/INVARIANTS.md
GEN_PREFIX = "Сгенерирован на "  # non-ascii-data: line of docs/CONTROL_MAP.md
UNREVIEWED = "Карта не сверена независимо: внешней сверки этой версии не было."  # non-ascii-data: docs/CONTROL_MAP.md
XPASS_MSG = "xfail-marked test passes unexpectedly"
# Корни кода для колонки implementation -- явный список (решение Оператора 22.11, B32-Q2 = [1]): src/ и пакет поиска
# по базе знаний oko_rag/ (INV-18). Любой другой каталог -- ошибка.
IMPL_ROOTS = ("src/", "oko_rag/")


def out(s: str = "") -> None:
    sys.stdout.buffer.write((s + "\n").encode("ascii", "backslashreplace"))  # B33 attempt 4: console output ASCII (non-ASCII data escaped)
    sys.stdout.buffer.flush()


# ------------------------------------------------------------------ входные файлы
def read_csv(path: Path) -> tuple[list[dict], list[str]]:
    errs: list[str] = []
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or rows[0] != COLUMNS:
        return [], [f"csv schema: header {rows[0] if rows else '(empty)'} != {','.join(COLUMNS)}"]
    res = []
    for n, r in enumerate(rows[1:], 2):
        if not any(c.strip() for c in r):
            continue
        if len(r) != len(COLUMNS):
            errs.append(f"csv schema: row {n}: columns {len(r)} != {len(COLUMNS)}")
            continue
        res.append({k: v.strip() for k, v in zip(COLUMNS, r)})
    return res, errs


def invariant_ids(path: Path) -> list[str]:
    return INV_RX.findall(path.read_text(encoding="utf-8-sig"))


def invariant_zones(path: Path) -> dict[str, list[str]]:
    z: dict[str, list[str]] = {}
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
        m = ZONE_ROW_RX.match(ln.strip())
        if m:
            z.setdefault(m.group(1), []).append(ZONES[m.group(2)])
    return z


def read_plain(path: Path) -> tuple[dict[str, str], list[str]]:
    if not path.is_file():
        return {}, [f"missing file {path.name} (rows 'what breaks')"]
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    if not rows or rows[0] != ["id", "breaks"]:
        return {}, [f"schema {path.name}: the header must be id,breaks"]
    res, errs = {}, []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        if len(r) != 2 or not r[1].strip():
            errs.append(f"{path.name}: empty or broken row {r[:1]}")
            continue
        if r[0].strip() in res:
            errs.append(f"{path.name}: duplicate {r[0].strip()}")
        res[r[0].strip()] = r[1].strip()
    return res, errs


# ------------------------------------------------------------------ код (AST, без импорта)
def _defs(body) -> dict:
    d = {}
    for n in body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            d[n.name] = n
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    d[t.id] = n
    return d


def find_symbol(root: Path, impl: str) -> str | None:
    """None -- найдено; иначе текст ошибки. impl = <путь>.py::<Символ>[.<метод>]."""
    if "::" not in impl:
        return f"implementation without '::': {impl}"
    rel, sym = impl.split("::", 1)
    if not rel.startswith(IMPL_ROOTS) or ".." in rel.split("/"):
        return f"implementation outside the roots {', '.join(IMPL_ROOTS)}: {rel}"
    p = root / rel
    if not p.is_file():
        return f"missing file {rel}"
    try:
        tree = ast.parse(p.read_text(encoding="utf-8-sig"))
    except SyntaxError as e:
        return f"{rel}: does not parse ({e.msg})"
    scope = _defs(tree.body)
    parts = sym.split(".")
    for i, part in enumerate(parts):
        node = scope.get(part)
        if node is None:
            return f"missing symbol {'.'.join(parts[:i + 1])} in {rel}"
        if i < len(parts) - 1:
            if not isinstance(node, ast.ClassDef):
                return f"{'.'.join(parts[:i + 1])} in {rel} -- not a class"
            scope = _defs(node.body)
    return None


def _is_xfail_expr(e) -> bool:
    """pytest.mark.xfail / pytest.mark.xfail(...) / mark.xfail."""
    if isinstance(e, ast.Call):
        e = e.func
    return isinstance(e, ast.Attribute) and e.attr == "xfail"


def xfail_marked(root: Path, nodeid: str) -> bool | None:
    """True -- у теста есть метка xfail (или он зовёт pytest.xfail); None -- функция теста не найдена."""
    rel, _, rest = nodeid.partition("::")
    p = root / rel
    if not p.is_file():
        return None
    try:
        tree = ast.parse(p.read_text(encoding="utf-8-sig"))
    except SyntaxError:
        return None
    names = [x.split("[", 1)[0] for x in rest.split("::")]
    marks = []
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets):
            marks.append(n.value)
    scope, node = tree.body, None
    for i, nm in enumerate(names):
        node = next((x for x in scope if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and x.name == nm), None)
        if node is None:
            return None
        marks.extend(node.decorator_list)
        if isinstance(node, ast.ClassDef):
            for x in node.body:
                if isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark"
                                                     for t in x.targets):
                    marks.append(x.value)
            scope = node.body
    for m in marks:
        for e in ast.walk(m):
            if _is_xfail_expr(e):
                return True
    for e in ast.walk(node):
        if isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "xfail":
            return True
    return False


def _src_read_calls(node) -> list[str]:
    """Признаки чтения исходника внутри узла: ast.parse, inspect.getsource*, read_text / read_bytes / open над
    выражением с литералом '*.py' или с __file__ / __code__."""
    hits = []
    for e in ast.walk(node):
        if not isinstance(e, ast.Call):
            continue
        f = e.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
        if name in ("getsource", "getsourcelines", "getsourcefile"):
            hits.append("inspect." + name)
        elif name == "parse" and base == "ast":
            hits.append("ast.parse")
        elif name in ("read_text", "read_bytes", "open", "readlines"):
            scope = [f.value] if isinstance(f, ast.Attribute) else []
            scope += list(e.args)
            for s in scope:
                for x in ast.walk(s):
                    if isinstance(x, ast.Constant) and isinstance(x.value, str) and x.value.endswith(".py"):
                        hits.append(f"{name}('{x.value}')")
                    if isinstance(x, (ast.Name, ast.Attribute)) and \
                            (getattr(x, "id", "") == "__file__" or getattr(x, "attr", "") in ("__file__", "__code__")):
                        hits.append(f"{name}(__file__)")
    return hits


def reads_source(root: Path, nodeid: str) -> list[str] | None:
    """Признаки чтения исходного кода в тесте и в функциях / фикстурах / константах модуля, которыми он пользуется.
    [] -- не читает; None -- тест не найден. Фикстуры из conftest.py не просматриваются."""
    rel, _, rest = nodeid.partition("::")
    p = root / rel
    if not p.is_file():
        return None
    tree = ast.parse(p.read_text(encoding="utf-8-sig"))
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    consts = {}
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        consts[x.id] = n
    names = [x.split("[", 1)[0] for x in rest.split("::")]
    scope, node = tree.body, None
    for nm in names:
        node = next((x for x in scope if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and x.name == nm), None)
        if node is None:
            return None
        scope = node.body if isinstance(node, ast.ClassDef) else []
    hits, seen, todo = [], set(), [node]
    while todo:
        cur = todo.pop()
        hits += _src_read_calls(cur)
        used = {x.id for x in ast.walk(cur) if isinstance(x, ast.Name)}
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            used |= {a.arg for a in cur.args.args}
        for u in sorted(used):
            if u in seen:
                continue
            if u in funcs and funcs[u] is not cur:
                seen.add(u)
                todo.append(funcs[u])
            elif u in consts:
                seen.add(u)
                todo.append(consts[u])
    return sorted(set(hits))


# ------------------------------------------------------------------ JUnit
def nodeid_key(nodeid: str) -> tuple[str, str]:
    """src/test_a.py::TestX::test_y[p] -> (classname 'src.test_a.TestX', name 'test_y[p]') -- как пишет pytest."""
    rel, _, rest = nodeid.partition("::")
    parts = rest.split("::")
    mod = rel[:-3] if rel.endswith(".py") else rel
    cls = ".".join([mod.replace("/", ".")] + parts[:-1])
    return cls, parts[-1]


def junit_cases(paths: list[Path]) -> dict[tuple[str, str], str]:
    """{(classname, name): PASS|FAIL|ERROR|SKIP|XFAIL|XPASS}. Повтор с разными исходами -> худший."""
    rank = {"PASS": 0, "SKIP": 1, "XFAIL": 2, "XPASS": 3, "FAIL": 4, "ERROR": 5}
    res: dict[tuple[str, str], str] = {}
    for p in paths:
        root = ET.parse(str(p)).getroot()
        for tc in root.iter("testcase"):
            st = "PASS"
            for ch in tc:
                if ch.tag == "failure":
                    st = "FAIL"
                elif ch.tag == "error":
                    st = "ERROR"
                elif ch.tag == "skipped":
                    if ch.get("type") == "pytest.xfail":
                        st = "XFAIL"
                    elif (ch.get("message") or "").startswith(XPASS_MSG):
                        st = "XPASS"
                    else:
                        st = "SKIP"
                if st != "PASS":
                    break
            k = (tc.get("classname", ""), tc.get("name", ""))
            if k not in res or rank[st] > rank[res[k]]:
                res[k] = st
    return res


def _unesc(s: str) -> str:
    try:
        return s.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


def test_status(cases: dict, nodeid: str) -> tuple[str, int]:
    """(худший статус, число найденных случаев). Без [..] в node ID -- все параметры теста, нужен хотя бы один."""
    cls, name = nodeid_key(nodeid)
    if "[" in name:
        want = _unesc(name)
        got = [st for (c, n), st in cases.items() if c == cls and _unesc(n) == want]
    else:
        got = [st for (c, n), st in cases.items() if c == cls and (n == name or n.startswith(name + "["))]
    if not got:
        return "MISSING", 0
    order = ["ERROR", "FAIL", "XPASS", "XFAIL", "SKIP", "PASS"]
    return next(s for s in order if s in got), len(got)


# ------------------------------------------------------------------ реестр кодов
def registry_codes(path: Path) -> dict[str, set] | None:
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    want = {"YELLOW_CODES": "yellow", "RED_CODES": "red", "SPLIT_CODES": "split"}
    got: dict[str, set] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and n.targets[0].id in want:
            vals = {e.value for e in ast.walk(n.value) if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            got[want[n.targets[0].id]] = vals
    return got if set(got) == set(want.values()) else None


_KC_LINES = {"yellow": re.compile(r"^Жёлтые коды[^:]*:\s*(.+)$"), "red": re.compile(r"^Красные коды[^:]*:\s*(.+)$"),  # non-ascii-data: docs/kernel_codes.txt
             "split": re.compile(r"^Смешанный код:\s*(.+)$")}  # non-ascii-data: docs/kernel_codes.txt


def doc_codes(path: Path) -> tuple[dict[str, set], int | None]:
    text = path.read_text(encoding="utf-8-sig")
    got: dict[str, set] = {}
    for ln in text.splitlines():
        for k, rx in _KC_LINES.items():
            m = rx.match(ln.strip())
            if m and k not in got:
                part = m.group(1).split(" — ")[0] if k == "split" else m.group(1)  # non-ascii-data: dash of docs/kernel_codes.txt
                got[k] = set(re.findall(r"\b[A-Z][A-Z_]{3,}\b", part))
    m = re.search(r"Всего (\d+) код", text)  # non-ascii-data: docs/kernel_codes.txt
    return got, (int(m.group(1)) if m else None)


def check_codes(root: Path, doc: Path, reg: Path) -> tuple[str, list[str]]:
    """('PASS'|'GAP'|'FAIL', строки)."""
    r = registry_codes(reg)
    if r is None:
        return "GAP", ["code registry missing, check not done"]
    if not doc.is_file():
        return "FAIL", [f"missing {doc.name}"]
    d, total = doc_codes(doc)
    lines, ok = [], True
    for k in ("yellow", "red", "split"):
        a, b = r.get(k, set()), d.get(k, set())
        if a != b:
            ok = False
            lines.append(f"{k}: only in the code {sorted(a - b)}, only in the document {sorted(b - a)}")
    allc = set().union(*r.values())
    if total != len(allc):
        ok = False
        lines.append(f"'Total N codes': document {total}, registry {len(allc)}")
    for c in sorted(allc):
        if f"[ERR: {c}]" not in doc.read_text(encoding="utf-8-sig"):
            ok = False
            lines.append(f"code {c} without a description [ERR: {c}] in the document")
    return ("PASS" if ok else "FAIL"), (lines or [f"document codes = registry ({len(allc)})"])


# ------------------------------------------------------------------ сводка строки
def _gap_names(gap: str, nodeid: str) -> bool:
    """Платформенный пропуск допустим, только если known_gap называет этот тест (имя функции)."""
    name = nodeid.rsplit("::", 1)[-1].split("[", 1)[0]
    return bool(gap) and re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", gap) is not None


def row_status(row: dict, per_platform: dict[str, dict], root: Path, xfail_known: set, full: bool) -> tuple[str, list]:
    notes = []
    ev = row["evidence"]
    tests = [t.strip() for t in row["tests"].split(";") if t.strip()]
    if ev == "manual":
        return "MANUAL", notes
    bad = skip = False
    for t in tests:
        if t in xfail_known or any(t == x.split("[", 1)[0] for x in xfail_known):
            bad = True
            notes.append(f"{t}: in known_xfail -- does not confirm (move it to known_gap)")
            continue
        xm = xfail_marked(root, t)
        if xm is None:
            bad = True
            notes.append(f"{t}: test function not found")
            continue
        for plat, cases in per_platform.items():
            st, n = test_status(cases, t)
            if st == "PASS" and xm:
                st = "XPASS"
            if st == "PASS":
                continue
            if st == "SKIP" and _gap_names(row["known_gap"], t):
                skip = True
                notes.append(f"{t} [{plat}]: SKIP (known_gap) -> GAP")
                continue
            bad = True
            notes.append(f"{t} [{plat}]: {st}")
    if bad:
        return "FAIL", notes
    if skip or row["known_gap"]:
        return "GAP", notes
    return ("PASS" if full else "PARTIAL"), notes


# ------------------------------------------------------------------ карта
def _md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def _md_codes_line(x: str) -> str:  # non-ascii-data: docs/CONTROL_MAP.md stays Russian (Operator document); console is English
    m = re.fullmatch(r"document codes = registry \((\d+)\)", x)
    return f"коды документа = реестр ({m.group(1)})" if m else x


def render_md(rows: list[dict], status: dict, zones: dict, plain: dict, codes: tuple, generated_on: str) -> str:  # non-ascii-data: docs/CONTROL_MAP.md (Operator document)
    cnt = {e: sum(1 for r in rows if r["evidence"] == e) for e in EVIDENCE}
    gaps = sum(1 for r in rows if r["known_gap"])
    st = {s: sum(1 for v in status.values() if v == s) for s in ("PASS", "GAP", "MANUAL", "FAIL")}
    red = sum(1 for r in rows if zones.get(r["id"]) == "red")
    head = [
        "# Карта доказуемости инвариантов (генерируется tools/verify_control_map.py, руками не править)",
        f"Доказательства: behavior {cnt['behavior']} · integration {cnt['integration']} · source-scan "
        f"{cnt['source-scan']} · manual {cnt['manual']} · с known_gap {gaps} (всего {len(rows)}).",
        f"Статус по JUnit обеих платформ CI: PASS {st['PASS']} · GAP {st['GAP']} · MANUAL {st['MANUAL']} · FAIL "
        f"{st['FAIL']}; коды ядра: {codes[0]}. Красная зона: {red}, рабочая: {len(rows) - red}.",
        UNREVIEWED,
        f"{GEN_PREFIX}{generated_on} (коммит не может содержать свой SHA; CI сверяет карту без этой строки).",
        "",
    ]
    body = [
        "## Как читать",
        "Верхний слой (чертёж): код, тесты, тип доказательства, статус. PASS -- каждый подтверждающий тест прошёл на "
        "Windows и на Linux; GAP -- тесты прошли, но есть известный пробел или платформенный пропуск; MANUAL -- "
        "автотеста нет; FAIL -- хотя бы один тест не прошёл, пропущен без записи о пробеле, помечен как ожидаемое "
        "падение или не найден в отчёте.",
        "",
        "Нижний слой (по-человечески): в каждой строке -- что сломается в жизни, если правило пробьют. Красная зона -- "
        "любая правка требует независимой проверки; рабочая -- хватает автомата CI и выборочной проверки.",
        "",
        "| ID | зона | статус | evidence | implementation | tests | scope | known_gap |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        tests = "<br>".join(f"`{_md_cell(t.strip())}`" for t in r["tests"].split(";") if t.strip()) or "—"
        impl = f"`{_md_cell(r['implementation'])}`" if r["implementation"] else "—"
        zone = {"red": "красная", "work": "рабочая"}.get(zones.get(r["id"], ""), "?")
        body.append(f"| {r['id']} | {zone} | {status[r['id']]} | {r['evidence']} | {impl} | {tests} | "
                    f"{_md_cell(r['scope'])} | {_md_cell(r['known_gap']) or '—'} |")
    body += ["", "## Что сломается, если правило пробьют", ""]
    for r in rows:
        body.append(f"- **{r['id']}** — {plain.get(r['id'], '?')}")
    body += ["", "## Коды ядра (docs/kernel_codes.txt против реестра в коде)", ""] + [f"- {_md_codes_line(x)}" for x in codes[1]]
    return "\n".join(head + body) + "\n"


def strip_generated(md: str) -> str:
    return "\n".join(ln for ln in md.splitlines() if not ln.startswith(GEN_PREFIX)) + "\n"


# ------------------------------------------------------------------ дельта
_CHILD_DROP = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH", "PYTHONWARNINGS",
               "PYTHONINSPECT", "PYTHONEXECUTABLE", "PYTHONNOUSERSITE", "PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST",
               "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG", "OKO_NOTIFY_OFF", "OKO_SIGMA_LIVE",
               "OKO_REQUEST_TS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
               "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CONFIG_PARAMETERS",
               "GIT_CONFIG_COUNT", "GIT_CEILING_DIRECTORIES")


def child_env() -> dict:
    """B33 attempt 4 (item 3): hermetic environment of a child (git / pytest): without the parent variables that change
    Python / pytest / git (PYTHONHOME, PYTHONPATH, PYTEST_ADDOPTS -q would empty --collect-only, OKO_B33_*, GIT_DIR ...),
    with UTF-8 stdio."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in _CHILD_DROP
           and not k.upper().startswith(("OKO_B33_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def _git(root: Path, *args: str) -> tuple[int, bytes]:
    try:
        r = subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=false", *args], capture_output=True,
                           timeout=60, env=child_env())
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, b""


def _guard_module(root: Path):
    """src/weakening_guard.py ЭТОГО дерева (формат строк согласия -- один на сторожа и верификатор); нет -> None."""
    import importlib.util
    f = root / "src" / "weakening_guard.py"
    if not f.is_file():
        return None
    spec = importlib.util.spec_from_file_location("weakening_guard_vcm", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def workflow_consent(root: Path, names: list[str], allow_rel: str) -> tuple[list[str], str]:
    """(ключи без строки согласия, пояснение). Строки -- из ревизии HEAD файла allow_rel."""
    wg = _guard_module(root)
    if wg is None:
        return list(names), "no src/weakening_guard.py (format of the consent lines unknown)"
    rc, raw = _git(root, "show", f"HEAD:{allow_rel}")
    allow, errs = wg.parse_allow(raw.decode("utf-8-sig", "replace").splitlines() if rc == 0 else [])
    missing = []
    for n in names:
        rc2, b = _git(root, "rev-parse", "-q", "--verify", f"HEAD:{n}")
        tag = b.decode("ascii", "replace").strip()[:12] if rc2 == 0 and b.strip() else "deleted"
        key = f"guarded_change:{n}::<module>@{tag}"
        if key not in allow:
            missing.append(key)
    note = f"consent lines in {allow_rel}: {len(allow)}" + (f", format errors {len(errs)}" if errs else "")
    return (missing + [f"{allow_rel}: {e}" for e in errs]), note


def delta(root: Path, rows: list[dict], base: str, csv_rel: str, errors: list = None,
          allow_rel: str = ".github/guarded_change_allow.txt") -> list[str]:
    rc, raw = _git(root, "show", f"{base}:{csv_rel}")
    lines = []
    if rc != 0:
        lines.append(f"delta: no base for the delta (first run: no {csv_rel} in {base})")
    else:
        try:
            old = {r[0]: dict(zip(COLUMNS, r)) for r in list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))[1:]
                   if r}
        except UnicodeDecodeError:
            old = {}
        new = {r["id"]: r for r in rows}
        add, rem = sorted(set(new) - set(old)), sorted(set(old) - set(new))
        chg = [f"{i}: {old[i].get('evidence', '?')} -> {new[i]['evidence']}" for i in sorted(set(new) & set(old))
               if old[i].get("evidence", "").strip() != new[i]["evidence"]]
        edited = sum(1 for i in set(new) & set(old) if [old[i].get(c, "").strip() for c in COLUMNS] !=
                     [new[i][c] for c in COLUMNS])
        lines.append(f"delta to {base}: added {len(add)} {add if add else ''} removed {len(rem)} "
                     f"{rem if rem else ''} rows edited {edited}".replace("  ", " "))
        lines.append("evidence changed: " + ("; ".join(chg) if chg else "none"))
    rc, raw = _git(root, "diff", "--name-only", base, "HEAD", "--", ".github/workflows")
    if rc != 0:
        lines.append(f".github/workflows: comparison with {base} impossible (no base)")
    else:
        names = [x for x in raw.decode("utf-8", "replace").splitlines() if x.strip()]
        if not names:
            lines.append(".github/workflows touched: no")
        else:
            miss, note = workflow_consent(root, names, allow_rel)
            lines.append(f".github/workflows touched: YES ({', '.join(names)}); consent: "
                         + ("present" if not miss else f"MISSING for {len(miss)} -- violation") + f" ({note})")
            if errors is not None:
                errors += [f".github/workflows touched without a consent line: {m}" for m in miss]
    return lines


# ------------------------------------------------------------------ main
def run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="verify docs/control_map.csv")
    ap.add_argument("--root", default=".")
    ap.add_argument("--csv", default="docs/control_map.csv")
    ap.add_argument("--invariants", default="docs/INVARIANTS.md")
    ap.add_argument("--plain", default="docs/control_map_plain.csv")
    ap.add_argument("--kernel-codes", default="docs/kernel_codes.txt")
    ap.add_argument("--code-registry", default="src/governor.py")
    ap.add_argument("--known-xfail", default=".github/known_xfail.txt")
    ap.add_argument("--junit", action="append", default=[], metavar="PLATFORM=PATH")
    ap.add_argument("--platforms", default=",".join(PLATFORMS))
    ap.add_argument("--write-md", default="")
    ap.add_argument("--check-md", default="")
    ap.add_argument("--generated-on", default="")
    ap.add_argument("--base-rev", default="HEAD~1")
    ap.add_argument("--guarded-allow", default=".github/guarded_change_allow.txt")
    ap.add_argument("--require-base", action="store_true", help="B33 v2 C3: no delta base -> violation")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    need = [p.strip() for p in a.platforms.split(",") if p.strip()]
    jun: dict[str, list[Path]] = {}
    for spec in a.junit:
        if "=" not in spec:
            out(f"USAGE --junit {spec}: PLATFORM=PATH needed")
            return 2
        plat, path = spec.split("=", 1)
        if plat not in need:
            out(f"USAGE --junit {plat}: platform not in --platforms {need}")
            return 2
        jun.setdefault(plat, []).append(Path(path))
    if not jun:
        out("USAGE at least one --junit PLATFORM=PATH needed (the verifier does not collect tests itself)")
        return 2
    full = set(jun) == set(need)
    if not full and (a.write_md or a.check_md):
        out("USAGE --write-md / --check-md only with the reports of all platforms")
        return 2

    errors: list[str] = []
    per: dict[str, dict] = {}
    for plat, paths in jun.items():
        for p in paths:
            if not p.is_file():
                errors.append(f"missing JUnit {plat}: {p}")
        try:
            per[plat] = junit_cases([p for p in paths if p.is_file()])
        except ET.ParseError as e:
            errors.append(f"JUnit {plat} does not parse: {e}")
            per[plat] = {}
    rows, e = read_csv(root / a.csv)
    errors += e
    inv = invariant_ids(root / a.invariants)
    ids = [r["id"] for r in rows]
    dup = sorted({i for i in ids if ids.count(i) > 1}) + sorted({i for i in inv if inv.count(i) > 1})
    if dup:
        errors.append(f"duplicate ID: {dup}")
    if set(ids) != set(inv) or not inv:
        errors.append(f"ID not 1-to-1: only in csv {sorted(set(ids) - set(inv))}, only in INVARIANTS.md "
                      f"{sorted(set(inv) - set(ids))}")
    zones_raw = invariant_zones(root / a.invariants)
    zones = {}
    for i in inv:
        z = zones_raw.get(i, [])
        if len(z) != 1:
            errors.append(f"{i}: zones in INVARIANTS.md {len(z)} (exactly one needed: red / work)")
        else:
            zones[i] = z[0]
    extra_z = sorted(set(zones_raw) - set(inv))
    if extra_z:
        errors.append(f"zones for missing IDs: {extra_z}")
    plain, e = read_plain(root / a.plain)
    errors += e
    if set(plain) != set(inv):
        errors.append(f"'what breaks' not 1-to-1: missing {sorted(set(inv) - set(plain))}, extra "
                      f"{sorted(set(plain) - set(inv))}")
    xk = set()
    p = root / a.known_xfail
    if p.is_file():
        xk = {_unesc(x.strip()) for x in p.read_text(encoding="utf-8-sig").splitlines() if x.strip()}
        xk |= {x.strip() for x in p.read_text(encoding="utf-8-sig").splitlines() if x.strip()}
    status: dict[str, str] = {}
    row_notes: dict[str, list] = {}
    for r in rows:
        rid = r["id"]
        if r["evidence"] not in EVIDENCE:
            errors.append(f"{rid}: invalid evidence '{r['evidence']}'")
        if not r["scope"]:
            errors.append(f"{rid}: empty scope")
        tests = [t.strip() for t in r["tests"].split(";") if t.strip()]
        if r["evidence"] == "manual":
            if tests:
                errors.append(f"{rid}: manual with tests (manual = no automated test)")
            if not r["known_gap"]:
                errors.append(f"{rid}: manual without known_gap (a description of the gap is needed)")
        else:
            if not tests:
                errors.append(f"{rid}: evidence {r['evidence']} without tests")
            if not r["implementation"]:
                errors.append(f"{rid}: empty implementation")
        for t in tests:
            if "::" not in t or not t.split("::", 1)[0].endswith(".py"):
                errors.append(f"{rid}: not a node ID: {t}")
            elif r["evidence"] in ("behavior", "integration"):
                rs = reads_source(root, t)
                if rs:
                    errors.append(f"{rid}: {t} reads the source ({', '.join(rs)}) -- this is source-scan, not "
                                  f"{r['evidence']}")
        if r["implementation"]:
            for impl in [x.strip() for x in r["implementation"].split(";") if x.strip()]:
                fe = find_symbol(root, impl)
                if fe:
                    errors.append(f"{rid}: {fe}")
        st, notes = row_status(r, per, root, xk, full)
        status[rid], row_notes[rid] = st, notes
        if st == "FAIL":
            errors.append(f"{rid}: FAIL -- " + "; ".join(notes))
    codes = check_codes(root, root / a.kernel_codes, root / a.code_registry)
    if codes[0] == "FAIL":
        errors.append("kernel_codes.txt differs from the registry: " + "; ".join(codes[1]))

    out(f"VERIFY root={root.name} csv={a.csv} rows={len(rows)} invariants={len(inv)} platforms="
        f"{','.join(sorted(jun))} need={','.join(need)} junit_cases=" +
        ",".join(f"{k}:{len(v)}" for k, v in sorted(per.items())))
    for r in rows:
        nts = row_notes.get(r["id"], [])
        out(f"ROW {r['id']} {status.get(r['id'])} {r['evidence']}" + (f" | {'; '.join(nts)}" if nts else ""))
    out(f"CODES {codes[0]} " + " | ".join(codes[1]))

    md_line = "map: not checked"
    if full and (a.write_md or a.check_md):
        md = render_md(rows, status, zones, plain, codes, a.generated_on or "(SHA не задан)")  # non-ascii-data: docs/CONTROL_MAP.md
        if a.write_md:
            Path(a.write_md).write_bytes(md.encode("utf-8"))
        if a.check_md:
            cp = root / a.check_md
            cur = cp.read_text(encoding="utf-8-sig").replace("\r\n", "\n") if cp.is_file() else ""
            if strip_generated(cur) != strip_generated(md):
                errors.append(f"map is stale, regenerate: {a.check_md} != fresh (without the HEAD line)")
                md_line = "map: STALE (regenerate)"
            else:
                md_line = "map: equals the fresh one (without the HEAD line)"
        elif a.write_md:
            md_line = f"map: written {Path(a.write_md).name}"
    for x in errors:
        out(f"ERROR {x}")

    cnt = {e2: sum(1 for r in rows if r["evidence"] == e2) for e2 in EVIDENCE}
    stc = {s: sum(1 for v in status.values() if v == s) for s in ("PASS", "PARTIAL", "GAP", "MANUAL", "FAIL")}
    block = ["=== ACCEPTANCE control_map ==="]
    block.append(("checked on 2 platforms of 2" if full else
                  f"checked on {len(jun)} platform of {len(need)}: no PASS given"))
    block.append(f"ID 1-to-1: {'yes' if set(ids) == set(inv) and not dup and inv else 'NO'} ({len(inv)})")
    block.append(f"evidence: behavior {cnt['behavior']} / integration {cnt['integration']} / source-scan "
                 f"{cnt['source-scan']} / manual {cnt['manual']} / known_gap {sum(1 for r in rows if r['known_gap'])}")
    block.append(" ".join(f"{k} {v}" for k, v in stc.items() if v or k != "PARTIAL"))
    block.append(f"kernel codes: {codes[0]}")
    block.append(md_line)
    n_err = len(errors)
    base_rev = a.base_rev
    if base_rev == "auto":                                # B33 v2 C3: база = опубликованный коммит
        rc_b, raw_b = _git(root, "merge-base", "HEAD", "refs/remotes/origin/oko-live")
        base_rev = raw_b.decode("ascii", "replace").strip() if rc_b == 0 and raw_b.strip() else "refs/oko/no-published-base"
    if a.require_base and _git(root, "rev-parse", "-q", "--verify", base_rev + "^{commit}")[0] != 0:
        errors.append(f"delta base not determined ({a.base_rev}): the published commit is needed (fetch-depth 0, origin/oko-live)")
    block += delta(root, rows, base_rev, a.csv, errors, a.guarded_allow)
    for x in errors[n_err:]:                            # B33 H1.3: нарушения дельты -- тоже строки ERROR
        out(f"ERROR {x}")
    block.append(f"violations: {len(errors)}")
    block.append(f"DECISION: {'[1] accept' if not errors and full else ('[0] do not accept' if errors else '[1] for this platform; the outcome -- in the job control-map')}")
    block.append(f"EXIT={1 if errors else 0}")
    for x in block[:15]:
        out(x)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
