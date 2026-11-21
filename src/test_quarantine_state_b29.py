"""run_metrics v2: перевод v1 -> v2 (копия базы, одна транзакция, старые строки -> backfill_text_b15), NULL в
дозаписи, недельный слепок до/после перевода тот же, цикл расписания на переведённой копии. Только tmp_path."""
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import quarantine_state as qs

CLI = Path(__file__).resolve().parent.parent / "tools" / "quarantine_state_cli.py"
NOW = dt.datetime(2026, 11, 21, 15, 0, 0)
V1_DDL = """CREATE TABLE run_metrics (
        run_id TEXT PRIMARY KEY, task TEXT NOT NULL, block TEXT NOT NULL, started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL, duration_s REAL NOT NULL, steps INTEGER NOT NULL, rollbacks INTEGER NOT NULL,
        uac INTEGER NOT NULL, hitl INTEGER NOT NULL, parse_errors INTEGER NOT NULL, net_errors INTEGER NOT NULL,
        flaky INTEGER NOT NULL, signal INTEGER NOT NULL, recorded_at TEXT NOT NULL)"""


def rec(start, sig=1, **kw):
    t = dt.datetime.fromisoformat(start)
    r = {"task": "B15", "block": "run", "started_at": start, "ended_at": (t + dt.timedelta(minutes=3)).isoformat(),
         "steps": 5, "rollbacks": 0, "uac": 0, "hitl": 0, "parse_errors": 0, "net_errors": 0, "flaky": 0, "signal": sig}
    r.update(kw)
    return r


def make_v1(db: Path, n: int = 6) -> None:
    con = sqlite3.connect(str(db))
    con.execute(V1_DDL)
    for i in range(n):
        r = rec(f"2026-11-{15 + i % 3:02d}T1{i}:00:00", sig=2 if i == 4 else 1, rollbacks=1 if i == 4 else 0)
        _, _, st, en, dur, vals, sig = qs.validate_run(r)
        con.execute("INSERT INTO run_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (qs.run_id(r["task"], r["block"], st), r["task"], r["block"], st, en, dur,
                     *[vals[k] for k in qs.RUN_COUNTS], sig, "2026-11-17T15:16:02"))
    con.commit()
    con.close()


def test_new_db_is_v2(tmp_path):
    con = qs.connect(tmp_path / "s.db")
    try:
        assert qs.run_schema(con) == 2
        cols = [r[1] for r in con.execute("PRAGMA table_info(run_metrics)")]
        nn = {r[1]: r[3] for r in con.execute("PRAGMA table_info(run_metrics)")}
    finally:
        con.close()
    assert cols[-15:] == ["source", "report_sha16", "outcome", "fail_stage", "fail_code", "attempt", "kind", "boundary",
                          "day", "time_text", "launch_ts", "launch_delay_s", "uac_wait_s", "hitl_wait_s", "retro_conflict"]
    assert nn["steps"] == 0 and nn["outcome"] == 1 and nn["day"] == 1 and nn["kind"] == 1


def test_migrate_v1_backup_then_one_transaction(tmp_path):
    db = tmp_path / "s.db"
    make_v1(db)
    with pytest.raises(ValueError):
        qs.migrate_run_metrics(db, None, now=NOW)
    with pytest.raises(qs.SchemaError):
        qs.record_run(rec("2026-11-21T10:00:00"), db)
    r = qs.migrate_run_metrics(db, tmp_path / "backups", now=NOW)
    assert r == {"migrated": True, "schema": 2, "rows": 6, "backup": "s_v1_20261121_150000.db", "backup_rows": 6}
    b = sqlite3.connect(str(tmp_path / "backups" / r["backup"]))
    assert [x[1] for x in b.execute("PRAGMA table_info(run_metrics)")][-1] == "recorded_at"
    b.close()
    con = sqlite3.connect(str(db))
    src = con.execute("SELECT source, count(*), count(report_sha16) FROM run_metrics GROUP BY source").fetchall()
    oa = con.execute("SELECT substr(started_at, 1, 10), outcome, attempt FROM run_metrics ORDER BY started_at").fetchall()
    con.close()
    assert src == [("backfill_text_b15", 6, 0)]
    assert oa == [("2026-11-15", "GREEN", 1), ("2026-11-15", "GREEN", 2), ("2026-11-16", "GREEN", 1),
                  ("2026-11-16", "RED", 2), ("2026-11-17", "GREEN", 1), ("2026-11-17", "GREEN", 2)]
    again = qs.migrate_run_metrics(db, tmp_path / "backups", now=NOW)
    assert again["migrated"] is False and again["rows"] == 6
    assert qs.record_run(rec("2026-11-21T10:00:00"), db)["new"]


