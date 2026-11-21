"""Сквозная нумерация попыток (B30 P6) и «призрак» (B30 P7) в run_metrics. Только временные базы; время --
константы."""
import datetime as dt
import shutil
import sqlite3

import pytest

import block_finish as bf
import quarantine_state as qs

NOW = dt.datetime(2026, 11, 21, 18, 0, 0)


def rec(start, end=None, sig=1, task="B28a", block="run", steps=8, **kw):
    r = {"task": task, "block": block, "started_at": start, "ended_at": end or start, "steps": steps, "rollbacks": 0,
         "uac": 0, "hitl": 0, "parse_errors": 0, "net_errors": 0, "flaky": 0, "signal": sig}
    r.update(kw)
    return r


def _ev(time, outcome="NOT_STARTED", attempt=1, task="run", block="B28a", day="2026-11-21", ref="r"):
    return {"day": day, "time": time, "block": block, "task": task, "outcome": outcome, "fail_code": "click_missed",
            "boundary": "action_to_pc", "source_ref": f"{ref}{attempt}{time}", "note": "x", "attempt": attempt,
            "source": "retro_audit"}


def group(db, task="B28a", block="run"):
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT r.outcome, r.attempt, a.attempt_src, a.attempt_order, r.source FROM run_metrics r "
                           "LEFT JOIN run_attempts a ON a.run_id = r.run_id WHERE r.task = ? AND r.block = ? "
                           "ORDER BY r.attempt", (task, block)).fetchall()
    finally:
        con.close()


def make_b28a_case(db):
    """Как в базе 21.11: дозапись GREEN 01:41 (attempt 1 по отчётам), ретро NOT_STARTED 01:32, 01:32, 01:33 (1, 2, 3)."""
    qs.record_run(rec("2026-11-21T01:41:22", "2026-11-21T01:52:00"), db, source="backfill", salt="aa" * 8, now=NOW)
    qs.import_retro([_ev("01:32", attempt=1), _ev("01:32", attempt=2), _ev("01:33", attempt=3)], db, now=NOW)


def test_b28a_run_not_started_1_2_3_green_4(tmp_path):
    db = tmp_path / "s.db"
    make_b28a_case(db)
    assert group(db) == [("NOT_STARTED", 1, 1, "exact", "retro_audit"), ("NOT_STARTED", 2, 2, "exact", "retro_audit"),
                         ("NOT_STARTED", 3, 3, "exact", "retro_audit"), ("GREEN", 4, 1, "exact", "backfill")]


def test_migrate_existing_rows_with_backup_then_idempotent(tmp_path):
    db = tmp_path / "s.db"
    make_b28a_case(db)
    con = sqlite3.connect(str(db))                       # вернуть вид B29: без боковой таблицы, GREEN = attempt 1
    con.execute("DROP TABLE run_attempts")
    con.execute("UPDATE run_metrics SET attempt = 1 WHERE source = 'backfill'")
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        qs.migrate_attempts(db, None, now=NOW)            # без каталога копии -- ничего
    r = qs.migrate_attempts(db, tmp_path / "bk", now=NOW)
    assert r["migrated"] is True and r["rows"] == 4 and r["backup_rows"] == 4 and r["changed"] == 1
    assert r["backup"] == "s_pre_attempts_20261121_180000.db"
    b = sqlite3.connect(str(tmp_path / "bk" / r["backup"]))
    assert b.execute("SELECT attempt FROM run_metrics WHERE source = 'backfill'").fetchone()[0] == 1   # копия -- до
    b.close()
    assert [x[:2] for x in group(db)] == [("NOT_STARTED", 1), ("NOT_STARTED", 2), ("NOT_STARTED", 3), ("GREEN", 4)]
    again = qs.migrate_attempts(db, tmp_path / "bk2", now=NOW)
    assert again["migrated"] is False and again["changed"] == 0 and not (tmp_path / "bk2").exists()


