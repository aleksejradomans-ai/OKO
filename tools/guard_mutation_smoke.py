"""Мутационный smoke красной зоны для сторожа ослабления (B33 H2.7): известные порчи обязаны давать находку.

  python tools/guard_mutation_smoke.py [--root .]

База -- файлы рабочего дерева (в CI = проверяемый снимок); каждая мутация меняет ОДИН файл в памяти и прогоняет
src/weakening_guard.py (compare -- тестовые файлы, compare_guarded -- охраняемая зона) с ПУСТЫМ списком разрешений.
Мутация не поймана (ни одной находки нужного вида) -- код возврата 1. Контроль: без мутаций находок 0.
Мутации (аудит B32h, H2): три assert -> `assert True` в test_b08e_P4_volume_above_15_refused_before_send; у INV-19 в
docs/control_map.csv остаётся 1 тест из 5; произвольный known_gap у строки PASS (INV-05 у аудита, с B33 v2 -- INV-07); плюс `assert x or True`, удалённый тест,
новый skip, правки conftest.py / pytest.ini / workflow (`if: false` у шага) / config/net_direct_registry.json /
tools/verify_control_map.py / src/test_ci_gate.py, падение PASS в docs/CONTROL_MAP.md; B33 v2 попытка 2 (ревизор G2):
в test_b06d2 значение "111" подменено на pytest.param("abc", id="111") -- node ID тот же, вид changed_param.
Ничего не пишет на диск. Печатает `SMOKE <имя> caught=yes|no kinds=<виды>` и итог
`SMOKE_SUMMARY mutants=<n> caught=<n> control_findings=<n>`. Только стандартная библиотека."""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import weakening_guard as wg  # noqa: E402

RED_TEST = "src/test_b08e.py"
RED_FN = "test_b08e_P4_volume_above_15_refused_before_send"
FILES = (RED_TEST, "docs/control_map.csv", "docs/CONTROL_MAP.md", "conftest.py", "pytest.ini",
         ".github/workflows/pytest.yml", "config/net_direct_registry.json", "tools/verify_control_map.py",
         "src/test_ci_gate.py", "src/test_b06d2.py")
CM_STEP = "      - name: control map (both platforms) + map freshness\n"


def blob(text: str) -> str:
    b = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


def _fn(src: str, name: str = RED_FN):
    return next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == name)


def _replace_segments(src: str, nodes, make) -> str:
    """Замена исходного текста узлов (с конца, чтобы смещения не поплыли)."""
    lines = src.splitlines(keepends=True)
    offs = [0]
    for ln in lines:
        offs.append(offs[-1] + len(ln))
    out = src
    for n in sorted(nodes, key=lambda x: (x.lineno, x.col_offset), reverse=True):
        a = offs[n.lineno - 1] + len(lines[n.lineno - 1].encode("utf-8")[:n.col_offset].decode("utf-8"))
        b = offs[n.end_lineno - 1] + len(lines[n.end_lineno - 1].encode("utf-8")[:n.end_col_offset].decode("utf-8"))
        out = out[:a] + make(out[a:b]) + out[b:]
    return out


def m_assert_true(f: dict) -> dict:
    asserts = [n.test for n in ast.walk(_fn(f[RED_TEST])) if isinstance(n, ast.Assert)]
    assert len(asserts) == 3, f"{RED_FN}: expected 3 asserts, found {len(asserts)}"
    return {RED_TEST: _replace_segments(f[RED_TEST], asserts, lambda s: "True")}


def m_assert_or_true(f: dict) -> dict:
    a = [n.test for n in ast.walk(_fn(f[RED_TEST])) if isinstance(n, ast.Assert)][0]
    return {RED_TEST: _replace_segments(f[RED_TEST], [a], lambda s: f"({s}) or True")}


def m_remove_test(f: dict) -> dict:
    fn = _fn(f[RED_TEST])
    lines = f[RED_TEST].splitlines(keepends=True)
    return {RED_TEST: "".join(lines[: fn.lineno - 1] + lines[fn.end_lineno:])}


def m_new_skip(f: dict) -> dict:
    t = f[RED_TEST]
    head = f"def {RED_FN}("
    assert t.count(head) == 1
    return {RED_TEST: t.replace(head, '@pytest.mark.skip(reason="smoke")\n' + head)}


def _csv_edit(f: dict, edit) -> dict:
    rows = list(csv.reader(io.StringIO(f["docs/control_map.csv"])))
    hdr = rows[0]
    for r in rows[1:]:
        if r:
            edit(dict(zip(hdr, r)), r, hdr)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return {"docs/control_map.csv": buf.getvalue()}


def m_inv19_one_test(f: dict) -> dict:
    def ed(d, r, hdr):
        if d["id"] == "INV-19":
            tests = [x for x in d["tests"].split(";") if x.strip()]
            assert len(tests) == 5, f"INV-19: tests {len(tests)}, expected 5"
            r[hdr.index("tests")] = tests[0]
    return _csv_edit(f, ed)