def test_weekly_same_before_and_after_migration(tmp_path):
    db = tmp_path / "s.db"
    make_v1(db)
    before = qs.weekly(db, "2026-11-17", now=NOW)
    qs.migrate_run_metrics(db, tmp_path / "b", now=NOW)
    after = qs.weekly(db, "2026-11-17", now=NOW)
    assert before["weekly_uuid"] == after["weekly_uuid"] and before["dips"] == after["dips"]


def test_backfill_nulls_allowed_only_for_backfill(tmp_path):
    db = tmp_path / "s.db"
    r = rec("2026-11-18T10:00:00", hitl=None, net_errors=None)
    with pytest.raises(ValueError):
        qs.record_run(r, db)                                   # source=block: только целые
    with pytest.raises(ValueError):
        qs.record_run(r, db, source="text")
    out = qs.record_run(r, db, source="backfill", report_sha16="00112233aabbccdd")
    row = qs.read_run(out["run_id"], db)
    assert row["hitl"] is None and row["net_errors"] is None and row["steps"] == 5 and row["source"] == "backfill"
    assert qs.read_run("no-such-id", db) is None and qs.read_run("x", tmp_path / "absent.db") is None


def _series(db):
    con = sqlite3.connect(str(db))
    try:
        return json.loads(con.execute("SELECT aggregate_json FROM quarantine_state_weekly ORDER BY created_at DESC, "
                                      "rowid DESC").fetchone()[0])["series"]
    finally:
        con.close()


def test_weekly_null_is_no_data_not_zero(tmp_path):
    """NULL счётчика -> точка None (нет данных), а не 0; < 3 известных точек -> insufficient, точки не ищутся."""
    db = tmp_path / "a.db"
    for i in range(5):
        day = f"2026-11-{14 + i:02d}"
        nul = i >= 2                                           # 3 суток из 5 -- hitl неизвестен
        qs.record_run(rec(f"{day}T10:00:00", hitl=None if nul else 0, flaky=None if nul else 0), db, source="backfill")
    w = qs.weekly(db, "2026-11-18", now=NOW)
    sa = _series(db)
    assert sa["run.hitl"] == [0.0, 0.0, None, None, None] and sa["run.count"] == [1.0] * 5
    assert w["known"]["run.hitl"] == 2 and "run.hitl" in w["insufficient"] and "run.count" not in w["insufficient"]
    assert sa["run.clean_pct"] == [100.0, 100.0, None, None, None]
    assert not any("run.hitl" in v for v in w["dips"].values())


def test_weekly_null_point_skipped_in_median(tmp_path):
    """Сутки с NULL не тянут медиану к 0: всплеск rollbacks считается по известным точкам."""
    db = tmp_path / "s.db"
    vals = [0, 0, None, 0, 0, 5, None]
    for i, v in enumerate(vals):
        day = f"2026-11-{12 + i:02d}"
        qs.record_run(rec(f"{day}T10:00:00", sig=2 if v else 1, rollbacks=v), db, source="backfill",
                      outcome="ROLLBACK" if v else None)
    w = qs.weekly(db, "2026-11-18", now=NOW)
    assert _series(db)["run.rollbacks"] == [0.0, 0.0, None, 0.0, 0.0, 5.0, None]
    assert w["known"]["run.rollbacks"] == 5 and "run.rollbacks" in w["dips"]["2026-11-17"]
    assert "run.green_pct" in w["dips"]["2026-11-17"] and list(w["dips"]) == ["2026-11-17"]
    # при NULL как 0 медиана та же, но 2 «нуля» -- выдуманные точки: известных было бы 7, а не 5


