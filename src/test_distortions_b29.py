"""tools/distortions.py: суточные файлы серии по стыкам (схема, put идемпотентно, add по event-id, runs из
run_metrics) и их ряды dist.<стык> в недельном слепке. Только tmp_path; стыки и виды -- нейтральные коды."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

import quarantine_state as qs

TOOL = Path(__file__).resolve().parent.parent / "tools" / "distortions.py"
_spec = importlib.util.spec_from_file_location("distortions_tool", TOOL)
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)


def spec(day="2026-11-18", **kw):
    s = {"day": day, "source": "registry", "runs": None, "green": None, "red": None, "ask": None, "manual": None,
         "series": {"false_red": 2, "true_red_caught": 1}, "by_boundary": {"stage_a_to_b": 1, "clock": 2},
         "reached_services": None, "secrets_leaked": None, "force_push_after_b09": None,
         "notes": ["synthetic note"]}
    s.update(kw)
    return s


def test_put_writes_schema_and_is_idempotent(tmp_path):
    r = ds.put(spec(), tmp_path)
    assert r["written"] and (tmp_path / "distortions_2026-11-18.json").is_file()
    obj = json.loads((tmp_path / "distortions_2026-11-18.json").read_text(encoding="utf-8"))
    assert list(obj) == ["day", "source", "runs", "green", "red", "ask", "manual", "series", "by_boundary",
                         "reached_services", "secrets_leaked", "force_push_after_b09", "notes"]
    assert obj["by_boundary"] == {"clock": 2, "stage_a_to_b": 1} and obj["runs"] is None
    again = ds.put(spec(), tmp_path)
    assert not again["written"] and again["same"]
    with pytest.raises(FileExistsError):
        ds.put(spec(series={"false_red": 3}), tmp_path)
    assert ds.put(spec(series={"false_red": 3}), tmp_path, replace=True)["written"]


@pytest.mark.parametrize("bad", [{"day": "18.11"}, {"source": ""}, {"series": {"Bad Key": 1}},
                                 {"by_boundary": {"x": -1}}, {"by_boundary": {"x": True}}, {"runs": 1.5},
                                 {"notes": ["a\nb"]}, {"notes": ["x" * 201]}, {"runs_from": "text"}])
def test_put_rejects_bad_schema_nothing_written(tmp_path, bad):
    with pytest.raises(ValueError):
        ds.put(spec(**bad), tmp_path)
    assert not list(tmp_path.glob("distortions_*"))


def test_runs_from_run_metrics(tmp_path):
    db = tmp_path / "s.db"
    for i, sig in enumerate((1, 1, 2, 3, 5)):
        qs.record_run({"task": "B20", "block": "run", "started_at": f"2026-11-18T1{i}:00:00",
                       "ended_at": f"2026-11-18T1{i}:01:00", "steps": 1, "rollbacks": 0, "uac": 0, "hitl": 0,
                       "parse_errors": 0, "net_errors": 0, "flaky": 0, "signal": sig}, db)
    qs.record_run({"task": "B20", "block": "run", "started_at": "2026-11-19T10:00:00", "ended_at": "2026-11-19T10:01:00",
                   "steps": 1, "rollbacks": 0, "uac": 0, "hitl": 0, "parse_errors": 0, "net_errors": 0, "flaky": 0,
                   "signal": 1}, db)
    r = ds.put(dict(spec(), runs_from="run_metrics"), tmp_path, db)
    assert (r["runs"], r["green"], r["red"], r["ask"], r["manual"]) == (5, 2, 1, 1, 1)
    with pytest.raises(ValueError):
        ds.put(dict(spec(day="2026-11-20"), runs_from="run_metrics"), tmp_path, None)


def test_add_by_event_id_once(tmp_path):
    assert ds.add(tmp_path, "2026-11-20", "e1", "false_red", "stage_a_to_b")["changed"]
    assert not ds.add(tmp_path, "2026-11-20", "e1", "false_red", "stage_a_to_b")["changed"]
    assert ds.add(tmp_path, "2026-11-20", "e2", None, "stage_a_to_b", 2)["changed"]
    s = ds.summary(tmp_path, "2026-11-20")
    assert s["series"] == {"false_red": 1} and s["by_boundary"] == {"stage_a_to_b": 3}
    with pytest.raises(ValueError):
        ds.add(tmp_path, "2026-11-20", "e3")
    with pytest.raises(ValueError):
        ds.add(tmp_path, "2026-11-20", "bad id with spaces", "false_red")


def test_weekly_distortion_spike_found_quiet_day_not(tmp_path):
    """Искажения: плохо = рост. Всплеск находится, «тихие» сутки (0) точкой не считаются; битый файл пропускается."""
    db = tmp_path / "s.db"
    base = qs.weekly(db, "2026-11-17")
    assert base["n_days"] == 0 and base["dist_days"] == 0
    vals = {"2026-11-11": 2, "2026-11-12": 2, "2026-11-13": 0, "2026-11-14": 2, "2026-11-15": 9, "2026-11-17": 2}
    for day, v in vals.items():
        ds.put(spec(day=day, by_boundary={"stage_a_to_b": v}, series={"false_red": v}), tmp_path)
    (tmp_path / "distortions_2026-11-16.json").write_text("{broken", encoding="utf-8")
    w = qs.weekly(db, "2026-11-17")
    assert w["dist_days"] == 6 and w["dist_skipped"] == 1 and w["n_days"] == 6
    assert w["dips"] == {"2026-11-15": ["dist.stage_a_to_b", "dist_kind.false_red"]}       # 13.11 (0) не точка
    assert w["known"]["dist.stage_a_to_b"] == 6
    z = qs.zoom(w["weekly_uuid"], db)
    assert z["points"][0]["nodes"][0] == {"metric": "dist.stage_a_to_b", "value": 9.0, "median": 2.0, "delta": 7.0,
                                          "direction": "up"}


def test_weekly_day_without_file_is_no_data(tmp_path):
    db = tmp_path / "s.db"
    for day in ("2026-11-15", "2026-11-17"):
        ds.put(spec(day=day), tmp_path)
    qs.record_run({"task": "B20", "block": "run", "started_at": "2026-11-16T10:00:00", "ended_at": "2026-11-16T10:01:00",
                   "steps": 1, "rollbacks": 0, "uac": 0, "hitl": 0, "parse_errors": 0, "net_errors": 0, "flaky": 0,
                   "signal": 1}, db)
    w = qs.weekly(db, "2026-11-17")
    assert w["n_days"] == 3 and w["known"]["dist.stage_a_to_b"] == 2 and "dist.stage_a_to_b" in w["insufficient"]
    assert w["known"]["run.count"] == 1


def test_weekly_dist_dir_explicit_and_absent(tmp_path):
    d = tmp_path / "dist"
    ds.put(spec(day="2026-11-17"), d)
    w0 = qs.weekly(tmp_path / "s.db", "2026-11-17")
    w1 = qs.weekly(tmp_path / "s.db", "2026-11-17", dist_dir=d)
    assert w0["dist_days"] == 0 and w1["dist_days"] == 1 and w0["weekly_uuid"] != w1["weekly_uuid"]


def test_cli_put_day_and_error_code(tmp_path):
    f = tmp_path / "spec.json"
    f.write_text(json.dumps(spec()), encoding="utf-8")
    p = subprocess.run([sys.executable, str(TOOL), "--dir", str(tmp_path / "d"), "put", "--spec", str(f)], capture_output=True)
    assert p.returncode == 0 and json.loads(p.stdout.decode("utf-8"))["written"] is True
    p = subprocess.run([sys.executable, str(TOOL), "--dir", str(tmp_path / "d"), "day", "--day", "2026-11-18"], capture_output=True)
    r = json.loads(p.stdout.decode("utf-8"))
    assert r["found"] and r["series_total"] == 3 and r["boundary_total"] == 3
    f.write_text(json.dumps(spec(series={"X": 1})), encoding="utf-8")
    p = subprocess.run([sys.executable, str(TOOL), "--dir", str(tmp_path / "d"), "put", "--spec", str(f)], capture_output=True)
    assert p.returncode == 2 and "error" in json.loads(p.stdout.decode("utf-8"))
