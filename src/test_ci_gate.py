"""CI как барьер: тесты ядра не пропускаются молча, список разрешённых пропусков честен.

1. Модули проекта (bot, router, governor, tv_*, registry, staging_manager, sandbox_talk, …) в тестах импортируются
   обычным import: сломанный импорт -- красный тест. pytest.importorskip остаётся только для необязательных
   сторонних библиотек (PyQt6, docx, openpyxl, fitz, PIL, numpy, chromadb, pyloudnorm) и для одного исключения
   (EXCEPTIONS): его пропуск в CI всё равно красный -- пропуска нет в known_skips.
2. .github/known_skips.txt: строка «модуль::тест | причина»; каждый тест из списка существует, и причина есть в его
   файле. Шаг workflow сверяет фактические пропуски со списком (неожиданный или пропавший пропуск -- ошибка).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
OPTIONAL_LIBS = {"PyQt6.QtWidgets", "docx", "openpyxl", "fitz", "PIL", "numpy", "chromadb", "pyloudnorm"}
# (файл теста, модуль): тест входит в список публичного экспорта, модуль -- нет (сторож экспорта считает import по AST).
EXCEPTIONS = {("test_menu_syntax.py", "router")}


def _importorskips():
    for f in sorted(SRC.glob("test_*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "importorskip"
                    and n.args and isinstance(n.args[0], ast.Constant)):
                yield f.name, n.lineno, str(n.args[0].value)


def test_importorskip_only_for_optional_third_party_libraries():
    bad = [(f, line, mod) for f, line, mod in _importorskips() if mod not in OPTIONAL_LIBS and (f, mod) not in EXCEPTIONS]
    assert bad == [], f"importorskip для модуля проекта или неизвестной библиотеки: {bad}"


def test_no_project_module_is_importorskipped():
    project = {p.stem for p in SRC.glob("*.py") if not p.name.startswith("test_")}
    project |= {p.stem for p in (ROOT / "oko_rag").glob("*.py")} if (ROOT / "oko_rag").is_dir() else set()
    hit = [(f, line, mod) for f, line, mod in _importorskips() if mod.split(".")[0] in project and (f, mod) not in EXCEPTIONS]
    assert hit == []
    assert {(f, mod) for f, _, mod in _importorskips() if mod.split(".")[0] in project} == EXCEPTIONS   # ровно одно


def _known_skips():
    p = ROOT / ".github" / "known_skips.txt"
    if not p.is_file():
        pytest.skip("нет .github/known_skips.txt (дерево без каталога CI)")
    return [x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]


def test_known_skips_lines_are_well_formed_and_unique():
    lines = _known_skips()
    assert lines == sorted(lines) and len(set(lines)) == len(lines)
    for ln in lines:
        assert re.fullmatch(r"[\w.]+::[\w\[\]\-., ]+ \| .+", ln), ln


def test_known_skips_point_to_existing_tests_with_that_reason():
    for ln in _known_skips():
        ident, reason = ln.split(" | ", 1)
        module, name = ident.split("::", 1)
        f = ROOT / (module.replace(".", "/") + ".py")
        assert f.is_file(), f"{module}: файла теста нет"
        text = f.read_text(encoding="utf-8-sig")
        assert re.search(rf"^def {re.escape(name.split('[')[0])}\(", text, re.M), f"{ident}: теста нет"
        assert reason in text, f"{ident}: причины пропуска нет в файле теста"


def test_workflow_checks_skips_and_runs_the_root_tests():
    wf = ROOT / ".github" / "workflows" / "pytest.yml"
    if not wf.is_file():
        pytest.skip("нет .github/workflows/pytest.yml (дерево без каталога CI)")
    text = wf.read_text(encoding="utf-8")
    assert "known_skips.txt" in text and "UNEXPECTED SKIP" in text and "MISSING SKIP" in text
    assert "--junitxml=pytest_src.xml" in text and "--junitxml=pytest_extra.xml" in text
    for f in ("test_data_hygiene.py", "tools/test_calibrate_sigma.py", "tools/test_check_sigma_2e.py"):
        assert f in text and (ROOT / f).is_file()
    assert "permissions:\n  contents: read" in text and "secrets." not in text
