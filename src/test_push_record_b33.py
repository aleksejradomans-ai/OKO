"""B33 п.6.7: каждая попытка пуша -- своя строка run_metrics (tools/push_record.py); исход по строке notify."""
import subprocess
import sys
from pathlib import Path
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


CLI = Path(__file__).resolve().parent.parent / "tools" / "push_record.py"


def _rec(db, rc, line, script="selfrun"):
    r = _hrun([sys.executable, "-I", str(CLI), "--db", str(db), "--task", "B33", "--script", script,
                        "--started", "2026-11-24T18:00:00", "--notify-rc", str(rc), "--notify-line", line], capture_output=True)
    return r.returncode, r.stdout.decode("utf-8")


def _rows(db):
    import sqlite3
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT block, outcome, fail_code, net_errors, steps FROM run_metrics ORDER BY recorded_at, rowid").fetchall()
    finally:
        con.close()


def test_b33_sent_and_not_sent_each_attempt_is_own_row(tmp_path):
    db = tmp_path / "q.db"
    assert _rec(db, 0, "NOTIFY sent=yes message_id=42")[0] == 0
    assert _rec(db, 1, "NOTIFY sent=no reason=network: no network or no answer after the retry (URLError)")[0] == 0
    assert _rec(db, 1, "NOTIFY sent=no reason=kill: рубильник сети", "cycle")[0] == 0
    assert _rec(db, 1, "")[0] == 0
    rows = _rows(db)
    assert rows == [("notify_selfrun", "GREEN", None, 0, 1), ("notify_selfrun", "RED", "network", 1, 1),
                    ("notify_cycle", "RED", "kill", 0, 1), ("notify_selfrun", "RED", "no_answer", 0, 1)], rows


def test_b33_liar_sent_yes_with_rc1_or_bad_id_is_not_green(tmp_path):
    db = tmp_path / "q.db"
    _rec(db, 1, "NOTIFY sent=yes message_id=42")
    _rec(db, 0, "NOTIFY sent=yes message_id=x")
    assert [r[1] for r in _rows(db)] == ["RED", "RED"]
