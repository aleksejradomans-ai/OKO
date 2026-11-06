"""SLEEP_CYCLE (M7, 20.10): проводка сна в bot.py — sleep_cycle_job, _touch_activity, /status.

Стиль test_bot.py: import bot, MagicMock-контекст, asyncio.run; config.* патчится атрибутом.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import bot
import config
import sleep_digest as sd


def _line(msg, level="INFO"):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + f",000 [{level}] governor: {msg}\n"


RAG_OK = {"rows": 12, "missing": 0, "partial": 1, "orphans": 2, "orphan_chunks": 9, "no_hash": 0}


@pytest.fixture
def sleep_env(tmp_path, monkeypatch):
    logf = tmp_path / "okobot_stderr.log"
    logf.write_text(_line("[METRICS] point=B ce=0.00 ad=1.00 rl=0.00 intent=inform(1.00) "
                          "cyr=0.87 lat=0.13 cjk=0.00 len=336"), encoding="utf-8")
    opsf = tmp_path / "data" / "ops.db"
    monkeypatch.setattr(config, "OKO_LOG_FILE", logf)
    monkeypatch.setattr(config, "OPS_DB_PATH", opsf)
    monkeypatch.setattr(config, "SLEEP_IDLE_S", 1200.0)
    monkeypatch.setattr(config, "SLEEP_MIN_INTERVAL_S", 21600.0)
    monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({111}))
    monkeypatch.setitem(bot._sleep_state, "last_activity", time.time() - 3600)
    monkeypatch.setitem(bot._sleep_state, "last_attempt", None)
    monkeypatch.setitem(bot._sleep_state, "disabled_logged", False)
    # M7.4: тесты сна не трогают реальные SQLite/ChromaDB
    monkeypatch.setattr(config, "OKO_RAG_HYGIENE", True, raising=False)
    monkeypatch.setattr(bot, "_rag_hygiene_counts", lambda: dict(RAG_OK))
    return logf, opsf


def _ctx(gate="CLOSED"):
    governor = MagicMock()
    governor.transport_gate_state = gate
    context = MagicMock()
    context.application.bot_data = {"governor": governor}
    return context


def test_idle_and_closed_writes_digest(sleep_env):
    _, opsf = sleep_env
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    assert opsf.exists() and sd.last_sleep_ts(opsf) is not None


def test_second_tick_within_interval_skips(sleep_env):
    logf, opsf = sleep_env
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    with open(logf, "a", encoding="utf-8") as f:
        f.write(_line("[TG_TRANSPORT] x", "WARNING"))
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    assert len(sd.OpsDB(opsf).recent_digests()) == 1


def test_restart_respects_ops_db_interval(sleep_env, monkeypatch):
    logf, opsf = sleep_env
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    monkeypatch.setitem(bot._sleep_state, "last_attempt", None)  # «рестарт»: память пуста
    with open(logf, "a", encoding="utf-8") as f:
        f.write(_line("[TG_TRANSPORT] x", "WARNING"))
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    assert len(sd.OpsDB(opsf).recent_digests()) == 1


@pytest.mark.parametrize("gate", ["OPEN", "HALF_OPEN", "UNKNOWN"])
def test_gate_not_closed_skips(sleep_env, gate):
    _, opsf = sleep_env
    asyncio.run(bot.sleep_cycle_job(_ctx(gate)))
    assert not opsf.exists()


def test_recent_activity_skips(sleep_env, monkeypatch):
    _, opsf = sleep_env
    monkeypatch.setitem(bot._sleep_state, "last_activity", time.time() - 60)
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    assert not opsf.exists()


def test_disabled_without_log_file(sleep_env, monkeypatch, caplog):
    _, opsf = sleep_env
    monkeypatch.setattr(config, "OKO_LOG_FILE", None)
    caplog.set_level("INFO")
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    assert not opsf.exists()
    assert sum("[SLEEP] disabled" in r.getMessage() for r in caplog.records) == 1


def test_missing_log_is_warning_not_error(sleep_env, monkeypatch, caplog):
    logf, opsf = sleep_env
    monkeypatch.setattr(config, "OKO_LOG_FILE", logf.parent / "nope.log")
    caplog.set_level("INFO")
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    fails = [r for r in caplog.records if "[SLEEP_FAIL]" in r.getMessage()]
    assert len(fails) == 1 and fails[0].levelname == "WARNING"
    assert not any(r.levelname in ("ERROR", "CRITICAL") for r in caplog.records)


def test_touch_activity_admin_only(sleep_env):
    old = bot._sleep_state["last_activity"]
    asyncio.run(bot._touch_activity(SimpleNamespace(effective_user=SimpleNamespace(id=999)), None))
    assert bot._sleep_state["last_activity"] == old
    asyncio.run(bot._touch_activity(SimpleNamespace(effective_user=None), None))
    assert bot._sleep_state["last_activity"] == old
    asyncio.run(bot._touch_activity(SimpleNamespace(effective_user=SimpleNamespace(id=111)), None))
    assert bot._sleep_state["last_activity"] > old


def test_sleep_status_text(sleep_env, monkeypatch):
    _, opsf = sleep_env
    txt = bot._sleep_status_text()
    assert "SLEEP: нет данных" in txt and "log: задан" in txt and not opsf.exists()
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    assert "ответов=1" in bot._sleep_status_text()
    monkeypatch.setattr(config, "OKO_LOG_FILE", None)
    assert "сон выключен" in bot._sleep_status_text()


def test_should_sleep_pure():
    f = sd.should_sleep
    assert f(10000, 0, None, "CLOSED", 1200, 21600) == (True, "ok")
    assert f(10000, 9000, None, "CLOSED", 1200, 21600) == (False, "busy")
    assert f(30000, 0, 20000, "CLOSED", 1200, 21600) == (False, "recent")
    assert f(30000, 0, 20000, "GateState.CLOSED", 1200, 5000) == (True, "ok")
    assert f(10000, 0, None, "HALF_OPEN", 1200, 21600) == (False, "gate")


# --- M7.4 [RAG_HYGIENE] (только отчёт, до сводки) ---------------------------------
def _rag_lines(caplog):
    return [r for r in caplog.records if r.getMessage().startswith("[RAG_HYGIENE]")]


def test_rag_hygiene_logged_before_digest_numbers_only(sleep_env, monkeypatch, caplog):
    order = []
    real_run = bot.sleep_digest.run_once
    monkeypatch.setattr(bot.sleep_digest, "run_once",
                        lambda *a, **k: (order.append("digest"), real_run(*a, **k))[1])
    monkeypatch.setattr(bot, "_rag_hygiene_counts", lambda: (order.append("rag"), dict(RAG_OK))[1])
    caplog.set_level("INFO")
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    assert order == ["rag", "digest"]
    (rec,) = _rag_lines(caplog)
    msg = rec.getMessage()
    assert msg == "[RAG_HYGIENE] rows=12 missing=0 partial=1 orphans=2 orphan_chunks=9 no_hash=0"
    assert sd.parse_rag(msg) == {"kind": "scan", **RAG_OK}


def test_rag_hygiene_fail_is_warning_and_digest_still_written(sleep_env, monkeypatch, caplog):
    _, opsf = sleep_env

    def boom():
        raise RuntimeError("chroma secret-path")
    monkeypatch.setattr(bot, "_rag_hygiene_counts", boom)
    caplog.set_level("INFO")
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    (rec,) = _rag_lines(caplog)
    assert rec.levelname == "WARNING" and rec.getMessage() == "[RAG_HYGIENE] fail=RuntimeError"
    assert not any(r.levelname in ("ERROR", "CRITICAL") for r in caplog.records)
    assert sd.last_sleep_ts(opsf) is not None


def test_rag_hygiene_timeout_does_not_block_sleep(sleep_env, monkeypatch, caplog):
    _, opsf = sleep_env
    monkeypatch.setattr(bot, "RAG_HYGIENE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(bot, "_rag_hygiene_counts", lambda: (time.sleep(0.5), dict(RAG_OK))[1])
    caplog.set_level("INFO")
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    assert [r.getMessage() for r in _rag_lines(caplog)] == ["[RAG_HYGIENE] fail=TimeoutError"]
    assert sd.last_sleep_ts(opsf) is not None


def test_rag_hygiene_unavailable_and_switch_off(sleep_env, monkeypatch, caplog):
    monkeypatch.setattr(bot, "_rag_hygiene_counts", lambda: None)
    caplog.set_level("INFO")
    asyncio.run(bot._log_rag_hygiene())
    assert [r.getMessage() for r in _rag_lines(caplog)] == ["[RAG_HYGIENE] skip=unavailable"]
    caplog.clear()
    called = []
    monkeypatch.setattr(bot, "_rag_hygiene_counts", lambda: called.append(1))
    monkeypatch.setattr(config, "OKO_RAG_HYGIENE", False)
    asyncio.run(bot._log_rag_hygiene())
    assert called == [] and _rag_lines(caplog) == []


def test_rag_hygiene_not_run_when_sleep_skipped(sleep_env, monkeypatch):
    called = []
    monkeypatch.setattr(bot, "_rag_hygiene_counts", lambda: called.append(1))
    monkeypatch.setitem(bot._sleep_state, "last_activity", time.time() - 60)  # не простой
    asyncio.run(bot.sleep_cycle_job(_ctx()))
    asyncio.run(bot.sleep_cycle_job(_ctx("OPEN")))
    assert called == []