def test_direction_table_covers_every_series(tmp_path):
    """Каждый ряд, который строит weekly (карантин, все исходы, счётчики, искажения), имеет направление."""
    from test_quarantine_state import make_q
    import importlib.util
    spec = importlib.util.spec_from_file_location("dist_tool", CLI.parent / "distortions.py")
    ds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ds)
    db = tmp_path / "s.db"
    qs.snapshot(make_q(tmp_path), db, now=NOW)
    for i, (oc, sig) in enumerate((("GREEN", 1), ("RED", 2), ("ROLLBACK", 2), ("STOP", 2), ("ASK", 3), ("MANUAL", 5))):
        qs.record_run(rec(f"2026-11-21T1{i}:00:00", sig=sig), db, outcome=oc, fail_stage=None if sig == 1 else "1/2",
                      fail_code=None if sig == 1 else "x", salt=f"{i:02d}" * 8)
    ds.put({"day": "2026-11-21", "source": "t", "series": {"false_red": 1}, "by_boundary": {"stage_a_to_b": 1}}, tmp_path)
    w = qs.weekly(db, "2026-11-21", now=NOW)
    keys = set(w["known"])
    assert {"files", "run.count", "run.stop", "run.manual", "run.uac", "dist.stage_a_to_b", "dist_kind.false_red"} <= keys
    assert w["undirected"] == [] and all(qs.direction(k) in ("up", "down") for k in keys)
    assert qs.direction("run.green_pct") == "down" and qs.direction("run.red") == "up"
    assert qs.direction("dist.stage_a_to_b") == "up" and qs.direction("files") == "down"


def test_series_without_direction_is_reported_red(tmp_path, monkeypatch):
    """Новый ряд без строки в DIRECTIONS не проходит молча: weekly кладёт его в undirected, guard-проверка падает."""
    db = tmp_path / "s.db"
    for i in range(3):
        qs.record_run(rec(f"2026-11-{19 + i}T10:00:00"), db)
    real = qs._run_series
    monkeypatch.setattr(qs, "_run_series", lambda con, a, b: {d: dict(m, **{"run.new_metric": 1.0})
                                                              for d, m in real(con, a, b).items()})
    w = qs.weekly(db, "2026-11-21", now=NOW)
    assert w["undirected"] == ["run.new_metric"] and qs.direction("run.new_metric") is None
    with pytest.raises(AssertionError):
        assert w["undirected"] == [], "ряд без направления"


def test_anomalies_rule_edges():
    assert qs.anomalies([5, None, 5], "up") == ([], 2)                       # < 3 известных
    assert qs.anomalies([10, 10, 10, 20], "up") == ([3], 4)                 # всплеск вверх
    assert qs.anomalies([10, 10, 10, 0], "up") == ([], 4)                   # «тихие» сутки -- не точка
    assert qs.anomalies([10, 10, 10, 0], "down") == ([3], 4)
    assert qs.anomalies([10, None, 10, 10, 6], "down") == ([4], 4)
    assert qs.anomalies([1, 2, 3], "sideways") == ([], 3)


def test_cycle_on_migrated_copy_runs_weekly(tmp_path):
    db = tmp_path / "s.db"
    make_v1(db)
    qs.migrate_run_metrics(db, tmp_path / "b", now=NOW)
    qs.record_run(rec("2026-11-20T10:00:00", hitl=None), db, source="backfill")
    root = tmp_path / "root"
    root.mkdir()
    day = qs.WEEKDAYS[NOW.weekday()]
    c = qs.cycle(root, "knowledge_*", day, db, dry_run=True, now=NOW, log=tmp_path / "q.log")
    assert c["summary"]["dirs"] == 0 and c["summary"]["weekly_uuid"] is not None and c["summary"]["purged"] == 0


