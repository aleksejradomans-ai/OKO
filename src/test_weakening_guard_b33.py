"""B33 H2: сторож ослабления -- константные assert, охраняемая зона, карта, строки согласия с entry_sha16, запуск кодом
базовой ревизии (как шаг CI) и мутационный smoke красной зоны (tools/guard_mutation_smoke.py)."""
from __future__ import annotations

import ast
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import weakening_guard as wg
# B33 attempt 4 (item 3): every child process of this test file gets a hermetic environment -- without the parent
# variables that change Python / pytest / git (selfrun / Operator profile: PYTHONHOME, PYTHONPATH, PYTEST_ADDOPTS,
# OKO_B33_*, OKO_NOTIFY_OFF, GIT_DIR ...), with UTF-8 stdio. tools/build_contour_guard.py: bare_child is red here.
_DIRTY_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH", "PYTHONWARNINGS",
              "PYTHONINSPECT", "PYTHONEXECUTABLE", "PYTHONNOUSERSITE", "PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST",
              "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG", "OKO_NOTIFY_OFF", "OKO_SIGMA_LIVE",
              "OKO_REQUEST_TS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CONFIG_PARAMETERS",
              "GIT_CONFIG_COUNT", "GIT_CEILING_DIRECTORIES")


def _henv(**extra) -> dict:
    import os as _os
    env = {k: v for k, v in _os.environ.items() if k.upper() not in _DIRTY_ENV
           and not k.upper().startswith(("OKO_B33_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    env.update(extra)
    return env


def _hrun(*a, **k):
    k.setdefault("env", _henv())
    return subprocess.run(*a, **k)


ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "tools" / "weakening_guard.py"
SMOKE = ROOT / "tools" / "guard_mutation_smoke.py"
P = "src/test_x.py"
BASE = "def test_a():\n    x = 1\n    assert x == 1\n    assert x > 0\n"
E1, E2 = "0123456789abcdef", "fedcba9876543210"


def one(head: str, base: str = BASE) -> list:
    return wg.compare({P: ("b1", base)}, {P: ("b2", head)})


# ------------------------------------------------------------------ H2.1 константные assert
@pytest.mark.parametrize("const", ["True", "1", '"x"', "x or True", "(x, 'msg')", "True and 1", "not 0", "[1]"])
def test_b33_constant_assert_counts_as_removed(const):
    head = BASE.replace("    assert x > 0\n", f"    assert {const}\n")
    assert one(head) == [f"fewer_asserts:{P}::test_a"]


@pytest.mark.parametrize("real", ["x >= 1", "x or y", "not x", "x and True", "(x)"])
def test_b33_real_assert_still_counts(real):
    head = BASE.replace("    assert x > 0\n", f"    assert {real}\n")
    assert one(head) == []


def test_b33_assert_false_still_a_check():
    assert wg.analyze("def test_f():\n    assert False\n")["test_f"]["asserts"] == 1
    assert wg.analyze("def test_t():\n    assert True\n")["test_t"]["asserts"] == 0


# ------------------------------------------------------------------ H2.2 / H2.3 охраняемая зона
GUARDED = ["conftest.py", "src/conftest.py", "a/b/conftest.py", "pytest.ini", "docs/control_map.csv",
           "config/net_direct_registry.json", ".github/workflows/pytest.yml", ".github/workflows/new.yml",
           "src/weakening_guard.py", "tools/weakening_guard.py", "tools/verify_control_map.py", "src/test_ci_gate.py",
           "tools/guard_mutation_smoke.py"]


@pytest.mark.parametrize("path", GUARDED)
def test_b33_guarded_paths_changed_added_deleted_are_findings(path):
    assert wg.is_guarded_path(path)
    b, h = "1" * 40, "2" * 40
    read = {b: "a", h: "b"}.__getitem__
    assert wg.compare_guarded({path: b}, {path: h}, read) == [f"guarded_change:{path}::<module>@{'2' * 12}"]
    assert wg.compare_guarded({}, {path: h}, read) == [f"guarded_change:{path}::<module>@{'2' * 12}"]
    if path not in ("docs/control_map.csv",):
        assert wg.compare_guarded({path: b}, {}, read) == [f"guarded_change:{path}::<module>@deleted"]
    assert wg.compare_guarded({path: b}, {path: b}, read) == []


@pytest.mark.parametrize("path", ["src/bot.py", "docs/RUNBOOK.md", ".github/known_skips.txt",
                                  ".github/guarded_change_allow.txt", "tools/notify.py", "src/test_notify.py"])
def test_b33_paths_outside_the_zone_are_not_guarded(path):
    assert not wg.is_guarded_path(path)
    assert wg.compare_guarded({path: "1" * 40}, {path: "2" * 40}, lambda b: "") == []


CSV = ("id,implementation,tests,evidence,scope,known_gap\n"
       "INV-05,src/a.py::f,src/test_a.py::t1;src/test_a.py::t2,behavior,s,\n"
       "INV-19,src/b.py::g,src/test_b.py::u1;src/test_b.py::u2;src/test_b.py::u3,behavior,s,старый пробел\n")


def _csv_find(new: str) -> list:
    b, h = "a" * 40, "b" * 40
    return wg.compare_guarded({"docs/control_map.csv": b}, {"docs/control_map.csv": h}, {b: CSV, h: new}.__getitem__)


def test_b33_map_test_removed_and_new_known_gap():
    new = CSV.replace("src/test_b.py::u1;src/test_b.py::u2;src/test_b.py::u3", "src/test_b.py::u1")
    new = new.replace("src/test_a.py::t2,behavior,s,", "src/test_a.py::t2,behavior,s,произвольный пробел")
    got = _csv_find(new)
    tag = "b" * 12
    assert got == sorted([f"guarded_change:docs/control_map.csv::<module>@{tag}",
                          f"map_new_known_gap:docs/control_map.csv::INV-05@{tag}",
                          f"map_test_removed:docs/control_map.csv::INV-19@{tag}"])


def test_b33_map_row_removed_and_new_row_with_gap():
    new = CSV.split("INV-19")[0] + "INV-20,src/c.py::h,src/test_c.py::v,behavior,s,пробел\n"
    got = {g.split(":", 1)[0] + ":" + g.split("::", 1)[1].split("@")[0] for g in _csv_find(new)}
    assert {"map_test_removed:INV-19", "map_new_known_gap:INV-20", "guarded_change:<module>"} == got


def test_b33_map_added_test_and_kept_gap_are_not_weakening():
    new = CSV.replace("src/test_a.py::t2,", "src/test_a.py::t2;src/test_a.py::t3,")
    assert [g for g in _csv_find(new) if not g.startswith("guarded_change:")] == []


MD = "# Карта\nСтатус по JUnit обеих платформ CI: PASS {n} · GAP 10 · MANUAL 1 · FAIL 0; kernel codes: PASS.\n"


@pytest.mark.parametrize("was,now,drop", [(10, 9, True), (10, 10, False), (10, 11, False), (10, None, True)])
def test_b33_map_pass_drop(was, now, drop):
    b, h = "c" * 40, "d" * 40
    txt = {b: MD.format(n=was), h: MD.format(n=now) if now is not None else "# Карта без строки статуса\n"}
    got = wg.compare_guarded({"docs/CONTROL_MAP.md": b}, {"docs/CONTROL_MAP.md": h}, txt.__getitem__)
    assert got == ([f"map_pass_drop:docs/CONTROL_MAP.md::<module>@{'d' * 12}"] if drop else [])


# ------------------------------------------------------------------ H2.4 строка согласия
def test_b33_allow_line_needs_entry_sha16_and_unique_values():
    ok = [f"fewer_asserts:src/test_x.py::test_a | r | 2026-11-24 | b33_x | {E1}",
          f"guarded_change:pytest.ini::<module>@{'a' * 12} | r | 2026-11-24 | b33_ini | {E2}",
          f"map_test_removed:docs/control_map.csv::INV-19@{'b' * 12} | r | 2026-11-24 | b33_map | {'1' * 16}"]
    allow, errs = wg.parse_allow(ok)
    assert errs == [] and len(allow) == 3 and allow[ok[0].split(" | ")[0]][3] == E1


@pytest.mark.parametrize("lines", [
    ["fewer_asserts:src/test_x.py::test_a | r | 2026-11-24 | b33_x"],                                  # 4 поля
    [f"fewer_asserts:src/test_x.py::test_a | r | 2026-11-24 | b33_x | {E1[:15]}"],                     # 15 hex
    [f"fewer_asserts:src/test_x.py::test_a | r | 2026-11-24 | b33_x | {E1.upper()}"],                  # заглавные
    [f"guarded_change:pytest.ini::<module> | r | 2026-11-24 | b33_x | {E1}"],                          # без @blob
    [f"guarded_change:pytest.ini::<module>@abc | r | 2026-11-24 | b33_x | {E1}"],                      # короткий blob
    [f"fewer_asserts:src/test_x.py::test_a | r | 2026-11-24 | b33_x | {E1}",
     f"fewer_asserts:src/test_x.py::test_a | r2 | 2026-11-24 | b33_y | {E2}"],                          # повтор ключа
    [f"fewer_asserts:src/test_x.py::test_a | r | 2026-11-24 | b33_x | {E1}",
     f"fewer_asserts:src/test_x.py::test_b | r | 2026-11-24 | b33_y | {E1}"],                           # повтор sha16
])
def test_b33_liar_bad_allow_lines_are_errors(lines):
    allow, errs = wg.parse_allow(lines)
    assert len(errs) == 1


# ------------------------------------------------------------------ CLI на git (как шаг CI)
def _git(d, *a):
    return _hrun(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *a], check=True, capture_output=True).stdout.decode("utf-8").strip()


def _repo(tmp_path, base: dict, head: dict) -> Path:
    d = tmp_path / "r"
    d.mkdir()
    _git(d, "init", "-q")
    for n, b in base.items():
        (d / n).parent.mkdir(parents=True, exist_ok=True)
        (d / n).write_text(b, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "base")
    for n, b in head.items():
        (d / n).parent.mkdir(parents=True, exist_ok=True)
        (d / n).write_text(b, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "head")
    return d


def _cli(d, cli=CLI, *extra):
    r = _hrun([sys.executable, "-I", str(cli), "--repo", str(d), *extra], capture_output=True)
    return r.returncode, r.stdout.decode("utf-8")


def test_b33_cli_guarded_change_red_without_line_green_with_line(tmp_path):
    d = _repo(tmp_path, {"pytest.ini": "[pytest]\n", P: BASE}, {"pytest.ini": "[pytest]\naddopts = -x\n"})
    rc, out = _cli(d)
    blob = _git(d, "rev-parse", "HEAD:pytest.ini")[:12]
    key = f"guarded_change:pytest.ini::<module>@{blob}"
    assert rc == 1 and f"WEAKENING red {key}" in out and "guarded=1" in out
    (d / ".github").mkdir()
    (d / ".github" / "guarded_change_allow.txt").write_text(f"{key} | B33 test | 2026-11-24 | b33_ini | {E1}\n",
                                                            encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "allow")
    rc, out = _cli(d, CLI, "--base", "HEAD~2", "--head", "HEAD")
    assert rc == 0 and f"WEAKENING allowed {key}" in out and " red=0 " in out, out


def test_b33_cli_line_for_an_older_version_does_not_allow_a_new_change(tmp_path):
    d = _repo(tmp_path, {"pytest.ini": "[pytest]\n"}, {"pytest.ini": "[pytest]\naddopts = -x\n",
                                                       ".github/guarded_change_allow.txt":
                                                           f"guarded_change:pytest.ini::<module>@{'0' * 12} | old | 2026-11-24 | b33_ini | {E1}\n"})
    rc, out = _cli(d)
    assert rc == 1 and "WEAKENING red guarded_change:pytest.ini::<module>@" in out


def test_b33_cli_duplicate_entry_sha16_across_the_two_files_is_an_error(tmp_path):
    d = _repo(tmp_path, {"pytest.ini": "[pytest]\n"}, {
        ".github/test_weakening_allow.txt": f"fewer_asserts:src/test_x.py::test_a | r | 2026-11-24 | b33_x | {E1}\n",
        ".github/guarded_change_allow.txt": f"guarded_change:pytest.ini::<module>@{'0' * 12} | r | 2026-11-24 | b33_y | {E1}\n"})
    rc, out = _cli(d)
    assert rc == 2 and "WEAKENING_ALLOW_ERROR" in out and "entry_sha16" in out


def test_b33_cli_constant_assert_in_red_zone_test_is_red(tmp_path):
    src = (ROOT / "src" / "test_b08e.py").read_text(encoding="utf-8")
    sm = _load_smoke()
    bad = sm.m_assert_true({sm.RED_TEST: src})[sm.RED_TEST]
    d = _repo(tmp_path, {"src/test_b08e.py": src}, {"src/test_b08e.py": bad})
    rc, out = _cli(d)
    assert rc == 1 and f"WEAKENING red fewer_asserts:src/test_b08e.py::{sm.RED_FN}" in out


def test_b33_guard_runs_from_a_copy_of_two_files_like_the_ci_base_step(tmp_path):
    """Шаг CI H2.5 кладёт ровно src/weakening_guard.py и tools/weakening_guard.py базовой ревизии во временную папку и
    запускает оттуда: копия обязана работать без остального дерева."""
    g = tmp_path / "guard_base"
    (g / "src").mkdir(parents=True)
    (g / "tools").mkdir()
    shutil.copyfile(ROOT / "src" / "weakening_guard.py", g / "src" / "weakening_guard.py")
    shutil.copyfile(CLI, g / "tools" / "weakening_guard.py")
    d = _repo(tmp_path, {"pytest.ini": "[pytest]\n", P: BASE}, {P: BASE.replace("    assert x > 0\n", "    assert True\n")})
    rc, out = _cli(d, g / "tools" / "weakening_guard.py")
    assert rc == 1 and f"WEAKENING red fewer_asserts:{P}::test_a" in out and "WEAKENING_SUMMARY" in out


# ------------------------------------------------------------------ H2.7 мутационный smoke
def _load_smoke(name: str = "guard_mutation_smoke_b33"):
    spec = importlib.util.spec_from_file_location(name, SMOKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_b33_mutation_smoke_catches_every_mutant_on_this_tree():
    sm = _load_smoke()
    rc, lines = sm.run(ROOT)
    assert rc == 0, lines
    assert lines[-1] == f"SMOKE_SUMMARY mutants={len(sm.MUTANTS)} caught={len(sm.MUTANTS)} control_findings=0"
    names = {x.split()[1] for x in lines[:-1]}
    assert {"audit_assert_true_x3", "audit_inv19_one_of_five", "audit_inv05_known_gap"} <= names
    assert len(sm.MUTANTS) >= 13


def test_b33_liar_smoke_turns_red_when_the_guard_ignores_constant_asserts(monkeypatch):
    """Тест-лжец: сторож без правила H2.1 (assert True считается проверкой) -> smoke обязан покраснеть."""
    sm = _load_smoke("guard_mutation_smoke_b33_liar")
    monkeypatch.setattr(sm.wg, "_always_true", lambda n: False)
    rc, lines = sm.run(ROOT)
    assert rc == 1 and any(x.startswith("SMOKE audit_assert_true_x3 caught=no") for x in lines)


def test_b33_liar_smoke_turns_red_without_the_guarded_zone(monkeypatch):
    sm = _load_smoke("guard_mutation_smoke_b33_liar2")
    monkeypatch.setattr(sm.wg, "compare_guarded", lambda b, h, r: [])
    rc, lines = sm.run(ROOT)
    assert rc == 1 and sum(1 for x in lines if " caught=no " in x) >= 9


def test_b33_smoke_cli_prints_summary(tmp_path):
    r = _hrun([sys.executable, "-I", str(SMOKE), "--root", str(ROOT)], capture_output=True)
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "SMOKE_SUMMARY mutants=" in out and " caught=no " not in out


# ------------------------------------------------------------------ B33 v2 C3 (B32m D1): база = опубликованный коммит
RED_T = "def test_keep():\n    assert 1 == 1\n\n\ndef test_live1_gated():\n    x = 2\n    assert x == 2\n"


def _published_then_two_commits(tmp_path):
    """Сценарий B32m D1: опубликовано P; коммит A удаляет тест; коммит B -- строка README."""
    d = _repo(tmp_path, {P: RED_T, "README.md": "x\n"}, {P: RED_T.split("\n\n\ndef test_live1_gated")[0] + "\n"})
    _git(d, "update-ref", "refs/remotes/origin/oko-live", "HEAD~1")              # опубликован P = base
    (d / "README.md").write_text("x\ny\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "B readme")
    return d


def test_b33v2_liar_two_commits_hide_removed_test_only_with_head1(tmp_path):
    d = _published_then_two_commits(tmp_path)
    rc1, out1 = _cli(d, CLI, "--base", "HEAD~1")                                   # прежний CI: видит только B
    assert rc1 == 0 and "findings=0" in out1
    rc, out = _cli(d, CLI, "--base", "auto")                                       # B33 v2: от опубликованного
    assert rc == 1 and f"WEAKENING red removed_test:{P}::test_live1_gated" in out and "WEAKENING_BASE auto" in out


def test_b33v2_base_auto_without_published_branch_is_red(tmp_path):
    d = _repo(tmp_path, {P: BASE}, {P: BASE + "\n"})
    rc, out = _cli(d, CLI, "--base", "auto")
    assert rc == 2 and "WEAKENING_ERROR base auto" in out


# ------------------------------------------------------------------ B33 v2 C4 (B32m D2): собранные node ID
PARAM = ('import pytest\n\n\n@pytest.mark.parametrize("uid", ["111", "abc", 1.0])\ndef test_only_int(uid):\n'
         '    assert not isinstance(uid, int)\n')


def test_b33v2_compare_cases_unit():
    base = ["src/test_b06d2.py::test_d2_only_exact_int_id_is_host[111]", "src/test_b06d2.py::test_d2_only_exact_int_id_is_host[abc]",
            "src/test_k.py::TestK::test_m", "src/test_mv.py::test_moved"]
    head = ["src/test_b06d2.py::test_d2_only_exact_int_id_is_host[abc]", "src/test_k.py::TestK::test_m", "src/test_new.py::test_moved"]
    assert wg.compare_cases(base, head) == ["removed_test:src/test_b06d2.py::test_d2_only_exact_int_id_is_host[111]"]
    assert wg.compare_cases(base, base) == []
    allow, errs = wg.parse_allow([f"removed_test:src/test_b06d2.py::test_d2_only_exact_int_id_is_host[111] | r | 2026-11-24 | b33_x | {E1}"])
    assert errs == [] and len(allow) == 1


def test_b33v2_liar_narrowed_parametrize_is_red_only_with_collect(tmp_path):
    """Сценарий B32m D2: из parametrize убран "111" (тот самый, что ловил регресс) -- AST-сторож молчит, сборка видит."""
    p = "src/test_b06d2.py"
    d = _repo(tmp_path, {p: PARAM, "pytest.ini": "[pytest]\ntestpaths = src\n"}, {p: PARAM.replace('["111", "abc", 1.0]', '["abc", 1.0]')})
    rc0, out0 = _cli(d)                                                            # попытка 2: AST видит значения
    assert rc0 == 1 and f"WEAKENING red changed_param:{p}::test_only_int" in out0 and "removed_test" not in out0
    rc, out = _cli(d, CLI, "--collect")
    assert rc == 1 and f"WEAKENING red removed_test:{p}::test_only_int[111]" in out and "WEAKENING_COLLECT" in out
    assert f"WEAKENING red changed_param:{p}::test_only_int" in out


def test_b33v2_collect_failure_is_red(tmp_path):
    p = "src/test_x.py"
    d = _repo(tmp_path, {p: BASE}, {p: "import not_a_module_b33\n" + BASE})
    rc, out = _cli(d, CLI, "--collect")
    assert rc == 2 and "WEAKENING_ERROR collect-only failed" in out


# ------------------------------------------------------------------ B33 v2 C5: мутационный smoke реализации
def test_b33v2_impl_smoke_catches_every_implementation_mutant():
    spec = importlib.util.spec_from_file_location("impl_smoke_b33", ROOT / "tools" / "impl_mutation_smoke.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    rc, lines = m.run(ROOT)
    assert rc == 0, lines
    assert lines[-1] == f"IMPL_SMOKE_SUMMARY mutants={len(m.MUTANTS)} caught={len(m.MUTANTS)} control=green"
    assert {x[0] for x in m.MUTANTS} >= {"sign_volume_max_99", "confirm_ttl_ignored", "sigma_live_enabled_false",
                                         "host_without_admin_check"}


def test_b33v2_liar_impl_smoke_mutation_that_breaks_nothing_is_not_caught(monkeypatch):
    """Тест-лжец: мутация, которую тесты не замечают (комментарий), обязана дать caught=no и код 1."""
    spec = importlib.util.spec_from_file_location("impl_smoke_b33_liar", ROOT / "tools" / "impl_mutation_smoke.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "MUTANTS", [("harmless", "src/tv_commands.py", "SIGN_VOLUME_MAX = 15", "SIGN_VOLUME_MAX = 15  # x", ["INV-19"], [])])
    rc, lines = m.run(ROOT)
    assert rc == 1 and lines[1].startswith("IMPL_SMOKE harmless caught=no")


def test_b33v2_impl_smoke_copy_takes_git_files_only(tmp_path):
    """В git-дереве копия берёт отслеживаемые и новые неигнорируемые файлы, но не игнорируемые и не data/ (на ПК там
    venv, логи, базы); без .git -- copytree (экспорт снимка)."""
    spec = importlib.util.spec_from_file_location("impl_smoke_b33_copy", ROOT / "tools" / "impl_mutation_smoke.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    r = tmp_path / "r"
    (r / "src").mkdir(parents=True)
    (r / "data").mkdir()
    (r / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (r / ".gitignore").write_text("big/\n", encoding="utf-8")
    _hrun(["git", "init", "-q", str(r)], check=True)
    _hrun(["git", "-C", str(r), "add", "src/a.py", ".gitignore"], check=True)
    (r / "src" / "new.py").write_text("N = 1\n", encoding="utf-8")
    (r / "big").mkdir()
    (r / "big" / "model.bin").write_bytes(b"x")
    (r / "data" / "x.db").write_bytes(b"x")
    (r / "data" / "t.txt").write_text("t", encoding="utf-8")
    _hrun(["git", "-C", str(r), "add", "-f", "data/t.txt"], check=True)
    d = tmp_path / "d"
    assert m.copy_tree(r, d) == "git_files"
    got = sorted(str(p.relative_to(d)).replace("\\", "/") for p in d.rglob("*") if p.is_file())
    assert got == [".gitignore", "src/a.py", "src/new.py"]
    e = tmp_path / "e"
    m.rmtree_force(r / ".git")                                # Windows: объекты git только для чтения (selfrun 25.11)
    assert m.copy_tree(r, e) == "copytree"
    assert (e / "big" / "model.bin").exists() and not (e / "data").exists()


# ------------------------------------------------------------------ B33 v2 попытка 2 (B32m D2 / ревизор G2): значения parametrize
HOST_STRICT = "def is_host(uid):\n    return type(uid) is int and uid == 111\n"
HOST_WEAK = "def is_host(uid):\n    return str(uid).strip() == '111'\n"
HOST_T = ('import pytest\nfrom host import is_host\n\n\n@pytest.mark.parametrize("uid", ["111", "abc", 1.0])\n'
          'def test_only_exact_int_is_host(uid):\n    assert not is_host(uid)\n')


def test_b33v2_g2_liar_same_id_other_value_is_red(tmp_path):
    """Сценарий ревизора G2: is_host принимает строку из цифр, а "111" подменён на pytest.param("abc", id="111") --
    node ID те же, сюита зелёная; сторож обязан дать красный changed_param (и без, и с --collect)."""
    p = "src/test_host.py"
    files = {"src/host.py": HOST_STRICT, p: HOST_T, "pytest.ini": "[pytest]\ntestpaths = src\npythonpath = src\n"}
    d = _repo(tmp_path, files, {"src/host.py": HOST_WEAK, p: HOST_T.replace('["111", "abc", 1.0]', '[pytest.param("abc", id="111"), "abc", 1.0]')})
    r = _hrun([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "src"], cwd=d, capture_output=True)
    assert r.returncode == 0                                                       # дыра не видна тестами
    rc, out = _cli(d, CLI, "--collect")
    assert "WEAKENING_COLLECT base=3 head=3" in out and "removed_test" not in out  # node ID не изменились
    assert rc == 1 and f"WEAKENING red changed_param:{p}::test_only_exact_int_is_host" in out
    rc0, out0 = _cli(d)
    assert rc0 == 1 and f"WEAKENING red changed_param:{p}::test_only_exact_int_is_host" in out0


def test_b33v2_g2_real_b06d2_value_swap_is_found():
    src = (ROOT / "src" / "test_b06d2.py").read_text(encoding="utf-8")
    old = '-111, "111", 111.0'
    assert src.count(old) == 1
    got = wg.compare({"src/test_b06d2.py": ("a", src)}, {"src/test_b06d2.py": ("b", src.replace(old, '-111, pytest.param("abc", id="111"), 111.0'))})
    assert got == ["changed_param:src/test_b06d2.py::test_d2_only_exact_int_id_is_host"]


@pytest.mark.parametrize("head, want", [
    ('[pytest.param("111"), "abc", 1.0]', []),                                      # тот же случай в pytest.param
    ('[pytest.param("111", id="x"), "abc", 1.0]', []),                              # смена id -- не потеря значения
    ('["111", "abc", 1.0, 2]', []),                                                 # добавлен случай
    ('["abc", "111", 1.0]', []),                                                    # порядок
    ('[pytest.param("111", marks=pytest.mark.skip), "abc", 1.0]', ["changed_param"]),
    ('["112", "abc", 1.0]', ["changed_param"]),
    ('["111", "abc"]', ["changed_param"]),
    ('VALS', ["changed_param"]),                                                    # список вынесен и сужен
])
def test_b33v2_g2_param_values_table(head, want):
    base = 'import pytest\nVALS = ["abc", 1.0]\n\n\n@pytest.mark.parametrize("uid", ["111", "abc", 1.0])\ndef test_v(uid):\n    assert uid\n'
    got = wg.compare({P: ("a", base)}, {P: ("b", base.replace('["111", "abc", 1.0]', head))})
    assert [k.split(":", 1)[0] for k in got] == want


def test_b33v2_g2_named_list_and_sum_are_expanded():
    base = 'import pytest\nA = ["x", "y"]\nB = ["z"]\n\n\n@pytest.mark.parametrize("t", A + B + ["w"])\ndef test_s(t):\n    assert t\n'
    assert wg.compare({P: ("a", base)}, {P: ("b", base.replace('B = ["z"]', 'B = []'))}) == [f"changed_param:{P}::test_s"]
    assert wg.compare({P: ("a", base)}, {P: ("b", base.replace('A = ["x", "y"]', 'A = ["y", "x"]'))}) == []


# ------------------------------------------------------------------ B33 v2 попытка 2 (B32r M6 / ревизор G8): документы под C3
DOC_C3 = ("docs/SECURITY.md", "docs/GUARDS.md", "docs/ARCHITECTURE.md", "docs/RUNBOOK.md", ".github/workflows/pytest.yml")
OLD_C3 = ("--base HEAD~1", "fetch-depth: 2", "parent snapshot", "git show HEAD~1:", "кодом РОДИТЕЛЯ", "с родителем (`HEAD~1`")


@pytest.mark.parametrize("rel", DOC_C3)
def test_b33v2_g8_docs_describe_published_base_not_head1(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert [o for o in OLD_C3 if o in text] == [], f"{rel}: старая механика базы сторожа (до C3)"


def test_b33v2_g8_docs_name_the_published_base():
    for rel in ("docs/SECURITY.md", "docs/GUARDS.md", "docs/ARCHITECTURE.md"):
        t = (ROOT / rel).read_text(encoding="utf-8")
        assert "merge-base" in t and "fetch-depth: 0" in t, rel



# ------------------------------------------------------------------ B33 v2 попытка 3 (selfrun ПК 25.11 09:09, 4 падения Windows)
def _impl(name="impl_smoke_b33_v3"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "impl_mutation_smoke.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _windows_unlink(monkeypatch):
    """Семантика Windows: файл «только для чтения» не удаляется (PermissionError), пока не снят атрибут."""
    import os
    import stat as st
    real = os.unlink

    def unlink(path, *a, **k):
        if not (os.stat(path, dir_fd=k.get("dir_fd"), follow_symlinks=False).st_mode & st.S_IWRITE):
            raise PermissionError(5, "Отказано в доступе", str(path))
        return real(path, *a, **k)
    monkeypatch.setattr(os, "unlink", unlink)


def test_b33v3_liar_rmtree_force_removes_read_only_git_objects(tmp_path, monkeypatch):
    import os
    m = _impl()
    r = tmp_path / "r"
    r.mkdir()
    (r / "a.txt").write_text("a", encoding="utf-8")
    _hrun(["git", "init", "-q", str(r)], check=True)
    _hrun(["git", "-C", str(r), "add", "a.txt"], check=True)
    objs = [p for p in (r / ".git" / "objects").rglob("*") if p.is_file()]
    assert objs
    for o in objs:
        os.chmod(o, 0o444)                                    # как git на Windows
    _windows_unlink(monkeypatch)
    with pytest.raises(PermissionError):                      # обычный rmtree падает (падение 3 selfrun)
        shutil.rmtree(r / ".git")
    m.rmtree_force(r / ".git")
    assert not (r / ".git").exists()
    m.rmtree_force(r / ".git")                                # повтор -- не ошибка


def test_b33v3_liar_impl_smoke_red_control_is_reported_not_caught_no(monkeypatch):
    """Контроль красный -> «control failed» + причина (первая строка FAILED), мутанты не гоняются; не «caught=no»."""
    m = _impl("impl_smoke_b33_v3_ctl")
    monkeypatch.setattr(m, "MUTANTS", [("x", "src/tv_commands.py", "SIGN_VOLUME_MAX = 15", "SIGN_VOLUME_MAX = 99", [],
                                        ["src/test_weakening_guard_b33.py::test_b33v3_helper_always_red"])])
    monkeypatch.setenv("OKO_B33V3_RED", "1")
    rc, lines = m.run(ROOT)
    assert rc == 1 and lines[-1] == "IMPL_SMOKE_SUMMARY mutants=1 caught=0 control=red"
    assert any(x.startswith("IMPL_SMOKE control failed") for x in lines)
    assert any(x.startswith("IMPL_SMOKE control_why ") and "test_b33v3_helper_always_red" in x for x in lines)
    assert "IMPL_SMOKE x not_run=control_failed" in lines and not any(" caught=no" in x for x in lines)


def test_b33v3_helper_always_red():
    """Помощник лжеца выше: красный только по флагу окружения, иначе зелёный (в обычной сюите проходит)."""
    import os
    assert os.environ.get("OKO_B33V3_RED") != "1"


def test_b33v3_liar_impl_smoke_child_is_hermetic(monkeypatch):
    """Окружение родителя (PYTEST_ADDOPTS, PYTHONPATH, кодировка консоли) не ломает контроль копии."""
    m = _impl("impl_smoke_b33_v3_env")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--definitely-not-an-option-b33")
    monkeypatch.setenv("PYTHONPATH", "/nonexistent/b33")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1251")
    env = m.child_env()
    assert "PYTEST_ADDOPTS" not in env and "PYTHONPATH" not in env and env["PYTHONIOENCODING"] == "utf-8"
    rc, lines = m.run(ROOT, only="bypass_live_false")
    assert rc == 0 and lines[-1] == "IMPL_SMOKE_SUMMARY mutants=1 caught=1 control=green", lines


_CP1251 = ("import io, runpy, sys; cli = sys.argv[1]; "
           "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='cp1251', errors='strict', write_through=True); "
           "sys.argv = sys.argv[1:]; runpy.run_path(cli, run_name='__main__')")


def _cli_cp1251(cli, *args):
    """CLI как на Windows под `python -I`: консоль cp1251, PYTHONIOENCODING не действует."""
    r = _hrun([sys.executable, "-I", "-c", _CP1251, str(cli), *args], capture_output=True)
    return r.returncode, r.stdout


def test_b33v3_liar_weakening_guard_output_is_utf8_under_cp1251(tmp_path):
    """Падение 1 selfrun: вывод сторожа под cp1251 не читался как UTF-8 (байт 0xf3). Сценарий того же теста."""
    d = _repo(tmp_path, {"pytest.ini": "[pytest]\n"}, {
        ".github/test_weakening_allow.txt": f"fewer_asserts:src/test_x.py::test_a | r | 2026-11-24 | b33_x | {E1}\n",
        ".github/guarded_change_allow.txt": f"guarded_change:pytest.ini::<module>@{'0' * 12} | r | 2026-11-24 | b33_y | {E1}\n"})
    rc, raw = _cli_cp1251(CLI, "--repo", str(d))
    out = raw.decode("utf-8")                                 # не бросает: вывод UTF-8
    assert rc == 2 and "already in" in out and "WEAKENING_ALLOW_ERROR" in out


@pytest.mark.parametrize("cli, args, want", [
    ("tools/verify_control_map.py", ["--junit", "windows=src/control_map_junit/windows.xml",
                                     "--junit", "linux=src/control_map_junit/linux.xml"], "checked on 2 platforms of 2"),
    ("tools/guard_mutation_smoke.py", ["--root", "."], "SMOKE_SUMMARY"),
])
def test_b33v3_block_clis_output_is_utf8_under_cp1251(cli, args, want, monkeypatch):
    monkeypatch.chdir(ROOT)
    rc, raw = _cli_cp1251(ROOT / cli, *args)
    assert want in raw.decode("utf-8") and rc == 0


def test_b33v3_all_block_clis_force_utf8_output():
    """Каждый CLI блока пишет UTF-8 сам (reconfigure или sys.stdout.buffer + encode('utf-8')), не через кодировку консоли."""
    for rel in ("tools/weakening_guard.py", "tools/verify_control_map.py", "tools/guard_mutation_smoke.py",
                "tools/impl_mutation_smoke.py", "tools/notify.py", "tools/consent_lines.py", "tools/push_record.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert 'reconfigure(encoding="utf-8"' in src or 'encode("utf-8")' in src, rel
        tree = ast.parse(src)
        bare = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "print"]
        assert not bare or 'reconfigure(encoding="utf-8"' in src, (rel, bare)


# ------------------------------------------------------------------ B33 v2 attempt 4 (selfrun #2 25.11 10:37; ASCII rule)
CONTOUR = ROOT / "tools" / "build_contour_guard.py"


def _contour(name="contour_b33a4"):
    spec = importlib.util.spec_from_file_location(name, CONTOUR)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _nonascii_at(raw: bytes) -> str:
    i = next((k for k, b in enumerate(raw) if b > 127), -1)
    return "" if i < 0 else raw[max(0, i - 60):i + 20].decode("utf-8", "replace")


@pytest.mark.parametrize("cli, args", [
    ("tools/verify_control_map.py", ["--junit", "windows=src/control_map_junit/windows.xml",
                                     "--junit", "linux=src/control_map_junit/linux.xml"]),
    ("tools/verify_control_map.py", ["--junit", "windows=src/control_map_junit/windows.xml"]),
    ("tools/verify_control_map.py", ["--junit", "windows=/nonexistent/b33a4.xml"]),
    ("tools/verify_control_map.py", []),
    ("tools/guard_mutation_smoke.py", ["--root", "."]),
    ("tools/notify.py", ["--status", "RED", "--text", "x", "--env", "/nonexistent/b33a4/.env"]),
])
def test_b33a4_liar_block_cli_output_is_ascii_under_cp1251(cli, args, monkeypatch):
    """Item 1: the console output of the block CLIs is ASCII (English), also on the error / USAGE paths, under a
    cp1251 console (python -I on Windows). Attempt 3 printed Russian (UTF-8 bytes > 127) -- red there."""
    monkeypatch.chdir(ROOT)
    rc, raw = _cli_cp1251(ROOT / cli, *args)
    assert raw.strip(), (cli, rc)
    assert all(b < 128 for b in raw), (cli, _nonascii_at(raw))


def test_b33a4_liar_guard_and_consent_lines_escape_non_ascii_data(tmp_path):
    """Item 1: data with Cyrillic (allow reason, path) reaches the output only as ASCII escapes; JSON of consent_lines
    is ensure_ascii. Attempt 3: raw UTF-8 Cyrillic in the output."""
    d = _repo(tmp_path, {"pytest.ini": "[pytest]\n"}, {
        ".github/test_weakening_allow.txt": f"fewer_asserts:src/test_x.py::test_a | причина | 2026-11-24 | b33_x | {E1}\n",
        ".github/guarded_change_allow.txt": f"guarded_change:pytest.ini::<module>@{'0' * 12} | r | 2026-11-24 | b33_y | {E1}\n"})
    rc, raw = _cli_cp1251(CLI, "--repo", str(d))
    assert rc == 2 and b"WEAKENING_ALLOW_ERROR" in raw and all(b < 128 for b in raw), _nonascii_at(raw)
    bad = tmp_path / "ошибка_allow.txt"
    bad.write_text("not a consent line\n", encoding="utf-8")
    rc2, raw2 = _cli_cp1251(ROOT / "tools" / "consent_lines.py", "--db", str(tmp_path / "none.db"), "--new", str(bad))
    assert rc2 == 2 and all(b < 128 for b in raw2), _nonascii_at(raw2)


def test_b33a4_contour_guard_this_repo_is_clean():
    """Item 2 (in CI: part of the suite on both platforms): no non-ASCII string in a block CLI, no implicit decode of
    process output in tests / tools, no bare child in the hermetic files."""
    r = _hrun([sys.executable, "-I", str(CONTOUR), "--root", str(ROOT)], capture_output=True)
    out = r.stdout.decode("ascii")
    assert r.returncode == 0 and out.strip().splitlines()[-1].endswith("findings=0 errors=0"), out[-1500:]


def _tree(tmp_path, files: dict) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return tmp_path


def _rules(found) -> list:
    return sorted(f[0] for f in found)


def test_b33a4_liar_contour_guard_python_rules(tmp_path):
    m = _contour("contour_b33a4_py")
    bad = _tree(tmp_path / "bad", {
        "tools/notify.py": 'def f():\n    """докстрока можно"""\n    print("ошибка вывода")\n',
        "src/test_y.py": ("import subprocess\n"
                          "def test_a():\n    r = subprocess.run(['git', 'status'], capture_output=True, text=True)\n"
                          "    assert r.stdout.decode() == ''\n"),
        "src/test_net_guard.py": "import subprocess\ndef test_b():\n    subprocess.run(['git', 'status'], capture_output=True)\n"})
    found, errs, _ = m.scan_python(bad)
    assert not errs and _rules(found) == ["ascii_cli", "bare_child", "implicit_decode", "implicit_decode"], found
    good = _tree(tmp_path / "good", {
        "tools/notify.py": ('def f():\n    """докстрока можно"""\n    print("output error")\n'
                            'RX = "^Статус"  # non-ascii-data: pattern of a Russian document\n'),
        "src/test_y.py": ("import subprocess\n"
                          "def test_a():\n    r = subprocess.run(['git', 'status'], capture_output=True, text=True, encoding='utf-8')\n"
                          "    assert r.stdout.encode().decode('utf-8') == r.stdout\n"),
        "src/test_net_guard.py": "import subprocess\ndef test_b(env={}):\n    subprocess.run(['git', 'status'], capture_output=True, env=env)\n"})
    found2, errs2, _ = m.scan_python(good)
    assert not errs2 and found2 == [], found2


_PS_COMMON = ('$L = New-Object System.Collections.Generic.List[string]\r\n'
              '$Root = "X:\\repo"\r\n'
              'function Log-Line($s) { $L.Add([string]$s); Write-Host $s }\r\n'
              'function Need-Root { return (Join-Path $Root "x") }\r\n')
# selfrun #2 (attempt 3 of run_b33.ps1, lines 114 / 262): a one-entry list without the comma + loop $l over it
_PS_RUN_BAD = ('. (Join-Path $PSScriptRoot "x_common.ps1")\r\nWrite-LaunchFlag "run_x"\r\n'
               '$LocalFiles = @( @("export_public.py", "5a12cacf1c71273e", "4a3c110cce0dd919") )\r\n'
               'function Uses-Root($root) { return (Need-Root) }\r\n'
               'try { foreach ($l in $LocalFiles) { Log-Line "LOCAL $($l[0])" } } finally { Finish-Block "run_x" "B33" "run" "" }\r\n')
_PS_RUN_GOOD = ('. (Join-Path $PSScriptRoot "x_common.ps1")\r\nWrite-LaunchFlag "run_x"\r\n'
                '$LocalFiles = @( ,@("export_public.py", "5a12cacf1c71273e", "4a3c110cce0dd919") )\r\n'
                'function Uses-Root($rootDir) { return (Need-Root) }\r\n'
                'try { foreach ($lcl in $LocalFiles) { Log-Line "LOCAL $($lcl[0])" } } finally { Finish-Block "run_x" "B33" "run" "" }\r\n')


def test_b33a4_liar_contour_guard_powershell_rules(tmp_path):
    """Items 7 / 8: the exact pattern of selfrun #2 is red (top_case + log_var + top_loop for $l, flat_pair for the
    one-entry list, dyn_shadow for a parameter that hides $Root from a callee, ascii_bytes); the fixed form is clean."""
    m = _contour("contour_b33a4_ps")
    bad = _tree(tmp_path / "bad", {"x_common.ps1": b"\xef\xbb\xbf" + _PS_COMMON.encode("ascii"),
                                   "run_x.ps1": b"\xef\xbb\xbf" + _PS_RUN_BAD.encode("ascii") + "# \u0442\u0435\u0441\u0442\r\n".encode("utf-8")})
    found, errs = m.ps_check([str(bad / "x_common.ps1"), str(bad / "run_x.ps1")])
    assert not errs
    got = _rules(found)
    for want in ("ascii_bytes", "dyn_shadow", "flat_pair", "log_var", "top_case", "top_loop"):
        assert want in got, (want, found)
    good = _tree(tmp_path / "good", {"x_common.ps1": b"\xef\xbb\xbf" + _PS_COMMON.encode("ascii"),
                                     "run_x.ps1": b"\xef\xbb\xbf" + _PS_RUN_GOOD.encode("ascii")})
    found2, errs2 = m.ps_check([str(good / "x_common.ps1"), str(good / "run_x.ps1")])
    assert not errs2 and found2 == [], found2


def test_b33a4_liar_henv_strips_dirty_parent(monkeypatch):
    """Item 3: the children of the block tests do not inherit PYTHONHOME / PYTEST_ADDOPTS / OKO_B33_* of the parent
    (selfrun / Operator profile). Without _henv a PYTHONHOME of another Python stops the child before main."""
    monkeypatch.setenv("PYTHONHOME", "/nonexistent/b33a4_home")
    monkeypatch.setenv("PYTHONPATH", "/nonexistent/b33a4_path")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--definitely-not-an-option-b33a4")
    monkeypatch.setenv("OKO_B33_ROOT", "/nonexistent/b33a4_root")
    monkeypatch.setenv("OKO_NOTIFY_OFF", "1")
    r = _hrun([sys.executable, "-c", "import os; print('child', os.environ.get('PYTHONHOME'), os.environ.get('PYTEST_ADDOPTS'),"
               " os.environ.get('OKO_B33_ROOT'), os.environ.get('OKO_NOTIFY_OFF'), os.environ.get('PYTHONIOENCODING'))"],
              capture_output=True)
    assert r.returncode == 0 and r.stdout.decode("utf-8").split() == ["child", "None", "None", "None", "None", "utf-8"], r.stderr[-400:]


def test_b33a4_liar_collect_survives_parent_pytest_addopts(tmp_path):
    """Item 3: `--collect` of the weakening guard runs its pytest child hermetically. With PYTEST_ADDOPTS=-q in the
    environment OF THE TOOL, attempt 3 collected 0 node IDs (`-q -q` prints file counts) and saw nothing removed."""
    p = "src/test_host.py"
    files = {"src/host.py": HOST_STRICT, p: HOST_T, "pytest.ini": "[pytest]\ntestpaths = src\npythonpath = src\n"}
    d = _repo(tmp_path, files, {p: HOST_T.replace('["111", "abc", 1.0]', '["111", "abc"]')})
    r = _hrun([sys.executable, "-I", str(CLI), "--repo", str(d), "--collect"], capture_output=True,
              env=_henv(PYTEST_ADDOPTS="-q"))
    out = r.stdout.decode("ascii")
    assert "WEAKENING_COLLECT base=3 head=2" in out, out
    assert r.returncode == 1 and f"WEAKENING red removed_test:{p}::test_only_exact_int_is_host[1.0]" in out, out


def test_b33a4_liar_tool_children_env_is_hermetic(monkeypatch):
    """Item 3: the same hermetic list in every block tool that starts a child (git / pytest)."""
    for k, v in (("PYTHONHOME", "/x"), ("PYTEST_ADDOPTS", "-q"), ("OKO_B33_STAGE", "/x"), ("OKO_NOTIFY_OFF", "1"),
                 ("GIT_DIR", "/x/.git"), ("GIT_INDEX_FILE", "/x/idx"), ("OKO_B33V3_RED", "1")):
        monkeypatch.setenv(k, v)
    for rel in ("tools/weakening_guard.py", "tools/verify_control_map.py", "tools/impl_mutation_smoke.py"):
        spec = importlib.util.spec_from_file_location("henv_" + rel.replace("/", "_")[:-3], ROOT / rel)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        env = mod.child_env()
        for k in ("PYTHONHOME", "PYTEST_ADDOPTS", "OKO_B33_STAGE", "OKO_NOTIFY_OFF", "GIT_DIR", "GIT_INDEX_FILE"):
            assert k not in env, (rel, k)
        assert env["PYTHONIOENCODING"] == "utf-8" and env.get("OKO_B33V3_RED") == "1", rel    # test flags pass
