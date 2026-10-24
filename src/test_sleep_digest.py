"""Тесты M7.1/M7.2: ops_db + sleep_digest (без LLM, без сети, временные файлы)."""
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import ops_db
import sleep_digest as sd
from ops_db import Cursor, OpsDB

T0 = datetime(2026, 10, 1, 12, 0, 0)


def ln(dt, level, name, msg, ms=0):
    return f"{dt:%Y-%m-%d %H:%M:%S},{ms:03d} [{level}] {name}: {msg}"


def metrics_msg(ce=0.0, ad=1.0, rl=0.0, cjk=0.0, length=300, point="B", intent="inform"):
    return (f"[METRICS] point={point} ce={ce:.2f} ad={ad:.2f} rl={rl:.2f} "
            f"intent={intent}(1.00) cyr=0.87 lat=0.13 cjk={cjk:.2f} len={length}")


def write(path: Path, lines, mode="a", enc="utf-8"):
    with open(path, mode, encoding=enc, newline="") as f:
        for l in lines:
            f.write(l + "\r\n")


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "okobot_stderr.log", tmp_path / "data" / "ops.db"


# --- разбор -------------------------------------------------------------------
def test_parse_line_and_continuation():
    p = sd.parse_line(ln(T0, "INFO", "src.governor", "[METRICS] point=B", 123))
    assert p is not None and p[1] == "INFO" and p[2] == "src.governor"
    assert abs(p[0] - (time.mktime(T0.timetuple()) + 0.123)) < 1e-6
    assert sd.parse_line("Traceback (most recent call last):") is None
    assert sd.parse_line('  File "bot.py", line 1') is None


def test_parse_metrics_handoff_line():
    m = sd.parse_metrics("[METRICS] point=B ce=0.00 ad=1.00 rl=0.00 intent=inform(1.00) "
                         "cyr=0.87 lat=0.13 cjk=0.00 len=336")
    assert m == {"point": "B", "ce": 0.0, "ad": 1.0, "rl": 0.0, "cyr": 0.87, "lat": 0.13,
                 "cjk": 0.0, "len": 336.0, "intent": "inform"}
    assert sd.parse_metrics("[METRICS] point=B ce=x") is None
    assert sd.parse_metrics("[METRICS_FAIL] boom") is None


def test_digest_counts_levels_tags_err_codes():
    lines = [
        ln(T0, "INFO", "governor", metrics_msg(cjk=0.1, ad=0.5, rl=0.3)),
        ln(T0, "WARNING", "governor", "[TRANSPORT_GATE] CLOSED->OPEN step=0 pause=6s"),
        ln(T0, "WARNING", "bot", "[TG_TRANSPORT] NetworkError"),
        ln(T0, "INFO", "bot", "reply [ERR: LLM_UNAVAILABLE] sent"),
        ln(T0, "ERROR", "bot", "boom"),
        "Traceback (most recent call last):",
    ]
    d = sd.build_digest(lines)
    assert d["lines_total"] == 6 and d["lines_unparsed"] == 1
    assert d["levels"] == {"INFO": 2, "WARNING": 2, "ERROR": 1}
    assert d["tags"] == {"METRICS": 1, "TRANSPORT_GATE": 1, "TG_TRANSPORT": 1}
    assert d["err_codes"] == {"LLM_UNAVAILABLE": 1}
    m = d["metrics"]
    assert m["n"] == 1 and m["share"] == {"cjk_pos": 1, "ad_low": 1, "rl_pos": 1}
    assert m["points"] == {"B": 1} and m["intent"] == {"inform": 1}


def test_tag_only_at_message_start_and_no_message_text_in_payload():
    secret = "мой пароль hunter2 [SECRET_TAG] тут"
    d = sd.build_digest([ln(T0, "INFO", "bot", f"incoming: {secret}")])
    assert d["tags"] == {}
    blob = json.dumps(d, ensure_ascii=False)
    assert "hunter2" not in blob and "SECRET_TAG" not in blob and "incoming" not in blob


def test_bad_metrics_line_counted_not_crash():
    d = sd.build_digest([ln(T0, "INFO", "governor", "[METRICS] point=B ce=oops")])
    assert d["metrics"]["n"] == 0 and d["metrics"]["bad"] == 1


