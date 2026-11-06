"""Этап 2 рубежа (наблюдение, 25.10): engaged_s в [DEPTH] и граница базы дрейфа
(OKO_DRIFT_BASELINE_SINCE). Этап-1 контракт (active_s/turns/span_s) не меняется."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import sleep_digest as sd
from src.memory_core import MemoryCore


class _NullIndex:
    def upsert(self, *a): pass
    def delete_subject(self, *a): pass
    def search(self, *a): return []


def _core_with_answers(tmp_path, ts_list):
    core = MemoryCore(tmp_path / "m.db", _NullIndex(), lambda t: (t, []))
    for i, ts in enumerate(ts_list):
        core.import_legacy("telegram", "1", [("user", f"q{i}", ts), ("assistant", f"a{i}", ts)])
    return core, core.resolve("telegram", "1")


# --- memory_core.session_stats: engaged_s ---------------------------------------
def test_engaged_s_counts_only_dense_gaps(tmp_path):
    core, sid = _core_with_answers(tmp_path, [1000.0, 1100.0, 1500.0, 1520.0])  # 100, 400, 20
    st = core.session_stats(sid, 2400, engaged_gap_s=300)
    assert st == {"turns": 4, "active_s": 520.0, "span_s": 520.0, "engaged_s": 120.0}


def test_engaged_s_absent_without_param_stage1_contract(tmp_path):
    core, sid = _core_with_answers(tmp_path, [1000.0, 1100.0])
    assert core.session_stats(sid, 2400) == {"turns": 2, "active_s": 100.0, "span_s": 100.0}


@pytest.mark.parametrize("ts_list, turns", [([], 0), ([1000.0], 1)])
def test_engaged_s_zero_cases(tmp_path, ts_list, turns):
    core, sid = _core_with_answers(tmp_path, ts_list)
    assert core.session_stats(sid, 2400, engaged_gap_s=300) == {
        "turns": turns, "active_s": 0.0, "span_s": 0.0, "engaged_s": 0.0}


# --- sleep_digest: [DEPTH] engaged_s ----------------------------------------------
T0 = datetime(2026, 10, 1, 12, 0, 0)


def ln(dt, level, name, msg):
    return f"{dt:%Y-%m-%d %H:%M:%S},000 [{level}] {name}: {msg}"


def test_parse_depth_engaged_optional():
    base = "[DEPTH] turns=3 active_s=821.7 span_s=821.7 pause_s=2400"
    assert sd.parse_depth(base) == {"turns": 3, "active_s": 821.7, "span_s": 821.7}
    assert sd.parse_depth(base + " engaged_s=120.0 gap_s=300")["engaged_s"] == 120.0
    assert "engaged_s" not in sd.parse_depth(base + " engaged_s=x")       # битое поле — без поля
    assert "engaged_s" not in sd.parse_depth(base + " engaged_s=-1")


def test_digest_depth_engaged_summary_only_from_new_lines():
    d = sd.build_digest([
        ln(T0, "INFO", "bot", "[DEPTH] turns=2 active_s=600.0 span_s=600.0 pause_s=2400"),
        ln(T0, "INFO", "bot", "[DEPTH] turns=4 active_s=900.0 span_s=900.0 pause_s=2400 engaged_s=100.0 gap_s=300"),
        ln(T0, "INFO", "bot", "[DEPTH] turns=5 active_s=950.0 span_s=950.0 pause_s=2400 engaged_s=150.0 gap_s=300"),
    ])["depth"]
    assert d["n"] == 3 and d["active_s"]["max"] == 950.0
    assert d["engaged_s"]["mean"] == 125.0 and d["engaged_s"]["max"] == 150.0


# --- sleep_digest.evaluate_drift: baseline_since ----------------------------------
def _stats(n):
    lines = [ln(T0, "INFO", "g", "[METRICS] point=B ce=0.00 ad=1.00 rl=0.00 intent=inform(1.00) "
                "cyr=0.87 lat=0.13 cjk=0.00 len=300")] * n
    return sd.build_digest(lines)["metrics"]["stats"]


class _Ops:
    def __init__(self, digests):
        self._d = digests
        self.frozen = None

    def recent_digests(self, n):
        return self._d

    def get_baseline(self):
        return self.frozen

    def freeze_baseline(self, first, end, samples, merged):
        self.frozen = {"samples": samples, "stats": merged}
        return True


def _prev(ws, we, n):
    return SimpleNamespace(window_start=ws, window_end=we, payload={"metrics": {"stats": _stats(n)}})


def _digest(ws, we, n):
    return {"metrics": {"stats": _stats(n)}, "window_start": ws, "window_end": we}


SINCE = 10_000.0
DAY = 86400.0


def test_baseline_excludes_digests_before_since_and_straddling():
    ops = _Ops([_prev(SINCE - 5000, SINCE - 100, 11),      # до смены identity
                _prev(SINCE - 50, SINCE + 500, 4),          # смешанное окно
                _prev(SINCE + 600, SINCE + 900, 6)])        # после
    score, status, det = sd.evaluate_drift(ops, _digest(SINCE + 1000, SINCE + 2000, 3),
                                           allow_freeze=True, baseline_since=SINCE)
    assert status == "baseline_pending" and det["baseline_samples"] == 9   # 6 + 3
    assert det["baseline_since"] == SINCE and ops.frozen is None


def test_without_since_old_behaviour_counts_all():
    ops = _Ops([_prev(SINCE - 5000, SINCE - 100, 11), _prev(SINCE + 600, SINCE + 900, 6)])
    _, _, det = sd.evaluate_drift(ops, _digest(SINCE + 1000, SINCE + 2000, 3), allow_freeze=True)
    assert det["baseline_samples"] == 20 and "baseline_since" not in det


def test_baseline_freezes_from_post_since_only():
    ops = _Ops([_prev(SINCE - 9 * DAY, SINCE - DAY, 100),                # много старых — не в счёт
                _prev(SINCE + 100, SINCE + 3 * DAY, 30)])
    sd.evaluate_drift(ops, _digest(SINCE + 7 * DAY, SINCE + 8 * DAY, 25), allow_freeze=True,
                      baseline_since=SINCE)
    assert ops.frozen is not None and ops.frozen["samples"] == 55


def test_current_straddling_digest_not_counted():
    ops = _Ops([])
    _, status, det = sd.evaluate_drift(ops, _digest(SINCE - 10, SINCE + 10, 8), allow_freeze=True,
                                       baseline_since=SINCE)
    assert det["baseline_samples"] == 0 and status == "baseline_pending"


# --- bot._log_depth: строка [DEPTH] с engaged_s -------------------------------------
def test_log_depth_line_has_engaged(monkeypatch, caplog):
    import bot
    import config
    core = SimpleNamespace(
        resolve=lambda *a: "sid", epoch_start=lambda sid: None,
        session_stats=lambda sid, pause, **k: {"turns": 3, "active_s": 821.7, "span_s": 821.7,
                                                "engaged_s": 120.0 if k.get("engaged_gap_s") == 300 else -1})
    monkeypatch.setattr(bot, "_get_memory", lambda: core)
    monkeypatch.setattr(config, "OKO_ENGAGED_GAP_S", 300.0, raising=False)
    caplog.set_level(logging.INFO)
    bot._log_depth(1)
    msg = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DEPTH]")][0]
    assert msg == "[DEPTH] turns=3 active_s=821.7 span_s=821.7 pause_s=2400 engaged_s=120.0 gap_s=300"
    assert sd.parse_depth(msg)["engaged_s"] == 120.0