GAP_ROW = "INV-07"     # аудит B32h H2 дописывал known_gap у INV-05; с B33 v2 у INV-05 он есть (C1) -> та же порча на INV-07


def m_inv05_known_gap(f: dict) -> dict:
    def ed(d, r, hdr):
        if d["id"] == GAP_ROW:
            assert not d["known_gap"].strip(), f"{GAP_ROW}: known_gap already present"
            r[hdr.index("known_gap")] = "arbitrary gap (smoke)"
    return _csv_edit(f, ed)


def m_pass_drop(f: dict) -> dict:
    t = f["docs/CONTROL_MAP.md"]
    n = wg._pass_count(t)
    assert n is not None and n > 0, "CONTROL_MAP.md: no line PASS N"
    return {"docs/CONTROL_MAP.md": t.replace(f"CI: PASS {n} ", f"CI: PASS {n - 1} ", 1)}


def m_workflow_if_false(f: dict) -> dict:
    t = f[".github/workflows/pytest.yml"]
    assert t.count(CM_STEP) == 1
    return {".github/workflows/pytest.yml": t.replace(CM_STEP, CM_STEP + "        if: false\n")}


def m_same_id_other_value(f: dict) -> dict:
    t = f["src/test_b06d2.py"]
    old = '-111, "111", 111.0'
    assert t.count(old) == 1, "test_b06d2: parametrize sample not found"
    return {"src/test_b06d2.py": t.replace(old, '-111, pytest.param("abc", id="111"), 111.0')}


def _append(path: str, tail: str):
    return lambda f: {path: f[path] + tail}


MUTANTS = [
    ("audit_assert_true_x3", m_assert_true, "fewer_asserts"),
    ("audit_inv19_one_of_five", m_inv19_one_test, "map_test_removed"),
    ("audit_inv05_known_gap", m_inv05_known_gap, "map_new_known_gap"),
    ("assert_or_true", m_assert_or_true, "fewer_asserts"),
    ("removed_red_test", m_remove_test, "removed_test"),
    ("new_skip_red_test", m_new_skip, "new_skip"),
    ("map_pass_drop", m_pass_drop, "map_pass_drop"),
    ("conftest_edit", _append("conftest.py", "\n# smoke\n"), "guarded_change"),
    ("pytest_ini_edit", _append("pytest.ini", "\naddopts = -p no:warnings\n"), "guarded_change"),
    ("workflow_step_if_false", m_workflow_if_false, "guarded_change"),
    ("net_registry_edit", _append("config/net_direct_registry.json", "\n"), "guarded_change"),
    ("verifier_edit", _append("tools/verify_control_map.py", "\n# smoke\n"), "guarded_change"),
    ("ci_gate_edit", _append("src/test_ci_gate.py", "\n# smoke\n"), "guarded_change"),
    ("b06d2_same_id_other_value", m_same_id_other_value, "changed_param"),
]


def findings(base: dict, head: dict) -> list:
    """base / head: {путь: текст} -> находки сторожа (тестовые файлы + охраняемая зона)."""
    texts = {}

    def idx(files):
        out = {}
        for p, t in files.items():
            b = blob(t)
            texts[b] = t
            out[p] = b
        return out
    bb, hb = idx(base), idx(head)
    tb = {p: (bb[p], t) for p, t in base.items() if wg.is_test_path(p)}
    th = {p: (hb[p], t) for p, t in head.items() if wg.is_test_path(p)}
    return sorted(set(wg.compare(tb, th)) | set(wg.compare_guarded(bb, hb, texts.__getitem__)))


def run(root: Path) -> tuple:
    base = {p: (root / p).read_text(encoding="utf-8-sig") for p in FILES}
    lines, caught = [], 0
    ctl = findings(base, dict(base))
    for name, mut, kind in MUTANTS:
        try:
            head = {**base, **mut(base)}
            got = findings(base, head)
            ok = any(g.startswith(kind + ":") for g in got) and wg.judge(got, {})["ok"] is False
            note = ",".join(sorted({g.split(":", 1)[0] for g in got})) or "-"
        except Exception as e:  # noqa: BLE001 -- мутацию не удалось построить = не поймана (красный)
            ok, note = False, f"error:{type(e).__name__}:{e}"
        caught += ok
        lines.append(f"SMOKE {name} caught={'yes' if ok else 'no'} want={kind} kinds={note}")
    lines.append(f"SMOKE_SUMMARY mutants={len(MUTANTS)} caught={caught} control_findings={len(ctl)}")
    return (0 if caught == len(MUTANTS) and not ctl else 1), lines


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):                    # B33 v2 попытка 3: вывод CLI всегда UTF-8 (и под -I на Windows)
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="guard_mutation_smoke")
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    try:
        rc, lines = run(Path(a.root))
    except OSError as e:
        print(f"SMOKE_ERROR file not readable: {type(e).__name__}")
        return 2
    for ln in lines:
        sys.stdout.buffer.write((ln + "\n").encode("ascii", "backslashreplace"))  # B33 attempt 4: output ASCII (non-ASCII data escaped)
    return rc


if __name__ == "__main__":
    sys.exit(main())