def test_reimport_retro_still_matches_by_source_attempt(tmp_path):
    """Сопоставление ретро с дозаписью -- по номеру внутри источника (attempt_src), не по сквозному."""
    db = tmp_path / "s.db"
    qs.record_run(rec("2026-11-19T19:07:00", "2026-11-19T19:08:00", sig=2, task="B18n"), db, source="backfill",
                  outcome="ROLLBACK", fail_stage="3/7", salt="aa" * 8, now=NOW)
    ev = _ev("19:07", outcome="ROLLBACK", attempt=1, block="B18n", day="2026-11-19")
    early = _ev("18:50", outcome="NOT_STARTED", attempt=1, block="B18n", day="2026-11-19", task="run", ref="e")
    r1 = qs.import_retro([early], db, now=NOW)                    # раньше по времени -> сдвигает сквозной номер
    assert r1["new_rows"] == 1
    assert [x[:3] for x in group(db, "B18n")] == [("NOT_STARTED", 1, 1), ("ROLLBACK", 2, 1)]
    r2 = qs.import_retro([ev], db, now=NOW)
    assert r2["matched"] == 1 and r2["new_rows"] == 0             # attempt_src 1 = событие attempt 1
    assert qs.import_retro([ev, early], db, now=NOW)["new_rows"] == 0


def test_unknown_time_goes_last_and_is_approx(tmp_path):
    db = tmp_path / "s.db"
    qs.record_run(rec("2026-11-18T23:35:08", "2026-11-18T23:36:20", task="B18h", block="elevated"), db,
                  source="backfill", salt="bb" * 8, now=NOW)
    qs.import_retro([_ev("23:3x", attempt=1, block="B18h", task="elevated", day="2026-11-18"),
                     _ev("утро", attempt=None, block="B18h", task="elevated", day="2026-11-18", ref="m")], db, now=NOW)
    assert [(x[0], x[1], x[3]) for x in group(db, "B18h", "elevated")] == [
        ("NOT_STARTED", 1, "approx"), ("GREEN", 2, "exact"), ("NOT_STARTED", 3, "approx")]


@pytest.mark.parametrize("tt,key", [("23:3x", "2026-11-18T23:30:00"), ("~20:xx", "2026-11-18T20:00:00"),
                                    ("17:34-17:44", "2026-11-18T17:34:00"), ("~11:4x", "2026-11-18T11:40:00"),
                                    ("утро", None)])
def test_order_key_from_time_text(tt, key):
    assert qs._order_key("2026-11-18", None, tt) == (key, "approx")
    assert qs._order_key("2026-11-18", "2026-11-18T01:02:03", tt) == ("2026-11-18T01:02:03", "exact")


def test_block_rows_attempt_counts_all_sources(tmp_path):
    """Новая строка блока после ретро-событий того же дня получает сквозной номер (клики без запуска считаются)."""
    db = tmp_path / "s.db"
    qs.import_retro([_ev("10:00", attempt=1, block="B30"), _ev("10:01", attempt=2, block="B30")], db, now=NOW)
    r = qs.record_run(rec("2026-11-21T10:05:00", "2026-11-21T10:20:00", task="B30"), db, salt="cc" * 8, now=NOW)
    assert r["attempt"] == 3 and qs.attempt_info(r["run_id"], db) == {"attempt_src": 1, "attempt_order": "exact"}


def _green(db, start, dur, task, block="run", steps=8, sig=1, salt="00", src="block", outcome=None):
    end = (dt.datetime.fromisoformat(start) + dt.timedelta(seconds=dur)).isoformat()
    return qs.record_run(rec(start, end, sig=sig, task=task, block=block, steps=steps), db, source=src,
                         salt=salt * 16, outcome=outcome, fail_stage=None if sig == 1 else "1/2",
                         fail_code=None if sig == 1 else "x_red", now=NOW)


