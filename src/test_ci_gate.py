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
5. B27: каждый `uses:` в workflow закреплён полным 40-символьным SHA коммита действия с комментарием `# vX.Y.Z`
   (не тег: тег можно передвинуть); в обоих джобах -- одни и те же действия с одними и теми же SHA.
6. B33 H1: job control-map сверяется с эталоном ЦЕЛИКОМ (sha256 нормализованного блока job: строки без комментариев,
   без пустых, без пробелов в конце), а не подстроками; любое `if:` у job или у его шагов, кроме `always()`, --
   находка. Правка job = новый эталон здесь (этот файл -- в охраняемой зоне сторожа ослабления, нужна строка согласия).
   B33 H2.5 / H2.7: в обоих джобах pytest -- сторож с кодом базовой ревизии и мутационный smoke красной зоны.
"""
from __future__ import annotations

import ast
import hashlib
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
    assert sorted(jobs) == ["control-map", "pytest-linux", "pytest-windows"]      # B32h: + job control-map
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
    assert sorted(jobs) == ["control-map", "pytest-linux", "pytest-windows"]  # имена проверок защиты веток
    for plat, job in (("windows", jobs["pytest-windows"]), ("linux", jobs["pytest-linux"])):
        assert "uses: actions/setup-python@" in job and "cache: pip" in job
        assert "cache-dependency-path: |\n            requirements.txt\n            requirements.lock" in job
        src_cmd = [x.strip() for x in job.splitlines() if x.strip().startswith("python -m pytest src ")]
        assert len(src_cmd) == 1 and " --durations=30 " in src_cmd[0] and "--junitxml=pytest_src.xml" in src_cmd[0]
        i = job.index("uses: actions/upload-artifact@")
        step = job[job.rindex("- name:", 0, i):]
        assert "if: always()" in step and f"name: pytest-reports-{plat}" in step and "retention-days: 7" in step
        for f in ("pytest_src.txt", "pytest_src.xml", "pytest_extra.txt", "pytest_extra.xml"):
            assert f"            {f}" in step, f
        assert job.index("skipped tests = known list") < i                    # отчёты -- после всех проверок
    assert "secrets." not in _wf_text()


# --- B27: действия закреплены полным SHA -----------------------------------------------------------------------------
_USES_RE = re.compile(r"^\s*(?:- )?uses:\s*(\S+)(.*)$")
_PIN_RE = re.compile(r"^(actions/[a-z-]+)@([0-9a-f]{40})$")
_TAG_COMMENT_RE = re.compile(r"^\s+# v\d+\.\d+\.\d+$")


def _uses_lines(text: str) -> list:
    return [m.groups() for m in (_USES_RE.match(x) for x in text.splitlines() if not x.lstrip().startswith("#")) if m]


def test_every_uses_is_pinned_to_a_full_sha_with_tag_comment():
    uses = _uses_lines(_wf_text())
    assert len(uses) == 11                                          # 3 действия x 2 джоба pytest + 5 в control-map
    for ref, tail in uses:
        m = _PIN_RE.match(ref)
        assert m, f"не закреплено полным SHA: {ref}"
        assert _TAG_COMMENT_RE.match(tail), f"нет комментария # vX.Y.Z: {ref}{tail}"
    assert "@v4\n" not in _wf_text() and "@v5\n" not in _wf_text()


def test_both_jobs_use_the_same_pinned_actions():
    jobs = _job_blocks(_wf_text())
    per = {k: sorted(r + t for r, t in _uses_lines(v)) for k, v in jobs.items()}
    assert per["pytest-windows"] == per["pytest-linux"]
    names = sorted(_PIN_RE.match(r).group(1) for r, _ in _uses_lines(jobs["pytest-linux"]))
    assert names == ["actions/checkout", "actions/setup-python", "actions/upload-artifact"]
    majors = {_PIN_RE.match(r).group(1): t.strip()[3] for r, t in _uses_lines(jobs["pytest-linux"])}
    assert majors == {"actions/checkout": "4", "actions/setup-python": "5", "actions/upload-artifact": "4"}


@pytest.mark.parametrize("bad", [
    "      - uses: actions/checkout@v4",
    "      - uses: actions/checkout@11d5960a  # v4.4.0",
    "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
    "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af67726Z  # v4.4.0",
])
def test_pin_check_rejects_tags_short_sha_and_missing_comment(bad):
    (ref, tail), = _uses_lines(bad + "\n")
    assert not (_PIN_RE.match(ref) and _TAG_COMMENT_RE.match(tail))


# --- B32h: job control-map -- карта по JUnit ОБЕИХ платформ и свежесть docs/CONTROL_MAP.md -------------------------
_CM_STEP = "      - name: control map (both platforms) + map freshness\n"
_CM_ARGS = ("--junit windows=reports/windows/pytest_src.xml", "--junit windows=reports/windows/pytest_extra.xml",
            "--junit linux=reports/linux/pytest_src.xml", "--junit linux=reports/linux/pytest_extra.xml",
            "--check-md docs/CONTROL_MAP.md")


# B33 H1.1: эталон job control-map -- sha256 нормализованного блока (см. _cm_job_norm). Меняется только вместе с job.
_CM_JOB_SHA256 = "27ad709a1a5265fe681df8406d90d191d2847715a6369a743cb34eb65c071adc"  # B33 attempt 4: verdict line grep in English (ASCII rule)
_IF_RE = re.compile(r"^\s*(?:-\s+)?if:\s*(.*?)\s*$")


def _cm_job_norm(j: str) -> str:
    """Блок job (из _job_blocks: без строк-комментариев) -> строки без пробелов в конце, без пустых строк."""
    return "\n".join(x.rstrip() for x in j.splitlines() if x.strip())


def _cm_job_sha256(text: str) -> str:
    return hashlib.sha256(_cm_job_norm(_job_blocks(text).get("control-map", "")).encode("utf-8")).hexdigest()


def _control_map_job_problems(text: str, check_hash: bool = True) -> list:
    """Что в job control-map не так (пусто = в порядке): нужны оба джоба pytest, запуск даже после их провала, отчёты
    обеих платформ из их артефактов, сверка карты, код выхода верификатора = код шага, без поблажек и секретов.
    B33: блок job = эталон (sha256), любое условие `if:` у job и шагов -- только `always()`."""
    jobs = _job_blocks(text)
    if "control-map" not in jobs:
        return ["нет job control-map"]
    j = jobs["control-map"]
    p = []
    if check_hash and _cm_job_sha256(text) != _CM_JOB_SHA256:
        p.append("job control-map не совпадает с эталоном (sha256 нормализованного блока)")
    for x in j.splitlines():
        m = _IF_RE.match(x)
        if m and m.group(1) != "always()":
            p.append(f"условие не always(): {x.strip()}")
    if "needs: [pytest-windows, pytest-linux]" not in j:
        p.append("needs не оба джоба pytest")
    if "if: always()" not in j.split("steps:", 1)[0]:
        p.append("job не запускается после провала джоба pytest")
    for plat in ("windows", "linux"):
        if f"name: pytest-reports-{plat}\n          path: reports/{plat}" not in j:
            p.append(f"нет артефакта {plat}")
    flat = " ".join(x.strip().rstrip("\\").strip() for x in j.splitlines())
    for a in _CM_ARGS:
        if a not in flat:
            p.append(f"нет аргумента: {a}")
    if "rc=$?" not in j or "exit $rc" not in j:
        p.append("код выхода верификатора не становится кодом шага")
    if "both_platforms=${both} EXIT=${rc}" not in j:
        p.append("нет аннотации с итогом")
    for bad in ("continue-on-error", "|| true", "secrets.", "write-all", "permissions:"):
        if bad in j:
            p.append(f"запрещено в job: {bad}")
    return p


def test_control_map_job_checks_both_platforms_and_the_map():
    assert _control_map_job_problems(_wf_text()) == []
    assert "permissions:\n  contents: read\n" in _wf_text()                     # токен только на чтение (весь файл)


@pytest.mark.parametrize("old,new", [
    ("needs: [pytest-windows, pytest-linux]", "needs: [pytest-linux]"),
    ("    if: always()\n    runs-on: ubuntu-latest", "    runs-on: ubuntu-latest"),
    (" --junit linux=reports/linux/pytest_src.xml", ""),
    (" --check-md docs/CONTROL_MAP.md", ""),
    ("          exit $rc\n", "          exit 0\n"),
    ("    timeout-minutes: 10\n    env:\n      PYTHONIOENCODING: utf-8\n    ", "    timeout-minutes: 10\n    continue-on-error: true\n    env:\n      PYTHONIOENCODING: utf-8\n    "),
    # B33 H1.2: 4 порчи внешнего аудита 348e3b0 (§3 H1), которые прежний сторож подстрок пропускал
    (_CM_STEP, _CM_STEP + "        if: false\n"),
    ("> control_map_verify.txt 2>&1\n", "> control_map_verify.txt 2>&1 || :\n"),
    ("          exit $rc\n", "          rc=0\n          exit $rc\n"),
    ("    if: always()\n    runs-on: ubuntu-latest", "    if: always() && false\n    runs-on: ubuntu-latest"),
])
def test_liar_control_map_job_spoiled_is_caught(old, new):
    """Тест-лжец: испорченный job (один джоб в needs, нет always, одна платформа, нет сверки карты, код шага 0,
    continue-on-error; B33: if: false у шага, `|| :`, rc=0 перед exit, if: always() && false у job) обязан дать находку."""
    text = _wf_text()
    j0 = text.index("\n  control-map:\n")
    assert text.count(old, j0) == 1, old
    bad = text[:j0] + text[j0:].replace(old, new, 1)
    assert _control_map_job_problems(bad) != []


def test_control_map_job_equals_the_reference_hash():
    """B33 H1.1: job control-map целиком = эталон (а не набор подстрок)."""
    assert _cm_job_sha256(_wf_text()) == _CM_JOB_SHA256
    assert _cm_job_norm("  a: 1  \n\n  b: 2\n") == "  a: 1\n  b: 2"


@pytest.mark.parametrize("old,new", [
    (_CM_STEP, _CM_STEP + "        if: false\n"),
    (_CM_STEP, _CM_STEP + "        if: ${{ github.run_attempt > 9 }}\n"),
    ("    if: always()\n    runs-on: ubuntu-latest", "    if: always() && false\n    runs-on: ubuntu-latest"),
    ("    if: always()\n    runs-on: ubuntu-latest", "    if: success()\n    runs-on: ubuntu-latest"),
])
def test_liar_control_map_conditions_caught_even_without_the_hash(old, new):
    """Правило «условие только always()» действует само по себе (не только через эталон)."""
    text = _wf_text()
    j0 = text.index("\n  control-map:\n")
    bad = text[:j0] + text[j0:].replace(old, new, 1)
    assert bad != text
    assert any(x.startswith("условие не always()") for x in _control_map_job_problems(bad, check_hash=False))
    assert _control_map_job_problems(text, check_hash=False) == []


def test_workflow_line1_names_three_required_jobs():
    """B33 H1.4: первая строка workflow -- три job, все обязательные (required check control-map включён 24.11)."""
    first = _wf_text().splitlines()[0]
    assert "three jobs, all required" in first and "two jobs" not in first


def test_both_pytest_jobs_run_the_base_revision_guard_and_the_mutation_smoke():
    """B33 H2.5 / H2.7: в обоих джобах pytest -- сторож с кодом родителя (git show HEAD~1:...) и smoke красной зоны."""
    jobs = _job_blocks(_wf_text())
    for name in ("pytest-windows", "pytest-linux"):
        j = jobs[name]
        i = j.index("- name: test weakening guard (code of the published base)")
        step = j[i:j.index("- name:", i + 1)]
        assert "shell: python" in step and "if: always()" in step
        assert '"git", "show", base + ":" + rel' in step and '"merge-base", "HEAD", "refs/remotes/origin/oko-live"' in step   # B33 v2 C3
        assert '("src/weakening_guard.py", "tools/weakening_guard.py")' in step
        assert '"--base", base' in step and '"--head", "HEAD"' in step and "sys.exit(subprocess.run(cmd).returncode)" in step
        assert "sys.exit(2)" in step                                                   # нет опубликованной базы -> красный
        assert "run: python tools/weakening_guard.py --base auto --head HEAD --collect --allow .github/test_weakening_allow.txt" in j
        k = j.index("- name: guard mutation smoke (red zone)")
        step2 = j[k:j.index("- name:", k + 1)]
        assert "run: python tools/guard_mutation_smoke.py --root ." in step2 and "if: always()" in step2
        assert k < j.index("- name: pytest src")


def test_b33v2_checkout_full_history_and_published_base():
    """B33 v2 C3 (B32m D1): fetch-depth 0 во всех трёх job; верификатор -- база auto + обязательна; HEAD~1 нигде."""
    text = _wf_text()
    assert text.count("fetch-depth: 0") == 3 and "fetch-depth: 2" not in text
    assert "HEAD~1" not in "\n".join(x for x in text.splitlines() if not x.lstrip().startswith("#"))
    jobs = _job_blocks(text)
    for name in ("pytest-windows", "pytest-linux", "control-map"):
        assert "--base-rev auto --require-base" in " ".join(x.strip().rstrip("\\").strip() for x in jobs[name].splitlines()), name
    for name in ("pytest-windows", "pytest-linux"):
        assert "run: python tools/impl_mutation_smoke.py --root ." in jobs[name]
