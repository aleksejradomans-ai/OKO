"""quarantine_state: суточный слепок (детерминизм, идемпотентность, без имён файлов), purge (двойное условие),
недельный слепок (partial / полный), точки спада и зум. Только временные папки и временная SQLite-база."""
import datetime as dt
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import quarantine_state as qs

T0 = dt.datetime(2026, 11, 17, 12, 40, 0)


def make_q(root: Path, extra: int = 0, name: str = "q") -> Path:
    q = root / name
    (q / "archived").mkdir(parents=True)
    (q / "root").mkdir()
    rows = [("archived/a_20260101_000000_000000.txt", "тестовый файл без записи (сирота)", b"aaa"),
            ("archived/b_20260101_000000_000000.txt", "лишняя копия (тот же sha256)", b"bbbb"),
            ("root/doc.pdf", "исходная часть, старая версия", b"%PDF-1.4 x"),
            ("root/doc (2).pdf", "склеен в общий файл", b"%PDF-1.4 yy")]
    for i in range(extra):
        rows.append((f"archived/t{i}.txt", "тест", b"t" * (i + 1)))
    lines = ["path\tsize\tsha256\treason\tmoved_at\tquarantine_path"]
    for rel, reason, data in rows:
        (q / rel).write_bytes(data)
        lines.append(f"knowledge/{rel}\t{len(data)}\t-\t{reason}\t{T0.isoformat()}\t{rel}")
    (q / "manifest.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return q


def test_categories_from_reason():
    assert qs.category("дубль (3) = (2) по sha256") == "duplicate"
    assert qs.category("лишняя копия (тот же sha256)") == "duplicate"
    assert qs.category("склеен в общий файл") == "merge_source"
    assert qs.category("исходная часть, старая версия") == "old_version"
    assert qs.category("archived без записи базы (сирота аудита)") == "test_junk"
    assert qs.category("что-то ещё") == "other"


def test_aggregate_has_no_names_and_counts(tmp_path):
    q = make_q(tmp_path)
    agg = qs.aggregate(q)
    blob = json.dumps(agg, ensure_ascii=False)
    for name in ("a_2026", "b_2026", "doc", "archived/", "root/"):
        assert name not in blob
    assert agg["files"] == 4 and agg["unlisted"] == 0 and agg["manifest_rows"] == 4
    assert {k: v["files"] for k, v in agg["categories"].items()} == {"test_junk": 1, "duplicate": 1,
                                                                     "old_version": 1, "merge_source": 1}
    assert agg["extensions"] == {".txt": 2, ".pdf": 2} and agg["time_max"] == T0.isoformat()


def test_daily_uuid_deterministic_and_snapshot_idempotent(tmp_path):
    q = make_q(tmp_path)
    db = tmp_path / "s.db"
    a = qs.snapshot(q, db, now=T0)
    b = qs.snapshot(q, db, now=T0 + dt.timedelta(hours=1))
    assert a["daily_uuid"] == b["daily_uuid"] and a["new"] is True and b["new"] is False
    assert uuid.UUID(a["daily_uuid"]).version == 5
    h = qs.sha256_hex(qs.canonical(qs.aggregate(q)))
    assert a["aggregate_sha256"] == h
    assert a["daily_uuid"] == str(uuid.uuid5(qs.NAMESPACE, qs.sha256_hex(qs.canonical(
        {"day": "2026-11-17", "dir": str(q.resolve()), "aggregate_sha256": h}))))
    c = qs.snapshot(q, db, now=T0 + dt.timedelta(days=1))                 # другие сутки -- другой слепок
    assert c["daily_uuid"] != a["daily_uuid"] and c["new"] is True
    con = qs.connect(db)
    assert con.execute("SELECT count(*) FROM quarantine_state_snapshots").fetchone()[0] == 2
    con.close()


def test_purge_refuses_without_snapshot(tmp_path):
    q = make_q(tmp_path)
    r = qs.purge(q, tmp_path / "s.db", now=T0 + dt.timedelta(days=3))
    assert r == {"deleted": 0, "reason": "no_snapshot", "aggregate_sha256": r["aggregate_sha256"]} and q.is_dir()


def test_purge_refuses_changed_directory(tmp_path):
    q = make_q(tmp_path)
    db = tmp_path / "s.db"
    qs.snapshot(q, db, now=T0)
    (q / "archived" / "a_20260101_000000_000000.txt").write_bytes(b"changed")
    r = qs.purge(q, db, now=T0 + dt.timedelta(days=3))
    assert r["deleted"] == 0 and r["reason"] == "directory_changed_since_snapshot" and q.is_dir()


def test_purge_refuses_below_ttl(tmp_path):
    q = make_q(tmp_path)
    db = tmp_path / "s.db"
    qs.snapshot(q, db, now=T0)
    r = qs.purge(q, db, now=T0 + dt.timedelta(hours=23, minutes=59))
    assert r["deleted"] == 0 and r["reason"] == "age_below_ttl" and r["ttl_h"] == 24.0 and q.is_dir()


def test_purge_dry_run_then_delete_after_ttl(tmp_path):
    q = make_q(tmp_path)
    db = tmp_path / "s.db"
    du = qs.snapshot(q, db, now=T0)["daily_uuid"]
    r = qs.purge(q, db, dry_run=True, now=T0 + dt.timedelta(hours=24))
    assert r["deleted"] == 0 and r["reason"] == "dry_run_would_delete" and q.is_dir()
    r = qs.purge(q, db, now=T0 + dt.timedelta(hours=24))
    assert r["deleted"] == 1 and r["daily_uuid"] == du and not q.exists()
    con = qs.connect(db)
    assert con.execute("SELECT purged_at FROM quarantine_state_snapshots").fetchone()[0] is not None
    con.close()
    assert qs.purge(q, db, now=T0 + dt.timedelta(days=2)) == {"deleted": 0, "reason": "no_directory"}


def test_purge_refuses_filesystem_root(tmp_path):
    assert qs.purge(Path(Path.cwd().anchor), tmp_path / "s.db")["reason"] == "refused_path"


def _week(tmp_path, counts):
    db = tmp_path / "w.db"
    for i, n in enumerate(counts):
        d = tmp_path / f"d{i}"
        d.mkdir()
        q = make_q(d, extra=n)
        qs.snapshot(q, db, now=dt.datetime(2026, 11, 10 + i, 13, 0))
    return db


def test_weekly_partial_and_deterministic(tmp_path):
    db = _week(tmp_path, [2, 2, 2])
    w1 = qs.weekly(db, end_day="2026-11-16", now=T0)
    w2 = qs.weekly(db, end_day="2026-11-16", now=T0 + dt.timedelta(hours=5))
    assert w1["partial"] is True and w1["n_days"] == 3 and w1["weekly_uuid"] == w2["weekly_uuid"]
    assert w1["dips"] == {}


def test_weekly_full_finds_dip_and_zoom(tmp_path):
    db = _week(tmp_path, [6, 6, 6, 0, 6, 6, 6])
    w = qs.weekly(db, end_day="2026-11-16", now=T0)
    assert w["partial"] is False and w["n_days"] == 7
    assert list(w["dips"]) == ["2026-11-13"] and "files" in w["dips"]["2026-11-13"]
    z = qs.zoom(w["weekly_uuid"], db)
    assert z["found"] is True and len(z["points"]) == 1
    p = z["points"][0]
    assert p["day"] == "2026-11-13" and p["daily_uuid"] == w["daily_uuids"][3]
    node = next(n for n in p["nodes"] if n["metric"] == "files")
    assert node["value"] == 4 and node["median"] == 10 and node["delta"] == -6
    assert any(n["metric"].startswith("cat.test_junk") for n in p["nodes"])
    assert qs.zoom("00000000-0000-0000-0000-000000000000", db)["found"] is False


def test_dips_rule_edges():
    assert qs.dips([5, 5]) == []                       # меньше 3 точек -- правила нет
    assert qs.dips([10, 10, 10, 10]) == []             # нет отклонения
    assert qs.dips([10, 10, 10, 9]) == []              # MAD = 0: масштаб max(1, 5 %) -> 9 не ниже 10 − 3
    assert qs.dips([10, 10, 10, 6]) == [3]
    assert qs.dips([10, 10, 10, 20]) == []             # рост -- не спад


def test_cli_snapshot_and_purge_json(tmp_path):
    q = make_q(tmp_path)
    db = tmp_path / "c.db"
    cli = Path(__file__).resolve().parent.parent / "tools" / "quarantine_state_cli.py"
    out = subprocess.run([sys.executable, str(cli), "--db", str(db), "snapshot", str(q)], capture_output=True, check=True)
    r = json.loads(out.stdout.decode("utf-8"))
    assert r["cmd"] == "snapshot" and r["new"] is True and "doc" not in out.stdout.decode("utf-8")
    out = subprocess.run([sys.executable, str(cli), "--db", str(db), "purge", str(q)], capture_output=True, check=True)
    r = json.loads(out.stdout.decode("utf-8"))
    assert r["deleted"] == 0 and r["reason"] == "age_below_ttl" and q.is_dir()


def test_cycle_fresh_and_expired_dirs(tmp_path):
    root = tmp_path / "backup"
    make_q(root, name="knowledge_fresh")
    old = make_q(root, name="knowledge_old")
    (root / "other_dir").mkdir()                                     # не по маске -- не трогается
    db = tmp_path / "c.db"
    log = tmp_path / "logs" / "qs.log"
    # первый проход «сегодня» (вторник): оба свежие -> слепки, 0 удалений, без weekly
    c1 = qs.cycle(root, "knowledge_*", "Sun", db, now=T0, log=log)
    assert c1["summary"]["dirs"] == 2 and c1["summary"]["snapshots_new"] == 2 and c1["summary"]["purged"] == 0
    assert {d["purge_reason"] for d in c1["dirs"]} == {"age_below_ttl"} and c1["summary"]["weekly_uuid"] is None
    # повтор в те же сутки -- слепки не дублируются
    c2 = qs.cycle(root, "knowledge_*", "Sun", db, now=T0 + dt.timedelta(hours=2), log=log)
    assert c2["summary"]["snapshots_new"] == 0
    assert len(log.read_text(encoding="utf-8").splitlines()) == 6                # 2 каталога + итог, дважды
    # «старый» каталог: переносы 2 суток назад -- новый слепок (агрегат изменился), dry-run сообщает, затем удаление
    man = old / "manifest.tsv"
    man.write_text(man.read_text(encoding="utf-8").replace(T0.isoformat(), (T0 - dt.timedelta(days=2)).isoformat()),
                   encoding="utf-8")
    c3 = qs.cycle(root, "knowledge_*", "Sun", db, dry_run=True, now=T0 + dt.timedelta(hours=3), log=log)
    by = {d["dir_name_sha16"]: d for d in c3["dirs"]}
    k_old, k_fresh = qs.sha256_hex(b"knowledge_old")[:16], qs.sha256_hex(b"knowledge_fresh")[:16]
    assert by[k_old]["purge_reason"] == "dry_run_would_delete" and by[k_fresh]["purge_reason"] == "age_below_ttl"
    c4 = qs.cycle(root, "knowledge_*", "Sun", db, now=T0 + dt.timedelta(hours=3), log=log)
    assert c4["summary"]["purged"] == 1 and not old.exists() and (root / "knowledge_fresh").is_dir()
    assert (root / "other_dir").is_dir()


def test_cycle_purges_expired_and_runs_weekly(tmp_path):
    root = tmp_path / "backup"
    q = make_q(root, name="knowledge_x")
    db = tmp_path / "c.db"
    sunday = dt.datetime(2026, 11, 22, 3, 17)
    c = qs.cycle(root, "knowledge_*", "Sun", db, dry_run=True, now=sunday)
    assert c["dirs"][0]["purge_reason"] == "dry_run_would_delete" and q.is_dir()
    assert c["summary"]["weekly_uuid"] and c["summary"]["weekly_partial"] is True
    c = qs.cycle(root, "knowledge_*", "Sun", db, now=sunday)
    assert c["summary"]["purged"] == 1 and not q.exists()
    assert qs.cycle(root, "knowledge_*", None, db, now=sunday)["summary"]["dirs"] == 0
    with pytest.raises(ValueError):
        qs.cycle(root, "knowledge_*", "Sunday", db, now=sunday)


def test_cli_cycle_lines_and_log(tmp_path):
    root = tmp_path / "backup"
    make_q(root, name="knowledge_a")
    db = tmp_path / "c.db"
    log = tmp_path / "q.log"
    cli = Path(__file__).resolve().parent.parent / "tools" / "quarantine_state_cli.py"
    out = subprocess.run([sys.executable, str(cli), "--db", str(db), "cycle", "--root", str(root), "--weekly-on", "none",
                          "--dry-run", "--log", str(log)], capture_output=True, check=True)
    lines = [json.loads(x) for x in out.stdout.decode("utf-8").splitlines()]
    assert [x["cmd"] for x in lines] == ["cycle_dir", "cycle"] and lines[-1]["dirs"] == 1 and lines[-1]["dry_run"] is True
    assert "knowledge_a" not in out.stdout.decode("utf-8")
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


# --- B15: run_metrics (метрики прогонов блоков) и их ряды в недельном слепке --------------------------------------
def _run(task="B15", block="run", start="2026-11-17T14:00:00", end="2026-11-17T14:05:00", **kw):
    r = {"task": task, "block": block, "started_at": kw.pop("started_at", start), "ended_at": kw.pop("ended_at", end), "steps": 6, "rollbacks": 0, "uac": 0,
         "hitl": 0, "parse_errors": 0, "net_errors": 0, "flaky": 0, "signal": 1}
    r.update(kw)
    return r


def test_record_run_idempotent_and_numbers_only(tmp_path):
    db = tmp_path / "s.db"
    a = qs.record_run(dict(_run(), note="текст отчёта не хранится"), db)
    b = qs.record_run(_run(), db)
    assert a["new"] and not b["new"] and a["run_id"] == b["run_id"] and a["duration_s"] == 300.0
    import sqlite3
    con = sqlite3.connect(str(db))
    cols = [r[1] for r in con.execute("PRAGMA table_info(run_metrics)")]
    rows = con.execute("SELECT * FROM run_metrics").fetchall()
    con.close()
    assert len(rows) == 1 and "flaky" in cols and "note" not in cols
    assert not any(isinstance(v, str) and "текст" in v for v in rows[0])


@pytest.mark.parametrize("bad", [{"task": "b15"}, {"task": "B15; DROP"}, {"block": "Run Block"}, {"steps": -1},
                                 {"steps": "3"}, {"flaky": True}, {"signal": 4}, {"started_at": "вчера"},
                                 {"ended_at": "2026-11-17T13:00:00"}])
def test_record_run_rejects_bad_fields(tmp_path, bad):
    db = tmp_path / "s.db"
    with pytest.raises(ValueError):
        qs.record_run(_run(**bad), db)
    import sqlite3
    con = sqlite3.connect(str(db)) if db.exists() else None
    assert con is None or con.execute("SELECT count(*) FROM run_metrics").fetchone()[0] == 0


def test_weekly_without_runs_unchanged_and_with_runs_finds_dip(tmp_path):
    db = tmp_path / "s.db"
    base = qs.weekly(db, "2026-11-17", now=T0)
    assert base["n_days"] == 0 and base["dips"] == {}
    for i in range(7):                                       # 6 ровных суток и сутки с откатами и FLAKY
        day = (dt.date(2026, 11, 11) + dt.timedelta(days=i)).isoformat()
        bad = day == "2026-11-15"
        qs.record_run(_run(start=f"{day}T10:00:00", end=f"{day}T10:04:00", signal=2 if bad else 1,
                           rollbacks=1 if bad else 0, flaky=2 if bad else 0), db)
        qs.record_run(_run(block="verify", start=f"{day}T11:00:00", end=f"{day}T11:01:00",
                           signal=2 if bad else 1), db)
    w = qs.weekly(db, "2026-11-17", now=T0)
    assert w["n_days"] == 7 and not w["partial"] and w["daily_uuids"] == [None] * 7
    assert list(w["dips"]) == ["2026-11-15"]
    assert {"run.green_pct", "run.clean_pct"} <= set(w["dips"]["2026-11-15"])
    z = qs.zoom(w["weekly_uuid"], db)
    node = [n for n in z["points"][0]["nodes"] if n["metric"] == "run.green_pct"][0]
    assert z["points"][0]["day"] == "2026-11-15" and node["value"] == 0.0 and node["median"] == 100.0
    assert z["points"][0]["aggregate"] is None


def test_weekly_mixes_quarantine_and_runs_same_window(tmp_path):
    db = tmp_path / "s.db"
    q = make_q(tmp_path)
    qs.snapshot(q, db, now=T0)
    qs.record_run(_run(start="2026-11-17T09:00:00", end="2026-11-17T09:02:00"), db)
    w = qs.weekly(db, "2026-11-17", now=T0)
    assert w["n_days"] == 1 and w["daily_uuids"][0] is not None
    import sqlite3
    con = sqlite3.connect(str(db))
    agg = json.loads(con.execute("SELECT aggregate_json FROM quarantine_state_weekly").fetchone()[0])
    con.close()
    assert agg["series"]["run.count"] == [1.0] and agg["series"]["files"] == [4.0]


def test_cli_record_run_list_atomic_validation(tmp_path):
    db = tmp_path / "s.db"
    cli = Path(__file__).resolve().parent.parent / "tools" / "quarantine_state_cli.py"
    f = tmp_path / "runs.json"
    f.write_text(json.dumps([_run(), _run(block="verify")]), encoding="utf-8")
    out = subprocess.run([sys.executable, str(cli), "--db", str(db), "record-run", str(f)], capture_output=True, check=True)
    r = json.loads(out.stdout.decode("utf-8"))
    assert r["records"] == 2 and r["new"] == 2
    out = subprocess.run([sys.executable, str(cli), "--db", str(db), "record-run", str(f)], capture_output=True, check=True)
    assert json.loads(out.stdout.decode("utf-8"))["new"] == 0
    f.write_text(json.dumps([_run(block="st"), _run(signal=9)]), encoding="utf-8")
    p = subprocess.run([sys.executable, str(cli), "--db", str(db), "record-run", str(f)], capture_output=True)
    assert p.returncode == 2 and "signal" in json.loads(p.stdout.decode("utf-8"))["error"]
    import sqlite3
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT count(*) FROM run_metrics").fetchone()[0] == 2      # ни одна запись не записана
    con.close()