# --- чтение лога ---------------------------------------------------------------
def test_incremental_read_only_new_lines(paths):
    logf, _ = paths
    write(logf, [ln(T0, "INFO", "a", "one"), ln(T0, "INFO", "a", "two")], mode="w")
    r1 = sd.read_new_lines(logf, None)
    assert [l[-3:] for l in r1.lines] == ["one", "two"] and not r1.rotated
    write(logf, [ln(T0, "INFO", "a", "three")])
    r2 = sd.read_new_lines(logf, r1.cursor)
    assert len(r2.lines) == 1 and r2.lines[0].endswith("three") and not r2.rotated
    assert sd.read_new_lines(logf, r2.cursor).lines == []


def test_partial_last_line_not_consumed(paths):
    logf, _ = paths
    write(logf, [ln(T0, "INFO", "a", "full")], mode="w")
    with open(logf, "a", encoding="utf-8") as f:
        f.write(ln(T0, "INFO", "a", "half"))
    r = sd.read_new_lines(logf, None)
    assert len(r.lines) == 1 and r.lines[0].endswith("full")
    with open(logf, "a", encoding="utf-8") as f:
        f.write("-done\n")
    r2 = sd.read_new_lines(logf, r.cursor)
    assert len(r2.lines) == 1 and r2.lines[0].endswith("half-done")


def test_rotation_detected_by_shrink_and_by_head(paths):
    logf, _ = paths
    write(logf, [ln(T0, "INFO", "a", "x" * 100)] * 5, mode="w")
    r = sd.read_new_lines(logf, None)
    write(logf, [ln(T0, "INFO", "b", "new")], mode="w")  # файл короче курсора
    r2 = sd.read_new_lines(logf, r.cursor)
    assert r2.rotated and len(r2.lines) == 1
    write(logf, [ln(T0 + timedelta(days=1), "INFO", "c", "y" * 2000)], mode="w")  # длиннее, другое начало
    r3 = sd.read_new_lines(logf, r2.cursor)
    assert r3.rotated and len(r3.lines) == 1


def test_cp1251_fallback_counted(paths):
    logf, _ = paths
    write(logf, [ln(T0, "WARNING", "bot", "[TG_TRANSPORT] сеть")], mode="w", enc="cp1251")
    r = sd.read_new_lines(logf, None)
    assert r.decode_fallback == 1 and r.lines[0].endswith("сеть")


def test_max_bytes_cap_continues_next_run(paths):
    logf, _ = paths
    write(logf, [ln(T0, "INFO", "a", f"line{i:03d}") for i in range(100)], mode="w")
    r = sd.read_new_lines(logf, None, max_bytes=1000)
    assert r.truncated and 0 < len(r.lines) < 100
    seen = list(r.lines)
    cur = r.cursor
    while True:
        rr = sd.read_new_lines(logf, cur, max_bytes=1000)
        if not rr.lines:
            break
        seen += rr.lines
        cur = rr.cursor
    assert [s[-7:] for s in seen] == [f"line{i:03d}" for i in range(100)]


# --- ops.db --------------------------------------------------------------------
def test_ops_db_wal_and_schema_guard(paths):
    _, opsf = paths
    OpsDB(opsf)
    c = sqlite3.connect(opsf)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError):
        OpsDB(opsf)


def test_digest_and_cursor_atomic(paths, monkeypatch):
    _, opsf = paths
    ops = OpsDB(opsf)
    ops.save_cursor("src", Cursor(10, "h", 1))
    orig = OpsDB._put_cursor

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O")
    monkeypatch.setattr(OpsDB, "_put_cursor", staticmethod(boom))
    with pytest.raises(sqlite3.OperationalError):
        ops.commit_digest("src", Cursor(99, "h2", 2), 1.0, 2.0, 5, 1, {"metrics": {}}, None, "x")
    monkeypatch.setattr(OpsDB, "_put_cursor", staticmethod(orig))
    assert ops.recent_digests() == []
    assert ops.get_cursor("src").offset == 10


def test_baseline_freezes_once(paths):
    _, opsf = paths
    ops = OpsDB(opsf)
    assert ops.freeze_baseline(1.0, 2.0, 50, {"a": 1}) is True
    assert ops.freeze_baseline(3.0, 4.0, 99, {"a": 2}) is False
    assert ops.get_baseline()["samples"] == 50


# --- прогон и дрейф ------------------------------------------------------------
def day_lines(day, n, **kw):
    base = T0 + timedelta(days=day)
    return [ln(base + timedelta(minutes=i), "INFO", "governor", metrics_msg(**kw)) for i in range(n)]