def test_cli_migrate_read_run_and_backfill_source(tmp_path):
    db = tmp_path / "s.db"
    make_v1(db, 3)
    f = tmp_path / "r.json"
    f.write_text(json.dumps([dict(rec("2026-11-19T10:00:00", hitl=None), report_sha16="0123456789abcdef")]), encoding="utf-8")
    p = subprocess.run([sys.executable, str(CLI), "--db", str(db), "record-run", "--source", "backfill", str(f)], capture_output=True)
    assert p.returncode == 3 and "v1" in json.loads(p.stdout.decode("utf-8"))["error"]
    p = subprocess.run([sys.executable, str(CLI), "--db", str(db), "migrate", "--backup-dir", str(tmp_path / "bk")], capture_output=True)
    m = json.loads(p.stdout.decode("utf-8"))
    assert p.returncode == 0 and m["migrated"] and m["rows"] == 3 and len(list((tmp_path / "bk").glob("*.db"))) == 1
    p = subprocess.run([sys.executable, str(CLI), "--db", str(db), "record-run", str(f)], capture_output=True)
    assert p.returncode == 2                                  # без --source backfill NULL не принимается
    p = subprocess.run([sys.executable, str(CLI), "--db", str(db), "record-run", "--source", "backfill", str(f)], capture_output=True)
    r = json.loads(p.stdout.decode("utf-8"))
    assert p.returncode == 0 and r["new"] == 1
    p = subprocess.run([sys.executable, str(CLI), "--db", str(db), "read-run", r["run_ids"][0]], capture_output=True)
    row = json.loads(p.stdout.decode("utf-8"))["row"]
    assert row["hitl"] is None and row["source"] == "backfill" and row["report_sha16"] == "0123456789abcdef"


def test_backfill_attempts_and_outcomes(tmp_path):
    db = tmp_path / "s.db"
    a = qs.record_run(rec("2026-11-19T19:07:00", sig=2), db, source="backfill", outcome="ROLLBACK", fail_stage="3/7",
                      salt="00" * 8)
    b = qs.record_run(rec("2026-11-19T19:15:00"), db, source="backfill", salt="11" * 8)
    c = qs.record_run(rec("2026-11-20T09:00:00"), db, source="backfill", salt="22" * 8)       # другие сутки
    assert (a["attempt"], b["attempt"], c["attempt"]) == (1, 2, 1) and a["outcome"] == "ROLLBACK"
    with pytest.raises(ValueError):
        qs.record_run(rec("2026-11-19T20:00:00"), db, outcome="STOP")                  # STOP = signal 2
    with pytest.raises(ValueError):
        qs.record_run(rec("2026-11-19T20:00:00"), db, fail_code="x")                   # GREEN без кода
    with pytest.raises(ValueError):
        qs.record_run(rec("2026-11-19T20:00:00", sig=2), db, fail_code="Has Text")


# --- ретро-разбор (source retro_audit): kind block_run | incident, сопоставление с дозаписью, идемпотентность ---------
def _ev(day="2026-11-19", time="19:07", block="B18n", task="run", outcome="ROLLBACK", fail_code="gate_red",
        boundary="stage_a_to_b", attempt=1, ref="ref:1"):
    return {"day": day, "time": time, "block": block, "task": task, "outcome": outcome, "fail_code": fail_code,
            "boundary": boundary, "source_ref": ref, "note": "описание", "attempt": attempt, "source": "retro_audit"}


def _bf(db, start, end, sig, outcome=None, task="B18n", block="run", salt="aa" * 8):
    r = rec(start, sig=sig, task=task, block=block, hitl=None)
    r["ended_at"] = end
    return qs.record_run(r, db, source="backfill", outcome=outcome, fail_stage=None if sig == 1 else "3/7", salt=salt)


EVENTS = [
    _ev(),                                                                   # совпадает с RED-строкой 19:07 (attempt 1)
    _ev(time="20:41", task="elevated", outcome="NOT_STARTED", fail_code="click_not_launched", attempt=1),
    _ev(time="19:1x", block="X_SRC", task="write", outcome="FAIL", fail_code="write_not_landed", attempt=None,
        boundary="write_to_store"),
    _ev(time="утро", block="Y_SRC", task="talk", outcome="WARN", fail_code="loop_guard", attempt=None),
    _ev(time="09:55", block="B18j?", task="elevated", outcome="NOT_STARTED", fail_code="uac_expired", attempt=1),
    _ev(time="19:15", outcome="ROLLBACK", attempt=2),                        # attempt 2 = GREEN-строка -> не сливать
]


