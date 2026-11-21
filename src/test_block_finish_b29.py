"""block_finish: GREEN только после записи строки run_metrics и чтения её назад; иначе ASK; RED остаётся RED.
Только временные базы (tmp_path)."""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import block_finish as bf
import quarantine_state as qs

CLI = Path(__file__).resolve().parent.parent / "tools" / "block_finish.py"
V1_DDL = """CREATE TABLE run_metrics (
        run_id TEXT PRIMARY KEY, task TEXT NOT NULL, block TEXT NOT NULL, started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL, duration_s REAL NOT NULL, steps INTEGER NOT NULL, rollbacks INTEGER NOT NULL,
        uac INTEGER NOT NULL, hitl INTEGER NOT NULL, parse_errors INTEGER NOT NULL, net_errors INTEGER NOT NULL,
        flaky INTEGER NOT NULL, signal INTEGER NOT NULL, recorded_at TEXT NOT NULL)"""


OUT = {1: "GREEN", 2: "RED", 3: "ASK", 5: "MANUAL"}
NONCE = "0123456789abcdef0123456789abcdef"


def lines(**kw):
    m = {"task": "B29", "block": "run", "started_at": "2026-11-21T15:00:00", "ended_at": "2026-11-21T15:09:30",
         "steps": 9, "rollbacks": 0, "uac": 0, "hitl": 0, "parse_errors": 0, "net_errors": 0, "flaky": 0,
         "signal_wanted": 1, "report_sha16": "0123456789abcdef", "run_nonce": NONCE, "outcome": None,
         "fail_stage": "", "fail_code": "", "launch_ts": "2026-11-21T14:59:58", "launch_delay_s": "",
         "uac_wait_s": "0", "hitl_wait_s": "0"}
    m.update(kw)
    if m["outcome"] is None:
        m["outcome"] = OUT.get(m["signal_wanted"], "RED") if isinstance(m["signal_wanted"], int) else "RED"
    return ["--- [step 1 of 9] text lines are ignored", "SIGNAL: 1 - GREEN; a text line is never read"] + [
        f"METRIC {k}={v}" for k, v in m.items() if v is not None]


def count(db):
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT count(*) FROM run_metrics").fetchone()[0]
    finally:
        con.close()


def test_green_only_after_write_and_read_back(tmp_path):
    db = tmp_path / "s.db"
    r = bf.finish(lines(), db)
    assert r["recorded"] and r["signal"] == 1 and r["line"].startswith("SIGNAL: 1 - GREEN; metrics_recorded run_id=")
    row = qs.read_run(r["run_id"], db)
    assert row["source"] == "block" and row["report_sha16"] == "0123456789abcdef" and row["steps"] == 9
    assert row["duration_s"] == 570.0 and row["signal"] == 1 and count(db) == 1


def test_idempotent_run_id_same_result(tmp_path):
    db = tmp_path / "s.db"
    a = bf.finish(lines(), db)
    b = bf.finish(lines(), db)
    assert a["run_id"] == b["run_id"] and a["new"] is True and b["new"] is False
    assert b["signal"] == 1 and count(db) == 1


def test_same_run_other_fields_is_not_green(tmp_path):
    db = tmp_path / "s.db"
    assert bf.finish(lines(), db)["signal"] == 1
    r = bf.finish(lines(steps=10, report_sha16="fedcba9876543210"), db)    # тот же run_id, другие поля
    assert r["signal"] == 3 and not r["recorded"] and "fields_differ:" in r["reason"]
    assert "report_sha16" in r["reason"] and "steps" in r["reason"]
    assert r["line"].startswith("SIGNAL: 3 - ASK; metrics_not_recorded (fields_differ:")


def test_db_unavailable_is_not_green(tmp_path):
    d = tmp_path / "db_is_a_directory"
    d.mkdir()
    r = bf.finish(lines(), d)
    assert r["signal"] == 3 and r["status"] == "ASK" and not r["recorded"] and r["reason"].startswith("db_error")


