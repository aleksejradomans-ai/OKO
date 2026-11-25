"""Сторож ослабления тестов (src/weakening_guard.py, tools/weakening_guard.py) на синтетических диффах.
Временные репозитории git -- в tmp_path; рабочее дерево проекта не читается."""
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


CLI = Path(__file__).resolve().parent.parent / "tools" / "weakening_guard.py"
P = "src/test_x.py"

BASE = '''import pytest


def test_a():
    assert 1 == 1
    assert 2 == 2


def test_b(x):
    assert x == pytest.approx(1.0, rel=1e-6)
    r = run(timeout=5)
    assert r


class TestK:
    def test_c(self):
        self.assertEqual(1, 1)
        with pytest.raises(ValueError):
            int("x")
'''


def one(src, base=BASE, path=P):
    return wg.compare({path: ("b1", base)}, {path: ("b2", src)})


def test_same_content_no_findings():
    assert wg.compare({P: ("b1", BASE)}, {P: ("b1", BASE)}) == []
    assert one(BASE + "\n\ndef test_new():\n    assert 3\n") == []                 # новый тест -- не ослабление


def test_removed_assert():
    assert one(BASE.replace("    assert 2 == 2\n", "")) == ["fewer_asserts:src/test_x.py::test_a"]


def test_removed_method_assert_and_raises():
    src = BASE.replace("        with pytest.raises(ValueError):\n            int(\"x\")\n", "        int(\"1\")\n")
    assert one(src) == ["fewer_asserts:src/test_x.py::TestK.test_c"]


def test_removed_test_function():
    src = BASE.replace("def test_a():\n    assert 1 == 1\n    assert 2 == 2\n", "")
    assert one(src) == ["removed_test:src/test_x.py::test_a"]


def test_moved_test_to_another_file_is_not_removal():
    src = BASE.replace("def test_a():\n    assert 1 == 1\n    assert 2 == 2\n", "")
    other = "def test_a():\n    assert 1 == 1\n    assert 2 == 2\n"
    got = wg.compare({P: ("b1", BASE)}, {P: ("b2", src), "src/test_y.py": ("b3", other)})
    assert got == []


@pytest.mark.parametrize("deco", ["@pytest.mark.skip(reason='x')\n", "@pytest.mark.skipif(True, reason='x')\n",
                                  "@pytest.mark.xfail(reason='x')\n"])
def test_new_skip_decorator(deco):
    assert one(BASE.replace("def test_a():", deco + "def test_a():")) == ["new_skip:src/test_x.py::test_a"]


def test_new_skip_call_and_importorskip_in_function():
    src = BASE.replace("def test_b(x):\n", "def test_b(x):\n    pytest.importorskip('numpy')\n")
    assert one(src) == ["new_skip:src/test_x.py::test_b"]
    src = BASE.replace("def test_b(x):\n", "def test_b(x):\n    pytest.skip('later')\n")
    assert one(src) == ["new_skip:src/test_x.py::test_b"]


def test_new_module_level_skip():
    src = BASE.replace("import pytest\n", "import pytest\n\npytestmark = pytest.mark.skip(reason='x')\n")
    assert one(src) == ["new_skip:src/test_x.py::<module>"]
    src = BASE.replace("import pytest\n", "import pytest\n\nnp = pytest.importorskip('numpy')\n")
    assert one(src) == ["new_skip:src/test_x.py::<module>"]


def test_class_skip_counts_for_methods():
    src = BASE.replace("class TestK:", "@pytest.mark.skip(reason='x')\nclass TestK:")
    assert one(src) == ["new_skip:src/test_x.py::TestK.test_c"]


def test_wider_tolerance_approx_and_timeout():
    assert one(BASE.replace("rel=1e-6", "rel=1e-3")) == ["wider_tolerance:src/test_x.py::test_b"]
    assert one(BASE.replace("timeout=5", "timeout=60")) == ["wider_tolerance:src/test_x.py::test_b"]
    assert one(BASE.replace("rel=1e-6", "rel=1e-9")) == []                          # строже -- не находка
    src = BASE.replace("def test_a():", "@pytest.mark.timeout(10)\ndef test_a():")
    assert one(src) == []                                                           # новый допуск -- не ослабление
    base2 = BASE.replace("def test_a():", "@pytest.mark.timeout(10)\ndef test_a():")
    assert one(base2.replace("timeout(10)", "timeout(30)"), base=base2) == ["wider_tolerance:src/test_x.py::test_a"]


def test_isclose_tolerance():
    base = "import math\n\n\ndef test_m():\n    assert math.isclose(1.0, 1.0, rel_tol=1e-9)\n"
    assert one(base.replace("1e-9", "1e-2"), base=base) == ["wider_tolerance:src/test_x.py::test_m"]


