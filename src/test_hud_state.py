"""Тесты логики HUD (hud_state) — без GUI, без сети, без Windows."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import hud_state as hs
import sleep_digest as sd

T0 = datetime(2026, 10, 24, 21, 20, 0)


def ln(dt, level, name, msg):
    return f"{dt:%Y-%m-%d %H:%M:%S},000 [{level}] {name}: {msg}\n"


def ts(dt):
    return time.mktime(dt.timetuple())


GATE_OPEN = "[TRANSPORT_GATE] -> OPEN (step=0, pause=4s): 3 transport failures in a row, last: ConnectError"
GATE_OPEN2 = "[TRANSPORT_GATE] -> OPEN (step=1, pause=11s): probe failed"
GATE_CLOSED = "[TRANSPORT_GATE] -> CLOSED (Ollama responds again)"
METRICS = ("[METRICS] point=B ce=0.00 ad=1.00 rl=0.00 intent=inform(1.00) "
           "cyr=1.00 lat=0.00 cjk=0.00 len=40")


@pytest.fixture
def logf(tmp_path):
    p = tmp_path / "OkoBot.stderr.log"
    p.write_text("", encoding="utf-8")
    return p


def append(p, *lines):
    with open(p, "a", encoding="utf-8") as f:
        f.writelines(lines)


# --- LogWatcher ---------------------------------------------------------------------
def test_gate_transitions_live_format(logf):
    w = hs.LogWatcher(logf)
    w.poll()
    assert (w.gate, w.gate_from_log) == ("CLOSED", False)
    append(logf, ln(T0, "WARNING", "kernel.governor", GATE_OPEN))
    w.poll()
    assert (w.gate, w.gate_pause, w.gate_ts) == ("OPEN", 4, ts(T0))
    append(logf, ln(T0 + timedelta(seconds=70), "WARNING", "kernel.governor", GATE_OPEN2))
    w.poll()
    assert (w.gate, w.gate_pause) == ("OPEN", 11)
    append(logf, ln(T0 + timedelta(minutes=3), "INFO", "kernel.governor", GATE_CLOSED))
    w.poll()
    assert (w.gate, w.gate_pause, w.gate_from_log) == ("CLOSED", None, True)


def test_metrics_and_last_message_without_text(logf):
    append(logf,
           ln(T0, "INFO", "bot", "Processing message from 1234567: len=6"),
           ln(T0 + timedelta(seconds=45), "INFO", "kernel.governor", METRICS))
    w = hs.LogWatcher(logf)
    w.poll()
    assert w.last_msg_ts == ts(T0)
    assert w.metrics["intent"] == "inform" and w.metrics["len"] == 40.0
    assert w.metrics_ts == ts(T0 + timedelta(seconds=45))


def test_start_reads_only_tail(logf):
    old = ln(T0, "WARNING", "kernel.governor", GATE_OPEN)
    filler = ln(T0, "INFO", "apscheduler", "x" * 200) * 400   # ~90 КБ после старой строки
    append(logf, old, filler)
    w = hs.LogWatcher(logf, start_tail=64 * 1024)
    w.poll()
    assert w.gate == "CLOSED" and not w.gate_from_log     # строка OPEN вне хвоста 64 КБ


def test_incremental_and_rotation(logf):
    w = hs.LogWatcher(logf)
    append(logf, ln(T0, "WARNING", "kernel.governor", GATE_OPEN))
    w.poll()
    assert w.gate == "OPEN"
    logf.write_text(ln(T0 + timedelta(hours=1), "INFO", "kernel.governor", GATE_CLOSED), encoding="utf-8")
    w.poll()
    assert w.gate == "CLOSED"


def test_red_lines_last_hour(logf):
    now = ts(T0)
    append(logf,
           ln(T0 - timedelta(hours=2), "ERROR", "bot", "old"),
           ln(T0 - timedelta(minutes=5), "ERROR", "bot", "fresh"),
           ln(T0, "CRITICAL", "bot", "fresh2"),
           ln(T0, "WARNING", "bot", "[TG_TRANSPORT] x"))
    w = hs.LogWatcher(logf)
    w.poll()
    assert w.red_count(now) == 2


def test_missing_log(tmp_path):
    w = hs.LogWatcher(tmp_path / "nope.log")
    assert w.poll() is False
    assert hs.LogWatcher(None).poll() is False


# --- пути ----------------------------------------------------------------------------
def test_resolve_paths_priority(tmp_path):
    (tmp_path / ".env").write_text("TELEGRAM_TOKEN=secret\nOKO_LOG_FILE=D:\\logs\\bot.log  # comment\n"
                                   "AI_BASE_DIR=./data\n", encoding="utf-8")
    log, ops = hs.resolve_paths(tmp_path, environ={})
    assert log == Path("D:\\logs\\bot.log") and ops == tmp_path / "data" / "ops.db"
    log, _ = hs.resolve_paths(tmp_path, environ={"OKO_LOG_FILE": "E:\\x.log"})
    assert log == Path("E:\\x.log")
    log, ops = hs.resolve_paths(tmp_path, log_arg="F:\\a.log", ops_arg="G:\\o.db", environ={})
    assert (log, ops) == (Path("F:\\a.log"), Path("G:\\o.db"))
    log, ops = hs.resolve_paths(tmp_path / "empty", environ={})
    assert log is None and ops == tmp_path / "empty" / "data" / "ops.db"


# --- внешние пробы --------------------------------------------------------------------
def test_probe_ollama_with_fake_fetch():
    def fetch(url, timeout):
        if url.endswith("/api/version"):
            return {"version": "0.31.2"}
        return {"models": [{"name": "gemma2:latest", "size_vram": 5.6 * 1024 ** 3}]}
    r = hs.probe_ollama(fetch=fetch)
    assert r["version"] == "0.31.2" and r["models"] == [("gemma2:latest", 5.6)] and r["rtt_ms"] >= 0


def test_probe_ollama_ps_failure_keeps_version():
    def fetch(url, timeout):
        if url.endswith("/api/ps"):
            raise OSError("boom")
        return {"version": "0.31.2"}
    assert hs.probe_ollama(fetch=fetch)["models"] == []


@pytest.mark.parametrize("text, want", [
    ("SERVICE_NAME: OkoBot\n        TYPE               : 10  WIN32_OWN_PROCESS\n"
     "        STATE              : 4  RUNNING\n", "RUNNING"),
    ("Имя_службы: OkoBot\n        Тип                : 10  WIN32_OWN_PROCESS\n"
     "        Состояние          : 1  STOPPED\n", "STOPPED"),
    ("        STATE              : 2  START_PENDING\n", "START_PENDING"),
    ("[SC] OpenService FAILED 1060", None),
])
def test_parse_sc_query(text, want):
    assert hs.parse_sc_query(text) == want
    assert hs.query_service(run=lambda cmd: text) == want


# --- ops.db read-only -----------------------------------------------------------------
def test_read_sleep_missing_does_not_create(tmp_path):
    p = tmp_path / "ops.db"
    assert hs.read_sleep(p) is None and not p.exists()


def test_read_sleep_reads_last_digest(tmp_path):
    logf = tmp_path / "bot.log"
    logf.write_text(ln(T0, "INFO", "g", METRICS) * 3, encoding="utf-8")
    opsf = tmp_path / "data" / "ops.db"
    sd.run_once(logf, opsf)
    r = hs.read_sleep(opsf)
    assert r["answers"] == 3 and r["drift_status"] == "baseline_pending" and r["baseline_samples"] == 3
    c = sqlite3.connect(opsf)                      # базу HUD не меняет
    assert c.execute("SELECT COUNT(*) FROM sleep_digest").fetchone()[0] == 1
    c.close()


def test_read_sleep_wrong_schema(tmp_path):
    p = tmp_path / "ops.db"
    sqlite3.connect(p).close()
    assert hs.read_sleep(p) is None


# --- сборщик ----------------------------------------------------------------------------
def test_collector_throttles_and_isolates_failures(logf, tmp_path):
    calls = {"ollama": 0, "svc": 0, "ops": 0}
    clock = [1000.0]

    def ollama():
        calls["ollama"] += 1
        raise OSError("down")

    def svc():
        calls["svc"] += 1
        return "RUNNING"

    def ops(_p):
        calls["ops"] += 1
        raise sqlite3.OperationalError("locked")

    c = hs.HudCollector(logf, tmp_path / "ops.db", probe_every_s=5, ops_every_s=30,
                        ollama=ollama, service=svc, sleep_reader=ops, clock=lambda: clock[0])
    s1 = c.tick()
    for _ in range(3):
        clock[0] += 1
        c.tick()
    assert calls == {"ollama": 1, "svc": 1, "ops": 1}
    assert s1.ollama_up is False and s1.service == "RUNNING" and s1.log_ok
    assert s1.errors == ["ops.db: OperationalError"]          # недоступная Ollama — не ошибка HUD
    clock[0] += 5
    c.tick()
    assert calls["ollama"] == 2


# --- отображение -----------------------------------------------------------------------
def snap(**kw):
    base = dict(now=ts(T0), ollama_up=True, ollama_version="0.31.2", ollama_rtt_ms=12,
                service="RUNNING", log_ok=True)
    base.update(kw)
    return hs.HudSnapshot(**base)


def test_render_tones_only_transport_colored():
    rows = hs.render(snap(metrics={"ce": 0, "ad": 1, "rl": 0, "intent": "inform", "len": 40.0},
                          metrics_ts=ts(T0),
                          sleep={"created_at": ts(T0), "answers": 3, "drift_score": None,
                                 "drift_status": "baseline_pending", "baseline_samples": 3}))
    tones = [t for _, t in rows]
    assert tones[:3] == ["ok", "ok", "ok"]
    assert tones[3] == "plain" and tones[4] == "plain"         # M6 и дрейф — без цвета
    text = " ".join(t for t, _ in rows)
    for banned in ("warning", "empathy", "resistance", "WARNING"):
        assert banned not in text
    assert "база 3/50" in text and "модель выгружена" in text


def test_render_open_gate_and_stopped_service():
    rows = hs.render(snap(gate="OPEN", gate_from_log=True, gate_ts=ts(T0), gate_pause_s=11,
                          ollama_up=False, service="STOPPED"))
    assert rows[0][1] == "alert" and "пауза 11 с" in rows[0][0]
    assert rows[1] == ("Ollama: не отвечает", "alert")
    assert rows[2][0].startswith("OkoBot: остановлен (игра?)") and rows[2][1] == "muted"


def test_render_footer_and_models():
    rows = hs.render(snap(models=[("gemma2:latest", 5.6)], red_last_hour=2, log_ok=False,
                          errors=["ops.db: OperationalError"]))
    assert "в VRAM: gemma2:latest 5.6 ГБ" in rows[1][0]
    assert rows[-1][0] == "красных строк за час: 2 · лог не найден (OKO_LOG_FILE) · ops.db: OperationalError"


def test_hud_state_has_no_writes_or_llm():
    src = Path(hs.__file__).read_text(encoding="utf-8")
    for bad in ("import requests", "import httpx", "chromadb", "governor", "OpsDB(", "commit_digest",
                "save_cursor", "INSERT", "UPDATE "):
        assert bad not in src


def test_overlay_module_imports():
    pytest.importorskip("PyQt6.QtWidgets")
    import hud_overlay
    assert hud_overlay.VK_F11 == 0x7A and set(hud_overlay.TONES) >= {"ok", "alert", "warn", "muted", "plain"}
