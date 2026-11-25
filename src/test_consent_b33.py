"""B33 H2.6: строки согласия, добавленные в блоке, -- только с entry_sha16 записи [1] локального журнала согласий
(tools/consent_lines.py; его вызывает push_*_stage перед пушем). Только временные базы; время -- константы."""
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import consent as cs
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


CLI = Path(__file__).resolve().parent.parent / "tools" / "consent_lines.py"
NOW = dt.datetime(2026, 11, 24, 18, 0, 0)
KEY = "guarded_change:pytest.ini::<module>@" + "a" * 12


def _entry(code: str, answer: int = 1) -> dict:
    return {"ts": "2026-11-24T18:00:00", "source": "chat", "question_id": f"sb-20261124-1800-b33-{code}".replace("_", "-"),
            "answer": answer, "question_text": f"B33 {code}", "question_code": code, "task": "B33", "origin": "retro_summary"}


def _db(tmp_path, *entries) -> Path:
    db = tmp_path / "c.db"
    cs.add_many(list(entries), db, now=NOW)
    return db


def _e16(db: Path, code: str) -> str:
    return [r for r in cs.list_entries(db) if r["question_code"] == code][0]["entry_sha16"]


def _cli(db: Path, *a: str) -> tuple:
    r = _hrun([sys.executable, "-I", str(CLI), "--db", str(db), *a], capture_output=True)
    return r.returncode, json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_b33_find_entries_by_entry_sha16(tmp_path):
    db = _db(tmp_path, _entry("b33_a"))
    s = _e16(db, "b33_a")
    got = cs.find_entries([s, "0" * 16], db)
    assert got[s]["question_code"] == "b33_a" and got["0" * 16] is None


def test_b33_check_lines_added_line_found_go(tmp_path):
    db = _db(tmp_path, _entry("b33_ini"))
    new = tmp_path / "new.txt"
    new.write_text(f"{KEY} | r | 2026-11-24 | b33_ini | {_e16(db, 'b33_ini')}\n", encoding="utf-8")
    rc, j = _cli(db, "--new", str(new), "--old", str(tmp_path / "absent.txt"))
    assert rc == 0 and j["decision"] == "GO" and j["added"] == 1 and j["bad"] == [] and j["chain_ok"] is True


def test_b33_liar_check_lines_unknown_sha16_is_stop(tmp_path):
    """Тест-лжец: строка, выписанная «тем же коммитом» с выдуманным entry_sha16, -> STOP (на ПК, до пуша)."""
    db = _db(tmp_path, _entry("b33_ini"))
    new = tmp_path / "new.txt"
    new.write_text(f"{KEY} | r | 2026-11-24 | b33_ini | 0123456789abcdef\n", encoding="utf-8")
    rc, j = _cli(db, "--new", str(new))
    assert rc == 3 and j["decision"] == "STOP" and j["bad"][0]["why"] == "not_in_journal"


def test_b33_check_lines_answer_0_is_stop(tmp_path):
    db = _db(tmp_path, _entry("b33_no", answer=0))
    new = tmp_path / "new.txt"
    new.write_text(f"{KEY} | r | 2026-11-24 | b33_no | {_e16(db, 'b33_no')}\n", encoding="utf-8")
    rc, j = _cli(db, "--new", str(new))
    assert rc == 3 and j["bad"][0]["why"] == "answer_0"


def test_b33_check_lines_old_lines_are_not_rechecked(tmp_path):
    db = _db(tmp_path, _entry("b33_ini"))
    line = f"{KEY} | r | 2026-11-24 | b33_old | fedcba9876543210\n"
    old, new = tmp_path / "old.txt", tmp_path / "new.txt"
    old.write_text(line, encoding="utf-8")
    new.write_text(line, encoding="utf-8")
    rc, j = _cli(db, "--new", str(new), "--old", str(old))
    assert rc == 0 and j["added"] == 0


def test_b33_check_lines_broken_chain_is_stop(tmp_path):
    db = _db(tmp_path, _entry("b33_ini"), _entry("b33_two"))
    new = tmp_path / "new.txt"
    new.write_text(f"{KEY} | r | 2026-11-24 | b33_ini | {_e16(db, 'b33_ini')}\n", encoding="utf-8")
    con = sqlite3.connect(str(db))
    con.execute("UPDATE consent SET answer = 0 WHERE question_code = 'b33_two'")
    con.commit()
    con.close()
    rc, j = _cli(db, "--new", str(new))
    assert rc == 3 and j["chain_ok"] is False


def test_b33_check_lines_bad_format_is_error(tmp_path):
    db = _db(tmp_path, _entry("b33_ini"))
    new = tmp_path / "new.txt"
    new.write_text(f"{KEY} | r | 2026-11-24 | b33_ini\n", encoding="utf-8")
    rc, j = _cli(db, "--new", str(new))
    assert rc == 2 and "error" in j