def test_ghost_best_green_7_days_and_comparison(tmp_path):
    db = tmp_path / "s.db"
    _green(db, "2026-11-12T10:00:00", 100, "B10", salt="01")                 # старше 7 суток -- не эталон
    _green(db, "2026-11-19T10:00:00", 400, "B26", salt="02", steps=9)
    _green(db, "2026-11-20T10:00:00", 300, "B27", salt="03", steps=8)        # лучший run
    _green(db, "2026-11-20T11:00:00", 50, "B27", salt="04", sig=2)          # RED -- не эталон
    _green(db, "2026-11-20T12:00:00", 20, "B27", block="push_stage", salt="05", steps=6)
    cur = _green(db, "2026-11-21T10:00:00", 360, "B30", salt="06", steps=9)
    g = qs.ghost(db, now=NOW, current_run_id=cur["run_id"])
    assert g["run"]["best"] == {"task": "B27", "started_at": "2026-11-20T10:00:00", "duration_s": 300.0, "attempt": 1,
                                "steps": 8}
    assert g["run"]["n_green"] == 2 and g["run"]["current"]["task"] == "B30"
    assert g["run"]["vs"] == {"duration": "slower", "duration_delta_s": 60.0, "attempts": "equal", "steps": "more"}
    assert g["stage"]["best"]["duration_s"] == 20.0 and g["stage"]["current"] is None and g["stage"]["vs"] is None
    assert g["promote"]["best"] is None and g["elevated"]["n_green"] == 0
    line = bf.ghost_line(cur["run_id"], "run", db, NOW)
    assert line.startswith("GHOST kind=run n_green_7d=2 best_task=B27 ") and " duration=slower delta_s=60 " in line


def test_ghost_tie_prefers_fewer_attempts_and_retro_is_not_reference(tmp_path):
    db = tmp_path / "s.db"
    qs.import_retro([_ev("09:00", attempt=1, block="B28a")], db, now=NOW)    # NOT_STARTED до GREEN -> attempt 2
    _green(db, "2026-11-21T09:30:00", 200, "B28a", salt="01")
    _green(db, "2026-11-21T09:00:30", 200, "B29", salt="02")                 # та же длительность, attempt 1
    g = qs.ghost(db, now=NOW)
    assert g["run"]["best"]["task"] == "B29" and g["run"]["best"]["attempt"] == 1


def test_finish_prints_ghost_line_and_signal_stays_last(tmp_path):
    db = tmp_path / "s.db"
    _green(db, "2026-11-20T10:00:00", 300, "B27", salt="03")
    lines = [f"METRIC {k}={v}" for k, v in {
        "task": "B30", "block": "run", "started_at": "2026-11-21T10:00:00", "ended_at": "2026-11-21T10:04:00",
        "steps": 7, "rollbacks": 0, "uac": 0, "hitl": 0, "parse_errors": 0, "net_errors": 0, "flaky": 0,
        "signal_wanted": 1, "report_sha16": "0123456789abcdef", "run_nonce": "ab" * 16, "outcome": "GREEN",
        "fail_stage": "", "fail_code": "", "launch_ts": "2026-11-21T09:59:58", "launch_delay_s": "",
        "uac_wait_s": "0", "hitl_wait_s": "0"}.items()]
    r = bf.finish(lines, db, now=NOW)
    assert r["signal"] == 1 and r["ghost"].startswith("GHOST kind=run ") and " duration=faster delta_s=-60 " in r["ghost"]


def test_ghost_failure_does_not_change_signal(tmp_path, monkeypatch):
    db = tmp_path / "s.db"

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(qs, "ghost", boom)
    assert bf.ghost_line("rid", "run", db, NOW) == "GHOST kind=run error=RuntimeError"
    assert bf.ghost_line("rid", "probe", db, NOW) is None                   # не вид «призрака» -- строки нет


def test_weekly_carries_ghost_outside_uuid(tmp_path):
    db = tmp_path / "s.db"
    _green(db, "2026-11-20T10:00:00", 300, "B27", salt="03")
    a = qs.weekly(db, "2026-11-21", now=NOW)
    shutil.copy(db, tmp_path / "t.db")
    b = qs.weekly(tmp_path / "t.db", "2026-11-21", now=NOW)
    assert a["ghost"]["run"]["best"]["task"] == "B27" and a["weekly_uuid"] == b["weekly_uuid"]