def test_row_missing_after_write_is_not_green(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    monkeypatch.setattr(qs, "read_run", lambda rid, db=None: None)        # запись «прошла», строки нет
    r = bf.finish(lines(), db)
    assert r["signal"] == 3 and r["reason"] == "row_not_read_back"


def test_fields_differ_on_read_back_is_not_green(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    real = qs.read_run
    monkeypatch.setattr(qs, "read_run", lambda rid, db=None: dict(real(rid, db), uac=1))
    r = bf.finish(lines(), db)
    assert r["signal"] == 3 and r["reason"] == "fields_differ:uac"


def test_v1_schema_is_not_green_and_untouched(tmp_path):
    db = tmp_path / "s.db"
    con = sqlite3.connect(str(db))
    con.execute(V1_DDL)
    con.commit()
    con.close()
    r = bf.finish(lines(), db)
    assert r["signal"] == 3 and r["reason"] == "db_schema_v1_not_migrated" and count(db) == 0


@pytest.mark.parametrize("kw,frag", [({"steps": None}, "нет ключей"), ({"uac": "-1"}, "uac"),
                                     ({"flaky": "1.5"}, "flaky"), ({"task": "b29"}, "task"),
                                     ({"report_sha16": "XYZ"}, "report_sha16"),
                                     ({"ended_at": "2026-11-21T14:00:00"}, "ended_at")])
def test_bad_metric_lines_not_green_nothing_written(tmp_path, kw, frag):
    db = tmp_path / "s.db"
    r = bf.finish(lines(**kw), db)
    assert r["signal"] == 3 and not r["recorded"] and frag in r["reason"]
    assert not db.exists() or count(db) == 0


def test_unknown_and_repeated_keys_rejected(tmp_path):
    db = tmp_path / "s.db"
    assert "неизвестный" in bf.finish(lines() + ["METRIC mood=1"], db)["reason"]
    assert "повторён" in bf.finish(lines() + ["METRIC steps=9"], db)["reason"]
    assert not db.exists() or count(db) == 0


@pytest.mark.parametrize("want,name", [(2, "RED"), (3, "ASK"), (5, "MANUAL")])
def test_non_green_stays_and_is_recorded(tmp_path, want, name):
    db = tmp_path / "s.db"
    r = bf.finish(lines(signal_wanted=want, fail_stage="3/9", fail_code="gate_red"), db)
    assert r["signal"] == want and r["recorded"] and r["line"].startswith(f"SIGNAL: {want} - {name}; metrics_recorded")
    assert qs.read_run(r["run_id"], db)["signal"] == want


def test_red_never_becomes_green_even_if_db_fails(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    r = bf.finish(lines(signal_wanted=2), d)
    assert r["signal"] == 2 and not r["recorded"] and r["line"].startswith("SIGNAL: 2 - RED; metrics_not_recorded")


def test_missing_or_bad_signal_wanted_is_red(tmp_path):
    db = tmp_path / "s.db"
    assert bf.finish(lines(signal_wanted=None), db)["signal"] == 2
    assert bf.finish(lines(signal_wanted=4), db)["signal"] == 2
    assert bf.finish([], db)["signal"] == 2


def test_cli_prints_signal_last_and_rc(tmp_path):
    db = tmp_path / "s.db"
    f = tmp_path / "m.txt"
    f.write_text("\n".join(lines()) + "\n", encoding="utf-8")
    p = subprocess.run([sys.executable, str(CLI), "--metrics", str(f), "--db", str(db)], capture_output=True)
    out = p.stdout.decode("utf-8").splitlines()
    assert p.returncode == 0 and out[0].startswith("BLOCK_FINISH recorded=True") and out[-1].startswith("SIGNAL: 1 - GREEN")
    bad = tmp_path / "dir"
    bad.mkdir()
    p = subprocess.run([sys.executable, str(CLI), "--metrics", str(f), "--db", str(bad)], capture_output=True)
    out = p.stdout.decode("utf-8").splitlines()
    assert p.returncode == 1 and out[-1].startswith("SIGNAL: 3 - ASK; metrics_not_recorded")
    p = subprocess.run([sys.executable, str(CLI), "--metrics", str(tmp_path / "none.txt"), "--db", str(db)],
                       capture_output=True)
    assert p.returncode == 1 and p.stdout.decode("utf-8").splitlines()[-1].startswith("SIGNAL: 2 - RED")


def test_cli_copy_outside_repo_uses_given_db(tmp_path):
    """Копия CLI вне репозитория (рядом src/) пишет только в базу из --db."""
    root = tmp_path / "copy"
    (root / "tools").mkdir(parents=True)
    (root / "src").mkdir()
    shutil.copy(CLI, root / "tools" / "block_finish.py")
    for m in ("block_finish.py", "quarantine_state.py"):
        shutil.copy(Path(__file__).resolve().parent / m, root / "src" / m)
    db = tmp_path / "given.db"
    f = tmp_path / "m.txt"
    f.write_text("\n".join(lines()) + "\n", encoding="utf-8")
    p = subprocess.run([sys.executable, "-I", str(root / "tools" / "block_finish.py"), "--metrics", str(f), "--db", str(db)],
                       capture_output=True)
    assert p.returncode == 0 and count(db) == 1 and not (root / "data").exists()


@pytest.mark.parametrize("outcome,want,stage,code", [("GREEN", 1, "", ""), ("RED", 2, "5/9", "suite_red"),
                                                     ("ASK", 3, "8/9", "restart_closure"),
                                                     ("ROLLBACK", 2, "3/9", "block_tests_red"),
                                                     ("STOP", 2, "0/9", "dryparse")])
def test_every_outcome_written_and_read_back(tmp_path, outcome, want, stage, code):
    db = tmp_path / "s.db"
    r = bf.finish(lines(signal_wanted=want, outcome=outcome, fail_stage=stage, fail_code=code), db)
    assert r["recorded"] and r["signal"] == want and r["outcome"] == outcome and r["attempt"] == 1
    row = qs.read_run(r["run_id"], db)
    assert row["outcome"] == outcome and row["signal"] == want and row["attempt"] == 1
    assert (row["fail_stage"], row["fail_code"]) == ((None, None) if outcome == "GREEN" else (stage, code))


def test_red_then_green_two_rows_attempts_1_and_2(tmp_path):
    db = tmp_path / "s.db"
    a = bf.finish(lines(signal_wanted=2, outcome="ROLLBACK", fail_stage="3/9", fail_code="block_tests_red",
                        started_at="2026-11-21T15:00:00", ended_at="2026-11-21T15:04:00",
                        run_nonce="aa" * 16), db)
    b = bf.finish(lines(started_at="2026-11-21T15:10:00", ended_at="2026-11-21T15:19:00", run_nonce="bb" * 16), db)
    assert a["signal"] == 2 and b["signal"] == 1 and a["run_id"] != b["run_id"] and count(db) == 2
    ra, rb = qs.read_run(a["run_id"], db), qs.read_run(b["run_id"], db)
    assert (ra["attempt"], ra["outcome"], ra["fail_stage"], ra["fail_code"]) == (1, "ROLLBACK", "3/9", "block_tests_red")
    assert (rb["attempt"], rb["outcome"], rb["fail_stage"], rb["fail_code"]) == (2, "GREEN", None, None)


def test_same_start_other_nonce_is_own_row_never_overwritten(tmp_path):
    db = tmp_path / "s.db"
    a = bf.finish(lines(signal_wanted=2, fail_stage="1/9", fail_code="error", run_nonce="cc" * 16), db)
    b = bf.finish(lines(run_nonce="dd" * 16), db)                  # то же время старта, другой запуск
    assert a["run_id"] != b["run_id"] and count(db) == 2
    again = bf.finish(lines(signal_wanted=2, fail_stage="1/9", fail_code="error", run_nonce="cc" * 16), db)
    assert again["new"] is False and again["attempt"] == 1 and count(db) == 2
    assert qs.read_run(a["run_id"], db)["outcome"] == "RED"


def test_stop_before_counters_null_fields_recorded(tmp_path):
    db = tmp_path / "s.db"
    r = bf.finish(lines(signal_wanted=2, outcome="STOP", steps="", rollbacks="", uac="", hitl="", parse_errors="",
                        net_errors="", flaky="", fail_stage="init", fail_code="helper_hash"), db)
    row = qs.read_run(r["run_id"], db)
    assert r["recorded"] and r["signal"] == 2 and row["steps"] is None and row["fail_code"] == "helper_hash"


def test_green_with_null_counter_or_fail_code_is_not_green(tmp_path):
    db = tmp_path / "s.db"
    assert bf.finish(lines(hitl=""), db)["signal"] == 3
    assert bf.finish(lines(fail_code="x"), db)["signal"] == 3
    assert bf.finish(lines(outcome="RED"), db)["signal"] == 3          # outcome не согласован с signal_wanted
    assert bf.finish(lines(run_nonce="short"), db)["signal"] == 3
    assert not db.exists() or count(db) == 0


def test_two_runs_same_second_get_attempts_1_and_2(tmp_path):
    db = tmp_path / "s.db"
    a = bf.finish(lines(signal_wanted=2, fail_stage="1/9", fail_code="error", run_nonce="ee" * 16), db)
    b = bf.finish(lines(run_nonce="ff" * 16), db)
    assert (a["attempt"], b["attempt"]) == (1, 2) and count(db) == 2


def test_wait_metrics_written_and_read_back_null_is_not_zero(tmp_path):
    db = tmp_path / "s.db"
    a = bf.finish(lines(uac_wait_s="37.25", hitl_wait_s="15", launch_delay_s="120", run_nonce="a1" * 16), db)
    row = qs.read_run(a["run_id"], db)
    assert a["signal"] == 1 and (row["uac_wait_s"], row["hitl_wait_s"], row["launch_delay_s"]) == (37.25, 15.0, 120.0)
    assert row["launch_ts"] == "2026-11-21T14:59:58"
    b = bf.finish(lines(started_at="2026-11-21T16:00:00", ended_at="2026-11-21T16:01:00", uac_wait_s="", hitl_wait_s="",
                        launch_ts="", run_nonce="b1" * 16), db)
    rb = qs.read_run(b["run_id"], db)
    assert b["signal"] == 1 and rb["uac_wait_s"] is None and rb["hitl_wait_s"] is None and rb["launch_ts"] is None
    assert bf.finish(lines(uac_wait_s="-1", run_nonce="c1" * 16), db)["signal"] == 3
    assert bf.finish(lines(hitl_wait_s="abc", run_nonce="d1" * 16), db)["signal"] == 3


def test_uac_expired_is_not_started_signal_2(tmp_path):
    db = tmp_path / "s.db"
    r = bf.finish(lines(signal_wanted=2, outcome="NOT_STARTED", fail_stage="1/2", fail_code="uac_expired",
                        uac_wait_s="120", run_nonce="e1" * 16), db)
    row = qs.read_run(r["run_id"], db)
    assert r["signal"] == 2 and row["outcome"] == "NOT_STARTED" and row["fail_code"] == "uac_expired"
    assert row["uac_wait_s"] == 120.0