def test_retro_import_counts_match_and_idempotent(tmp_path):
    db = tmp_path / "s.db"
    a = _bf(db, "2026-11-19T19:07:00", "2026-11-19T19:07:40", 2, "ROLLBACK", salt="aa" * 8)
    b = _bf(db, "2026-11-19T19:15:00", "2026-11-19T19:23:00", 1, salt="bb" * 8)
    r = qs.import_retro(EVENTS, db, now=NOW)
    assert (r["total"], r["inserted"], r["matched"], r["rejected"]) == (6, 5, 1, [])
    assert r["inserted"] + r["matched"] + len(r["rejected"]) == len(EVENTS) and r["new_rows"] == 5
    row = qs.read_run(a["run_id"], db)
    assert (row["source"], row["fail_code"], row["boundary"], row["outcome"], row["fail_stage"]) == (
        "backfill+retro_audit", "gate_red", "stage_a_to_b", "ROLLBACK", "3/7")
    assert row["retro_conflict"] is None                                    # исходы совпали -- без пометки
    assert qs.read_run(b["run_id"], db)["fail_code"] is None                 # GREEN не получает причин
    con = sqlite3.connect(str(db))
    n0 = con.execute("SELECT count(*) FROM run_metrics").fetchone()[0]
    con.close()
    again = qs.import_retro(EVENTS, db, now=NOW)
    con = sqlite3.connect(str(db))
    n1 = con.execute("SELECT count(*) FROM run_metrics").fetchone()[0]
    kinds = dict(con.execute("SELECT kind, count(*) FROM run_metrics WHERE source = 'retro_audit' GROUP BY kind"))
    times = con.execute("SELECT time_text, started_at FROM run_metrics WHERE source = 'retro_audit' ORDER BY time_text").fetchall()
    con.close()
    assert n0 == n1 == 7 and again["new_rows"] == 0 and again["matched"] == 1 and again["inserted"] == 5
    assert kinds == {"block_run": 2, "incident": 3}                         # B18j? -- неточный код -> incident
    assert ("утро", None) in times and ("19:1x", None) in times and ("20:41", "2026-11-19T20:41:00") in times


def test_retro_conflict_keeps_machine_outcome(tmp_path):
    db = tmp_path / "s.db"
    a = _bf(db, "2026-11-19T19:07:00", "2026-11-19T19:08:00", 2, "STOP")
    r = qs.import_retro([_ev(outcome="RED")], db, now=NOW)
    assert r["matched"] == 1 and r["conflicts"] == [{"index": 0, "row_outcome": "STOP", "retro_outcome": "RED"}]
    assert qs.read_run(a["run_id"], db)["outcome"] == "STOP" and qs.read_run(a["run_id"], db)["retro_conflict"] == 1


def test_retro_time_must_fit_the_run(tmp_path):
    db = tmp_path / "s.db"
    _bf(db, "2026-11-19T19:07:00", "2026-11-19T19:08:00", 2, "ROLLBACK")
    r = qs.import_retro([_ev(time="19:30")], db, now=NOW)                    # тот же ключ, время вне запуска
    assert r["matched"] == 0 and r["new_rows"] == 1


@pytest.mark.parametrize("bad,frag", [({"source": "other"}, "source"), ({"day": "19.11"}, ""),
                                      ({"block": "B18 n!"}, "block"), ({"outcome": "GREEN"}, "GREEN"),
                                      ({"block": "X", "outcome": "ROLLBACK"}, "outcome"),
                                      ({"fail_code": "Has Text"}, "fail_code"), ({"attempt": 0}, "attempt"),
                                      ({"time": "x" * 17}, "time")])