def test_deleted_file_and_rename():
    assert wg.compare({P: ("b1", BASE)}, {}) == ["deleted_file:src/test_x.py::<module>"]
    assert wg.compare({P: ("b1", BASE)}, {"src/test_z.py": ("b1", BASE)}) == []          # тот же blob -- переименование


def test_syntax_error_in_head_is_red():
    assert "removed_test:src/test_x.py::<module>" in one("def test_a(:\n")


def test_non_test_paths_ignored():
    assert wg.is_test_path("src/test_x.py") and wg.is_test_path("src/x_test.py")
    assert not wg.is_test_path("src/x.py") and not wg.is_test_path("tools/archive/test_old.py")


def test_allow_list_parse_and_judge():
    lines = ["# comment", "", "fewer_asserts:src/test_x.py::test_a | merged into test_b | 2026-11-21 | b30_merge_ok | 0123456789abcdef",
             "new_skip:src/test_x.py::<module> | numpy optional | 2026-11-21 | b30_skip_ok | fedcba9876543210"]   # B33: + entry_sha16
    allow, errs = wg.parse_allow(lines)
    assert errs == [] and set(allow) == {"fewer_asserts:src/test_x.py::test_a", "new_skip:src/test_x.py::<module>"}
    j = wg.judge(["fewer_asserts:src/test_x.py::test_a", "removed_test:src/test_x.py::test_b"], allow)
    assert j["ok"] is False and j["red"] == ["removed_test:src/test_x.py::test_b"]
    assert j["allowed"] == ["fewer_asserts:src/test_x.py::test_a"] and j["unused_allow"] == ["new_skip:src/test_x.py::<module>"]
    assert wg.judge(["fewer_asserts:src/test_x.py::test_a"], allow)["ok"] is True


@pytest.mark.parametrize("bad", ["fewer_asserts:src/test_x.py::test_a | reason | 2026-11-21",
                                 "fewer_asserts:src/test_x.py::test_a | | 2026-11-21 | code",
                                 "fewer_asserts:src/test_x.py::test_a | r | 21.11.2026 | code",
                                 "fewer_asserts:src/test_x.py::test_a | r | 2026-11-21 | Has Text",
                                 "weaker:src/test_x.py::test_a | r | 2026-11-21 | code"])
def test_allow_list_bad_line_is_error(bad):
    allow, errs = wg.parse_allow([bad])
    assert allow == {} and len(errs) == 1


def _git(d, *a):
    _hrun(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *a], check=True, capture_output=True)


def _repo(tmp_path, files_base, files_head, allow=None):
    d = tmp_path / "r"
    d.mkdir()
    _git(d, "init", "-q")
    for name, body in files_base.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "base")
    for name in files_base:
        if name not in files_head:
            (d / name).unlink()
    for name, body in files_head.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body, encoding="utf-8")
    if allow is not None:
        (d / ".github").mkdir(exist_ok=True)
        (d / ".github" / "test_weakening_allow.txt").write_text(allow, encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "head")
    return d


def _cli(d, *extra):
    r = _hrun([sys.executable, str(CLI), "--repo", str(d), *extra], capture_output=True)
    return r.returncode, r.stdout.decode("utf-8")


def test_cli_red_on_weakening_green_when_allowed(tmp_path):
    d = _repo(tmp_path, {P: BASE}, {P: BASE.replace("    assert 2 == 2\n", "")})
    rc, out = _cli(d)
    assert rc == 1 and "WEAKENING red fewer_asserts:src/test_x.py::test_a" in out and "red=1" in out
    (tmp_path / "r2").mkdir()
    d2 = _repo(tmp_path / "r2", {P: BASE}, {P: BASE.replace("    assert 2 == 2\n", "")},
               allow="fewer_asserts:src/test_x.py::test_a | duplicate check | 2026-11-21 | b30_dup_ok | 0123456789abcdef\n")
    rc, out = _cli(d2)
    assert rc == 0 and "WEAKENING allowed fewer_asserts:src/test_x.py::test_a" in out and "red=0" in out


def test_cli_clean_diff_and_missing_base(tmp_path):
    d = _repo(tmp_path, {P: BASE}, {P: BASE + "\n\ndef test_more():\n    assert True\n"})
    rc, out = _cli(d)
    assert rc == 0 and "findings=0" in out
    rc, out = _cli(d, "--base", "HEAD~5")
    assert rc == 2 and "WEAKENING_ERROR" in out                                  # нет родителя -- красный, не зелёный


def test_cli_bad_allow_file_is_error(tmp_path):
    d = _repo(tmp_path, {P: BASE}, {P: BASE}, allow="not a valid line\n")
    rc, out = _cli(d)
    assert rc == 2 and "WEAKENING_ALLOW_ERROR" in out
