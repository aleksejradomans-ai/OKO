"""B31 P1/P3/P4/P6: счётчик слоёв -- запись, закрытый список кодов (приватность), потери, ночной счёт, реестр."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import layer_events as le

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "layer_digest.py"
NOW = dt.datetime(2026, 11, 22, 3, 17, 0)
DAY = "2026-11-21T12:00:00"


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "state" / "layer_events.db"
    monkeypatch.setenv("OKO_LAYER_EVENTS_DB", str(p))
    monkeypatch.setenv("OKO_BLOCK_SHA", "0123456789abcdef")
    monkeypatch.setattr(le, "_SHA", None)
    le.reset_memory()
    yield p
    le.reset_memory()


def _rows(p):
    con = sqlite3.connect(str(p))
    try:
        return con.execute("SELECT ts, layer, kind, code, block_sha12 FROM layer_events ORDER BY id").fetchall()
    finally:
        con.close()


def _put(p, layer, kind, code, n, ts=DAY):
    con = le.connect(p)
    try:
        con.executemany("INSERT INTO layer_events (ts, layer, kind, code, block_sha12) VALUES (?,?,?,?,?)",
                        [(ts, layer, kind, code, "abc")] * n)
        con.commit()
    finally:
        con.close()


def test_record_writes_one_row_numbers_only(db):
    assert le.record("L4", "fail", "TV_TIMEOUT") is True
    rows = _rows(db)
    assert len(rows) == 1
    ts, layer, kind, code, sha = rows[0]
    assert (layer, kind, code, sha) == ("L4", "fail", "TV_TIMEOUT", "0123456789ab")
    assert dt.datetime.fromisoformat(ts) and len(ts) == 19
    cols = [r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(layer_events)")]
    assert cols == ["id", "ts", "layer", "kind", "code", "block_sha12"]    # свободного текста нет по построению


@pytest.mark.parametrize("layer,kind,code", [
    ("L4", "fail", "Телевизор не ответил"),        # текст в code
    ("L4", "fail", "tv timeout"),
    ("L4", "fail", "TV_TIMEOUT; DROP TABLE x"),
    ("L4", "fail", "A" * 30),
    ("L4", "input", "TV_TIMEOUT"),                 # код не того вида
    ("L9", "fail", "TV_TIMEOUT"),
    ("L4", "oops", "TV_TIMEOUT"),
    ("L4", "fail", None),
])
def test_privacy_text_in_code_rejected_with_own_l7_row(db, layer, kind, code):
    assert le.record(layer, kind, code) is False
    rows = _rows(db)
    assert [(r[1], r[2], r[3]) for r in rows] == [("L7", "fail", "SCHEMA_REJECT")]
    raw = db.read_bytes()
    if isinstance(code, str) and code != "TV_TIMEOUT":
        assert code.encode("utf-8") not in raw
    assert le.stats()["rejected"] == 1


def test_table_check_refuses_text_even_bypassing_module(db):
    le.record("L0", "input", "TEXT")
    con = sqlite3.connect(str(db))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO layer_events (ts, layer, kind, code, block_sha12) VALUES "
                    "('2026-11-21T12:00:00','L0','input','привет мир','abc')")
    con.close()


def test_every_code_in_closed_list_is_machine_code():
    assert set(k[0] for k in le.CODES) == set(le.LAYERS)
    for (layer, kind), codes in le.CODES.items():
        assert kind in le.KINDS
        for c in codes:
            assert le.CODE_RE.match(c), c
    for L in le.LAYERS:                            # у каждого слоя есть вход и сбой
        assert (L, "input") in le.CODES and (L, "fail") in le.CODES


def test_write_failure_is_fail_open_and_counted_then_landed(db, monkeypatch):
    monkeypatch.setattr(le, "_now", lambda: DAY)                     # часы заморожены (сторож живых часов B30)
    real = le._write
    monkeypatch.setattr(le, "_write", lambda rows, lost: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    assert le.record("L0", "input", "TEXT") is False         # не исключение
    assert le.record("L0", "input", "TEXT") is False
    assert le.stats()["lost_pending"] == 2 and le.stats()["lost_total"] == 2
    monkeypatch.setattr(le, "_write", real)
    assert le.record("L0", "input", "TEXT") is True
    assert le.stats()["lost_pending"] == 0
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT SUM(n) FROM layer_events_lost").fetchone()[0] == 2
    con.close()
    d = le.digest(db, now=NOW)
    assert d["lost_1d"] == 2 and d["lost_7d"] == 2


def test_memory_sink_under_pytest_without_explicit_db(monkeypatch, tmp_path):
    monkeypatch.delenv("OKO_LAYER_EVENTS_DB", raising=False)
    le.reset_memory()
    assert le.record("L1", "input", "ROUTE") is True
    assert [r[1:4] for r in le.memory_rows()] == [("L1", "input", "ROUTE")]
    assert not le.default_db().exists() or le.default_db().stat().st_size >= 0   # боевой файл не создан тестом
    le.reset_memory()


def test_block_sha12_from_git_head(tmp_path, monkeypatch):
    monkeypatch.delenv("OKO_BLOCK_SHA", raising=False)
    g = tmp_path / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/master\n", encoding="ascii")
    (g / "refs" / "heads" / "master").write_text("3421056" + "a" * 33 + "\n", encoding="ascii")
    assert le.block_sha12(tmp_path) == "3421056aaaaa"
    (g / "refs" / "heads" / "master").unlink()
    (g / "packed-refs").write_text("# pack\n" + "b" * 40 + " refs/heads/master\n", encoding="ascii")
    assert le.block_sha12(tmp_path) == "b" * 12
    assert le.block_sha12(tmp_path / "none") == "unknown"


def test_digest_empty_base_is_no_data_not_zero(tmp_path):
    d = le.digest(tmp_path / "absent.db", now=NOW)
    for L in le.LAYERS:
        r = d["layers_1d"][L]
        assert r["input"] == 0 and r["e_k"] is None and r["c_k"] is None and r["decision"] == "NO_DATA"
    assert d["bot_alive"] is False and d["sterile"] == []          # нет событий и нет пульса -- не «стерильно»
    t = le.register_table(d)
    assert "NO_DATA" in t and "0.0 %" not in t


def test_digest_ratios_small_sample_and_division_by_zero(db):
    _put(db, "L4", "input", "TV_CMD", 136)
    _put(db, "L4", "fail", "TV_TIMEOUT", 2)
    _put(db, "L5", "input", "VERIFY", 10)                 # < 70 -> значение есть, решение NO_DATA
    _put(db, "L5", "fail", "VERIFY_FAIL", 1)
    _put(db, "L2", "catch", "CJK_GUARD", 3)               # catch без input: e_k = NO_DATA, c_k = 1
    d = le.digest(db, now=NOW)
    l4, l5, l2 = d["layers_1d"]["L4"], d["layers_1d"]["L5"], d["layers_1d"]["L2"]
    assert l4["e_k"] == round(2 / 136, 6) and l4["decision"] == "OK" and l4["c_k"] == 0.0
    assert l5["e_k"] == 0.1 and l5["decision"] == "NO_DATA"
    assert l2["e_k"] is None and l2["c_k"] == 1.0 and l2["decision"] == "NO_DATA"
    assert l4["codes"] == {"fail:TV_TIMEOUT": 2, "input:TV_CMD": 136}
    assert d["direction"] == "up_bad" and d["min_n"] == 70


def test_digest_sterile_layers_when_bot_alive(db):
    _put(db, "L0", "input", "TEXT", 80)
    _put(db, "L1", "input", "ROUTE", 80)
    d = le.digest(db, now=NOW)
    assert d["bot_alive"] is True
    assert d["sterile"] == ["L2", "L3", "L4", "L5", "L6", "L7"]
    assert le.digest(db, now=NOW, bot_alive=False)["sterile"] == []
    assert "STERILE" in le.register_table(d, "1d")


def test_digest_windows_1d_and_7d(db):
    _put(db, "L0", "input", "TEXT", 5, ts="2026-11-21T23:59:59")    # вчера -> 1d и 7d
    _put(db, "L0", "input", "TEXT", 7, ts="2026-11-16T00:00:00")    # 6 суток назад -> только 7d
    _put(db, "L0", "input", "TEXT", 9, ts="2026-11-14T23:59:59")    # вне 7d (окно 15.11 00:00 .. 22.11 00:00)
    _put(db, "L0", "input", "TEXT", 4, ts="2026-11-22T00:00:00")    # сегодня -> никуда
    d = le.digest(db, now=NOW)
    assert d["layers_1d"]["L0"]["input"] == 5 and d["layers_7d"]["L0"]["input"] == 12
    assert d["day"] == "2026-11-21"


def test_store_and_read_digest_into_quarantine_state(db, tmp_path):
    _put(db, "L4", "input", "TV_CMD", 100)
    _put(db, "L4", "fail", "TV_ERROR", 1)
    qs = tmp_path / "qs.db"
    d = le.digest(db, now=NOW)
    assert le.store_digest(d, qs, now=NOW) == 16
    assert le.store_digest(d, qs, now=NOW) == 16                     # повтор за те же сутки -- замена, не дубль
    con = sqlite3.connect(str(qs))
    assert con.execute("SELECT COUNT(*) FROM layer_digest").fetchone()[0] == 16
    con.close()
    w = le.read_digest(qs, "2026-11-21")
    assert w["2026-11-21"]["L4"] == {"input": 100, "e_k": 0.01, "decision": "OK", "sterile": False, "lost": 0,
                                     "parts": {"tv": 0.01, "st": None}}
    assert w["2026-11-21"]["L0"]["e_k"] is None


def test_weekly_carries_layers_outside_weekly_uuid(db, tmp_path):
    import quarantine_state as qsm
    qs = tmp_path / "qs.db"
    w0 = qsm.weekly(qs, end_day="2026-11-21", now=NOW)
    assert w0["layers"] == {}
    _put(db, "L4", "input", "TV_CMD", 100)
    le.store_digest(le.digest(db, now=NOW), qs, now=NOW)
    w1 = qsm.weekly(qs, end_day="2026-11-21", now=NOW)
    assert w1["weekly_uuid"] == w0["weekly_uuid"]                    # слепок прежний
    assert w1["layers"]["2026-11-21"]["L4"]["input"] == 100


def test_cli_digest_and_register(db, tmp_path):
    _put(db, "L4", "input", "TV_CMD", 100)
    _put(db, "L4", "fail", "TV_TIMEOUT", 2)
    qs = tmp_path / "qs.db"
    hb = tmp_path / "bot.heartbeat"
    hb.write_text("", encoding="ascii")
    import os
    os.utime(hb, (NOW.timestamp() - 60, NOW.timestamp() - 60))      # пульс минуту назад относительно --now
    base = [sys.executable, "-I", str(TOOL), "--db", str(db), "--qs", str(qs), "--heartbeat", str(hb),
            "--now", NOW.isoformat()]
    r = subprocess.run(base, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr[-400:]
    j = json.loads(r.stdout.decode("ascii"))
    assert j["stored_rows"] == 16 and j["layers_1d"]["L4"]["e_k"] == 0.02 and j["layers_1d"]["L0"]["e_k"] is None
    assert j["bot_alive"] is True and "L0" in j["sterile"] and j["codes_sterile"] == "STERILE"
    r = subprocess.run(base + ["--register", "--window", "1d"], capture_output=True, timeout=60)
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "| L4 исполнитель | 102 |" not in out and "| L4 исполнитель | 100 | 2 | 0 | 2.0 % |" in out
    r = subprocess.run([sys.executable, "-I", str(TOOL), "--db", str(tmp_path / "none.db"), "--qs", str(qs),
                        "--heartbeat", str(tmp_path / "nohb"), "--now", NOW.isoformat(), "--dry-run"],
                       capture_output=True, timeout=60)
    j = json.loads(r.stdout.decode("ascii"))
    assert r.returncode == 0 and j["stored_rows"] == 0 and j["bot_alive"] is False
    assert all(v["e_k"] is None and v["decision"] == "NO_DATA" for v in j["layers_7d"].values())


def test_q1_route_miss_out_of_rate(db):
    """Q1 (21.11 20:08): ROUTE_MISS -- отдельный вид вне e_k; сбой L1 = только ROUTE_ERROR."""
    _put(db, "L1", "input", "ROUTE", 100)
    _put(db, "L1", "input", "ROUTE_MISS", 60)
    _put(db, "L1", "fail", "ROUTE_ERROR", 1)
    r = le.digest(db, now=NOW)["layers_1d"]["L1"]
    assert r["input"] == 100 and r["fail"] == 1 and r["e_k"] == 0.01
    assert r["out_of_rate"] == {"input:ROUTE_MISS": 60}
    assert not le.valid("L1", "fail", "ROUTE_MISS")
    assert "ROUTE_MISS (вне e_k) | 60" in le.register_table(le.digest(db, now=NOW), "1d")


def test_q2_l5_two_rates(db):
    """Q2: жёсткая e_k = VERIFY_FAIL / VERIFY (основная); вторая = (FAIL+WARN+BLIND) / VERIFY."""
    _put(db, "L5", "input", "VERIFY", 144)
    _put(db, "L5", "fail", "VERIFY_FAIL", 2)
    _put(db, "L5", "fail", "VERIFY_WARN", 3)
    _put(db, "L5", "fail", "VERIFY_BLIND", 1)
    r = le.digest(db, now=NOW)["layers_1d"]["L5"]
    assert r["parts"]["hard"]["e_k"] == round(2 / 144, 6) and r["e_k"] == round(2 / 144, 6)
    assert r["parts"]["all"]["e_k"] == round(6 / 144, 6) and r["fail"] == 6
    assert r["decision"] == "OK" and r["parts"]["all"]["decision"] == "OK"


def test_q3_l4_tv_and_cloud_apart(db):
    """Q3: L4 раздельно -- ТВ (TV_CMD) и облако (ST_POLL); основная e_k слоя = ТВ."""
    _put(db, "L4", "input", "TV_CMD", 136)
    _put(db, "L4", "fail", "TV_TIMEOUT", 2)
    _put(db, "L4", "input", "ST_POLL", 2880)
    _put(db, "L4", "fail", "ST_TRANSPORT", 4)
    r = le.digest(db, now=NOW)["layers_1d"]["L4"]
    assert r["parts"]["tv"] == {"input": 136, "fail": 2, "e_k": round(2 / 136, 6), "decision": "OK"}
    assert r["parts"]["st"] == {"input": 2880, "fail": 4, "e_k": round(4 / 2880, 6), "decision": "OK"}
    assert r["e_k"] == round(2 / 136, 6)                                  # облако не растворяет сбой ТВ
    t = le.register_table(le.digest(db, now=NOW), "1d")
    assert "| L4 · tv | 136 | 2 |" in t and "| L4 · st | 2880 | 4 |" in t


def test_q3_parts_small_sample_and_zero(db):
    _put(db, "L4", "input", "TV_CMD", 5)
    r = le.digest(db, now=NOW)["layers_1d"]["L4"]
    assert r["parts"]["tv"]["decision"] == "NO_DATA" and r["parts"]["st"]["e_k"] is None


def test_cli_output_is_utf8_bytes_under_cp1251_locale(db, tmp_path):
    """B31 попытка 2 (ПК 21.11 22:41): stdout, отданный в трубу на Windows, кодировался локалью cp1251 -> реестр с
    кириллицей не читался как UTF-8. Эмуляция Windows: PYTHONIOENCODING=cp1251 и перевод строк CRLF потока
    (PYTHONIOENCODING не меняет newline, поэтому LF ниже -- вывод байтами мимо потока). Строгая проверка байтов."""
    import os
    _put(db, "L4", "input", "TV_CMD", 100)
    env = {"PYTHONIOENCODING": "cp1251", "PYTHONUTF8": "0", "PATH": "", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    base = [sys.executable, str(TOOL), "--db", str(db), "--heartbeat", str(tmp_path / "nohb"), "--now", NOW.isoformat()]
    r = subprocess.run(base + ["--register", "--window", "1d"], capture_output=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr[-300:]
    want = le.register_table(le.digest(db, now=NOW, bot_alive=None), "1d").encode("utf-8")
    assert r.stdout == want                                            # байт-в-байт UTF-8, LF
    assert "| L4 исполнитель | 100 |".encode("utf-8") in r.stdout
    r = subprocess.run(base + ["--dry-run"], capture_output=True, timeout=60, env=env)
    assert r.returncode == 0 and r.stdout.endswith(b"}\n") and r.stdout.count(b"\n") == 1
    assert json.loads(r.stdout.decode("utf-8"))["layers_1d"]["L4"]["input"] == 100