def test_retro_rejects_with_reason(tmp_path, bad, frag):
    db = tmp_path / "s.db"
    r = qs.import_retro([dict(_ev(), **bad), _ev(time="21:00", attempt=None)], db, now=NOW)
    assert len(r["rejected"]) == 1 and r["rejected"][0]["index"] == 0 and frag in r["rejected"][0]["reason"]
    assert r["inserted"] + r["matched"] + len(r["rejected"]) == 2


def test_weekly_counts_blocks_by_block_run_and_boundaries_by_all(tmp_path):
    db = tmp_path / "s.db"
    for i in range(4):
        _bf(db, f"2026-11-{17 + i}T10:00:00", f"2026-11-{17 + i}T10:05:00", 1, salt=f"{i:02d}" * 8)
    qs.import_retro([_ev(day="2026-11-20", time="11:1x", block="X_SRC", task="write", outcome="FAIL",
                         fail_code="write_not_landed", attempt=None, boundary="write_to_store", ref=f"r{i}")
                     for i in range(5)], db, now=NOW)
    w = qs.weekly(db, "2026-11-20", now=NOW)
    sa = _series(db)
    assert sa["run.count"] == [1.0, 1.0, 1.0, 1.0] and sa["run.incident"] == [0.0, 0.0, 0.0, 5.0]
    assert sa["run_bnd.write_to_store"] == [0.0, 0.0, 0.0, 5.0]
    assert "run.incident" in w["dips"]["2026-11-20"] and w["undirected"] == []


def test_weekly_and_cycle_survive_retro_rows_without_time(tmp_path):
    """Ретро-строка запуска без точного времени (duration NULL) не роняет weekly / ночной цикл: длительность суток --
    нет данных."""
    db = tmp_path / "s.db"
    _bf(db, "2026-11-19T10:00:00", "2026-11-19T10:05:00", 1, salt="cc" * 8)
    qs.import_retro([_ev(time="~11:4x", outcome="RED", attempt=None)], db, now=NOW)
    w = qs.weekly(db, "2026-11-19", now=NOW)
    assert _series(db)["run.duration_s"] == [None] and _series(db)["run.count"] == [2.0] and w["undirected"] == []
    root = tmp_path / "root"
    root.mkdir()
    c = qs.cycle(root, "knowledge_*", qs.WEEKDAYS[NOW.weekday()], db, dry_run=True, now=NOW)
    assert c["summary"]["weekly_uuid"] is not None


def test_cli_import_retro(tmp_path):
    db = tmp_path / "s.db"
    f = tmp_path / "retro.json"
    f.write_text(json.dumps(EVENTS, ensure_ascii=False), encoding="utf-8")
    p = subprocess.run([sys.executable, str(CLI), "--db", str(db), "import-retro", str(f)], capture_output=True)
    r = json.loads(p.stdout.decode("utf-8"))
    assert p.returncode == 0 and r["total"] == 6 and r["inserted"] + r["matched"] == 6



def test_weekly_wait_series_null_not_zero_and_direction_up(tmp_path):
    db = tmp_path / "s.db"
    for i, w in enumerate((5, 5, None, 5, 5, 300)):
        qs.record_run(rec(f"2026-11-{15 + i}T10:00:00"), db, salt=f"{i:02d}" * 8,
                      waits={"uac_wait_s": w, "hitl_wait_s": 0, "launch_delay_s": None})
    w = qs.weekly(db, "2026-11-20", now=NOW)
    sa = _series(db)
    assert sa["run.uac_wait_s"] == [5.0, 5.0, None, 5.0, 5.0, 300.0] and sa["run.hitl_wait_s"] == [0.0] * 6
    assert "run.launch_delay_s" not in sa                                   # нигде не измерялось -- ряда нет
    assert w["known"]["run.uac_wait_s"] == 5 and "run.uac_wait_s" in w["dips"]["2026-11-20"]
    assert all(qs.direction(k) == "up" for k in ("run.uac_wait_s", "run.hitl_wait_s", "run.launch_delay_s"))
    with pytest.raises(ValueError):
        qs.record_run(rec("2026-11-21T10:00:00"), db, waits={"uac_wait_s": -1})
