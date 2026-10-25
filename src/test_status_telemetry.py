"""HUD v2 в /status: блок TELEMETRY из хвоста лога (тот же разбор, что у HUD)."""
from __future__ import annotations

import time

import bot
import config


def _ln(msg, level="INFO"):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + f",000 [{level}] g: {msg}\n"


def test_telemetry_text_from_log(tmp_path, monkeypatch):
    logf = tmp_path / "OkoBot.stderr.log"
    logf.write_text(_ln("Processing message from 111: len=5 secret-text")
                    + _ln("[CTX] call=main pe=1190 ev=16 nctx=4096 fill=0.291 chars=3279")
                    + _ln("P1 loop guard: repeated answer not persisted (user=111)", "WARNING")
                    + _ln("[DEPTH] turns=3 active_s=821.7 span_s=821.7 pause_s=2400"), encoding="utf-8")
    monkeypatch.setattr(config, "OKO_LOG_FILE", logf)
    txt = bot._telemetry_status_text()
    lines = txt.split("\n")
    assert len(lines) == 3
    assert "fill 0.29" in lines[0] and "1190/4096" in lines[0]
    assert "записано ответов 3" in lines[1] and "P1 за час 1" in lines[1]
    assert lines[2] == "S: в хвосте лога нет [VECTOR_S]"
    assert "secret" not in txt and "111" not in txt


def test_telemetry_text_no_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OKO_LOG_FILE", None)
    assert "не задан" in bot._telemetry_status_text()
    monkeypatch.setattr(config, "OKO_LOG_FILE", tmp_path / "nope.log")
    assert bot._telemetry_status_text() == "лог не найден"