def test_dry_run_creates_nothing(paths):
    logf, opsf = paths
    write(logf, day_lines(0, 5), mode="w")
    res = sd.run_once(logf, opsf, dry_run=True)
    assert res["status"] == "dry_run" and not opsf.exists()
    assert res["digest"]["metrics"]["n"] == 5


def test_run_once_writes_then_no_new_lines(paths):
    logf, opsf = paths
    write(logf, day_lines(0, 5), mode="w")
    res = sd.run_once(logf, opsf)
    assert res["status"] == "written" and res["drift_status"] == "baseline_pending"
    assert sd.run_once(logf, opsf)["status"] == "no_new_lines"
    assert len(OpsDB(opsf).recent_digests()) == 1


def test_baseline_then_drift_ok_and_shift_detected(paths):
    logf, opsf = paths
    for day in range(8):  # база: 8 дней по 10 ответов, ce/ad/rl с разбросом
        write(logf, [ln(T0 + timedelta(days=day, minutes=i), "INFO", "g",
                        metrics_msg(ce=0.1 * (i % 3), ad=0.9 + 0.01 * (i % 5), length=200 + 10 * i))
                     for i in range(10)])
        res = sd.run_once(logf, opsf)
    ops = OpsDB(opsf)
    bl = ops.get_baseline()
    assert bl is not None and bl["samples"] == 80
    assert res["drift_status"] == "ok"  # 10 своих + добор прошлых сводок до >=30
    assert res["digest"]["drift"]["window_digests"] == 3
    assert res["drift_score"] < 1.0

    write(logf, [ln(T0 + timedelta(days=9, minutes=i), "INFO", "g",
                    metrics_msg(cjk=0.5, ad=0.4, length=2000)) for i in range(30)])
    res2 = sd.run_once(logf, opsf)
    assert res2["drift_status"] == "ok" and res2["digest"]["drift"]["window_digests"] == 1
    comps = res2["digest"]["drift"]["components"]
    assert comps["cjk"] == sd.Z_CAP and res2["drift_score"] > 3.0
    assert OpsDB(opsf).get_baseline()["samples"] == 80  # база не сдвинулась


def test_baseline_needs_both_days_and_samples(paths):
    logf, opsf = paths
    write(logf, day_lines(0, 60), mode="w")  # много ответов, но 1 день
    assert sd.run_once(logf, opsf)["drift_status"] == "baseline_pending"
    write(logf, day_lines(8, 1))  # 8 дней, 61 ответ -> заморозка
    sd.run_once(logf, opsf)
    assert OpsDB(opsf).get_baseline() is not None


def test_insufficient_window(paths):
    logf, opsf = paths
    write(logf, day_lines(0, 50), mode="w")
    sd.run_once(logf, opsf)
    write(logf, day_lines(8, 5))  # заморозка; окно 5 + прошлое 50 (в пределах 7 дней? нет: 8 дней)
    res = sd.run_once(logf, opsf)
    assert res["drift_status"] == "insufficient"
    assert res["digest"]["drift"]["window_samples"] == 5


def test_no_metrics_window(paths):
    logf, opsf = paths
    write(logf, [ln(T0, "WARNING", "bot", "[TG_TRANSPORT] x")], mode="w")
    res = sd.run_once(logf, opsf)
    assert res["drift_status"] == "no_metrics" and res["drift_score"] is None


def test_status_line(paths):
    logf, opsf = paths
    assert sd.status_line(opsf) == "SLEEP: нет данных" and not opsf.exists()
    write(logf, day_lines(0, 3), mode="w")
    sd.run_once(logf, opsf)
    s = sd.status_line(opsf)
    assert "ответов=3" in s and "baseline_pending" in s and "3/50" in s


def test_cli_missing_log_exit_2(tmp_path):
    assert sd.main(["--log", str(tmp_path / "nope.log"), "--ops", str(tmp_path / "o.db")]) == 2


def test_no_llm_network_or_knowledge_imports():
    for mod in (sd, ops_db):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for bad in ("requests", "httpx", "ollama", "chromadb", "aiohttp",
                    "db", "memory_core", "governor", "bot"):
            assert f"\nimport {bad}\n" not in src and f"\nfrom {bad} import" not in src
        assert "sqlite3.connect(self.db_path" in Path(ops_db.__file__).read_text(encoding="utf-8")
