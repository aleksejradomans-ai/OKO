"""CI как барьер: тесты ядра не пропускаются молча, список разрешённых пропусков честен.

1. Модули проекта (bot, router, governor, tv_*, registry, staging_manager, sandbox_talk, …) в тестах импортируются
   обычным import: сломанный импорт -- красный тест. pytest.importorskip остаётся только для необязательных
   сторонних библиотек (PyQt6, docx, openpyxl, fitz, PIL, numpy, chromadb, pyloudnorm) и для одного исключения
   (EXCEPTIONS): его пропуск в CI всё равно красный -- пропуска нет в known_skips.
2. .github/known_skips.txt (windows-latest) и .github/known_skips_linux.txt (ubuntu-latest, B25): строка
   «модуль::тест | причина»; каждый тест из списка существует, и причина есть в его файле. Шаг workflow каждого джоба
   сверяет фактические пропуски со списком своей платформы (неожиданный или пропавший пропуск -- ошибка).
3. B25: два джоба (pytest-windows, pytest-linux) с одними и теми же командами pytest; список linux = список windows +
   только тесты, пропускаемые вне Windows по sys.platform (skipif с причиной), -- без ослабления.
4. B26: прогон -- на push в ветки ci/** и вручную (workflow_dispatch), не на push в опубликованные ветки и не на
   pull_request; один прогон на коммит (concurrency по github.sha, без отмены); в обоих джобах -- кэш pip по
   requirements.txt + requirements.lock, --durations=30 у pytest src, отчёты тестов -- артефакт (if: always(),
   имя с платформой, хранение 7 дней). Имена джобов не меняются (проверки защиты веток привязаны к ним).
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


SKIP_LISTS = ("known_skips.txt", "known_skips_linux.txt")      # windows-latest, ubuntu-latest (B25)


def _known_skips(name: str = "known_skips.txt"):
    p = ROOT / ".github" / name
    if not p.is_file():
        pytest.skip(f"нет .github/{name} (дерево без каталога CI)")
    return [x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]


@pytest.mark.parametrize("name", SKIP_LISTS)
def test_known_skips_lines_are_well_formed_and_unique(name):
    lines = _known_skips(name)
    assert lines == sorted(lines) and len(set(lines)) == len(lines)
    for ln in lines:
        assert re.fullmatch(r"[\w.]+::[\w\[\]\-., ]+ \| .+", ln), ln


@pytest.mark.parametrize("name", SKIP_LISTS)
def test_known_skips_point_to_existing_tests_with_that_reason(name):
    for ln in _known_skips(name):
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


# --- B25: Linux-джоб рядом с Windows ---------------------------------------------------------------------------------
def _skipif_win_only(ident: str) -> bool:
    """Тест помечен skipif(sys.platform != "win32", reason=...) -- пропуск только вне Windows."""
    module, name = ident.split("::", 1)
    text = (ROOT / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8-sig")
    tree = ast.parse(text)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name.split("[")[0]:
            for d in n.decorator_list:
                src = ast.unparse(d)
                if "skipif" in src and "sys.platform != 'win32'" in src:
                    return True
    return False


def test_linux_skips_are_windows_skips_plus_windows_only_tests():
    win, lin = set(_known_skips("known_skips.txt")), set(_known_skips("known_skips_linux.txt"))
    assert win <= lin, f"в linux нет пропусков windows: {sorted(win - lin)}"
    extra = sorted(lin - win)
    assert extra, "linux-список не отличается от windows: тесты только для Windows не учтены"
    for ln in extra:
        ident, reason = ln.split(" | ", 1)
        assert _skipif_win_only(ident), f"{ident}: в linux-списке, но не skipif(sys.platform != 'win32')"
        assert "Windows" in reason, ln


def _job_blocks(text: str) -> dict:
    jobs = text.split("\njobs:\n", 1)[1]
    res, cur = {}, None
    for line in jobs.splitlines():
        m = re.match(r"^  ([\w-]+):$", line)
        if m:
            cur = m.group(1)
            res[cur] = []
        elif cur and not line.strip().startswith("#"):    # комментарии между джобами -- не часть джоба
            res[cur].append(line)
    return {k: "\n".join(v) for k, v in res.items()}


def test_workflow_has_windows_and_linux_jobs_with_the_same_commands():
    wf = ROOT / ".github" / "workflows" / "pytest.yml"
    if not wf.is_file():
        pytest.skip("нет .github/workflows/pytest.yml (дерево без каталога CI)")
    jobs = _job_blocks(wf.read_text(encoding="utf-8"))
    assert sorted(jobs) == ["pytest-linux", "pytest-windows"]
    w, li = jobs["pytest-windows"], jobs["pytest-linux"]
    assert "name: pytest-windows" in w and "runs-on: windows-latest" in w
    assert "name: pytest-linux" in li and "runs-on: ubuntu-latest" in li
    assert "known_skips.txt" in w and "known_skips_linux.txt" not in w
    assert "known_skips_linux.txt" in li and ".github/known_skips.txt " not in li
    for job in (w, li):
        assert 'python-version: "3.12"' in job and "known_xfail.txt" in job
        assert "UNEXPECTED SKIP" in job and "MISSING SKIP" in job
    cmds = lambda j: sorted(x.strip() for x in j.splitlines() if x.strip().startswith("python -m p"))
    assert cmds(w) == cmds(li) and len(cmds(w)) == 3      # pip install + pytest src + pytest root/tools


# --- B26: запуск на ci/**, один прогон на коммит, кэш pip, --durations, артефакты ------------------------------------
def _wf_text():
    wf = ROOT / ".github" / "workflows" / "pytest.yml"
    if not wf.is_file():
        pytest.skip("нет .github/workflows/pytest.yml (дерево без каталога CI)")
    return wf.read_text(encoding="utf-8")


def _top_block(text: str, key: str) -> str:
    m = re.search(rf"^{key}:\n((?:[ #].*\n|\n)*?)(?=^\S)", text, re.M)
    assert m, f"нет блока {key}:"
    return m.group(1)


def test_workflow_runs_on_ci_branches_and_by_hand_only():
    text = _wf_text()
    on = _top_block(text, "on")
    lines = [x.rstrip() for x in on.splitlines() if x.strip() and not x.strip().startswith("#")]
    assert lines == ["  push:", "    branches: ['ci/**']", "  workflow_dispatch:"], lines
    assert "pull_request" not in on and "oko-live" not in on and "main" not in on


def test_workflow_one_run_per_commit_not_cancelled():
    conc = _top_block(_wf_text(), "concurrency")
    assert "group: pytest-${{ github.sha }}" in conc and "cancel-in-progress: false" in conc
    assert "github.ref" not in conc


def test_both_jobs_cache_pip_durations_and_upload_reports():
    jobs = _job_blocks(_wf_text())
    assert sorted(jobs) == ["pytest-linux", "pytest-windows"]               # имена проверок защиты веток
    for plat, job in (("windows", jobs["pytest-windows"]), ("linux", jobs["pytest-linux"])):
        assert "uses: actions/setup-python@v5" in job and "cache: pip" in job
        assert "cache-dependency-path: |\n            requirements.txt\n            requirements.lock" in job
        src_cmd = [x.strip() for x in job.splitlines() if x.strip().startswith("python -m pytest src ")]
        assert len(src_cmd) == 1 and " --durations=30 " in src_cmd[0] and "--junitxml=pytest_src.xml" in src_cmd[0]
        i = job.index("uses: actions/upload-artifact@v4")
        step = job[job.rindex("- name:", 0, i):]
        assert "if: always()" in step and f"name: pytest-reports-{plat}" in step and "retention-days: 7" in step
        for f in ("pytest_src.txt", "pytest_src.xml", "pytest_extra.txt", "pytest_extra.xml"):
            assert f"            {f}" in step, f
        assert job.index("skipped tests = known list") < i                    # отчёты -- после всех проверок
    assert "secrets." not in _wf_text()
