"""(16.11, фаза 2, этап 2e): tools/check_sigma_2e.py -- офлайн decide ядра Σ на отложенной выборке.

Проверяется: выборка seq > last_seq и порог А; срез volume (полный цикл, только абсолютные, потолок 15); сквозной decide
= HALT только из-за NO_DATA намерения; ПРОБА веток состояния и диссонанса; синтетика гистерезиса (4 сбоя -> OBSERVE,
5 успехов -> сброс, Δt ≫ τ_D -> затухание, одиночный сбой -> без тревоги); β-модель файла; кейсы полигона must_not /
violated / F01-27 до 1c -> 0 EXECUTE; в stdout только счётчики; детерминизм; ошибки входа -> код 2; файлов не пишет."""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parent / "check_sigma_2e.py"
_spec = importlib.util.spec_from_file_location("check_sigma_2e_b07g", _TOOL)
cs2e = importlib.util.module_from_spec(_spec)
sys.modules["check_sigma_2e_b07g"] = cs2e
_spec.loader.exec_module(cs2e)
al = cs2e.al
cal = cs2e.cal

TS0 = 1794700000000
LAT = 4242427
MANUAL = {"ref": "решение 2026-11-16 12:04 — ручные значения 2d (тест)", "tau_D": 60, "Theta_crit": 0.5,
          "Theta_reset": 0.2, "R_t": {"N_obs": 3, "N_route": 3, "N_reflect": 2},
          "eps_state": {"setting_710": 1}, "tau_b": 600, "delta_rule": "any_fail_1"}
P1204 = al.AbstractionParams(tau_b=600.0, tau_D=60.0, eps_state=0.1, theta_state=1.0, theta_crit=0.5, theta_reset=0.2)
BETA6 = (tuple(sorted(cal.ERR_TYPES)), tuple(1.0 / 6 for _ in cal.ERR_TYPES))
SECRET_IN = "ВВОД_ПОЛИГОНА_НЕ_ВЫВОДИТЬ"
SECRET_ANS = "ОТВЕТ_БОТА_НЕ_ВЫВОДИТЬ"


def _step(t, before, target, obs=None, outcome="PASS", feature="setting_710", action="amb_step", err=None):
    """readback(до) -> send -> ack -> readback(после) -> outcome (как setting_N после ). seq = t."""
    obs = target if obs is None else obs
    rows = [{"ts": t, "step": "readback", "seq": t, "cmd_kind": "verified", "action": action, "feature": feature,
             "s_obs": before, "latency_ms": LAT},
            {"ts": t + 1, "step": "send", "seq": t, "cmd_kind": "verified", "action": action, "feature": feature,
             "s_exp": target},
            {"ts": t + 2, "step": "ack", "seq": t, "cmd_kind": "verified", "action": action, "feature": feature,
             "s_exp": target, "latency_ms": LAT},
            {"ts": t + 3, "step": "readback", "seq": t, "cmd_kind": "verified", "action": action, "feature": feature,
             "s_obs": obs, "latency_ms": LAT}]
    o = {"ts": t + 4, "step": "outcome", "seq": t, "cmd_kind": "verified", "action": action, "feature": feature,
         "outcome": outcome, "s_exp": target, "s_obs": obs, "latency_ms": LAT}
    if err:
        o["err"] = err
    return rows + [o]


def _lounge(t):
    rows = []
    for k in range(3):
        rows += [{"ts": t + 2 * k, "step": "send", "seq": t, "cmd_kind": "verified", "action": "amb_colour",
                  "feature": "lounge_brightness", "s_exp": 255},
                 {"ts": t + 2 * k + 1, "step": "ack", "seq": t, "cmd_kind": "verified", "action": "amb_colour",
                  "feature": "lounge_brightness", "latency_ms": LAT}]
    rows += [{"ts": t + 7, "step": "readback", "seq": t, "cmd_kind": "verified", "action": "amb_colour",
              "feature": "lounge_brightness", "s_obs": 255, "latency_ms": LAT},
             {"ts": t + 8, "step": "outcome", "seq": t, "cmd_kind": "verified", "action": "amb_colour",
              "feature": "lounge_brightness", "outcome": "PASS", "s_exp": 255, "s_obs": 255, "latency_ms": LAT}]
    return rows


def _volume(t, target=10, action="volume_set", steps=("send", "ack", "readback", "outcome"), outcome="PASS"):
    base = {"seq": t, "cmd_kind": "verified", "action": action, "feature": "volume"}
    out = []
    for i, s in enumerate(steps):
        r = dict(base, ts=t + i, step=s)
        if s in ("send", "ack", "outcome"):
            r["s_exp"] = target
        if s in ("readback", "outcome"):
            r["s_obs"] = target
        if s == "outcome":
            r["outcome"] = outcome
        if s != "send":
            r["latency_ms"] = LAT
        out.append(r)
    return out


def _journal_2d():
    """2d: setting_710 цикл 0..10 по шагу +1 (все переходы v -> v+1), lounge_brightness одно значение, volume."""
    rows, v, t = [], 0, TS0
    for i in range(40):
        nv = (v + 1) % 11
        rows += _step(t, v, nv)
        v, t = nv, t + 30_000
    rows += _lounge(t)
    t += 30_000
    rows += _volume(t)
    return rows, v, t + 30_000


def _new_good(v, t, n=12, lounge=2, vol=2):
    rows = []
    for _ in range(n):
        nv = (v + 1) % 11
        rows += _step(t, v, nv)
        v, t = nv, t + 13_000
    for _ in range(lounge):
        rows += _lounge(t)
        t += 13_000
    for k in range(vol):
        rows += _volume(t, target=5 + k)
        t += 13_000
    return rows, v, t


def _write(p: Path, rows) -> Path:
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return p


def _poly_row(i, run=1, verdict="PASS", violated=None, would_send=None):
    return {"id": i, "run": run, "input": SECRET_IN, "expect": "x", "must_not": "запрещено: " + SECRET_IN,
            "route_kind": "clarify", "route_reason": "r", "would_send": would_send or [], "answer": SECRET_ANS,
            "ms": 10, "error": None, "verdict": verdict, "violated": violated or [], "human": None}


def _poly(d: Path, latest=("batch99_b01_g", "batch100_b02_g", "batch101_b30_g")) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for b in latest:
        _write(d / f"{b}_fuzz_report.jsonl", [_poly_row(f"G-{k}") for k in range(5)])
    _write(d / "batch75_b01_iso_fuzz_report.jsonl",
           [_poly_row("F01-27", r, "FAIL", ["mn1"]) for r in (1, 2, 3)] + [_poly_row("F01-01")])
    _write(d / "batch71_b01_after_b04_fuzz_report.jsonl",
           [_poly_row("F01-27", 1)] + [_poly_row("F01-27", r, "FAIL", ["mn1"], ["tv:x"]) for r in (2, 3)])
    _write(d / "batch80_b01_c_fuzz_report.jsonl", [_poly_row("F01-27", r) for r in (1, 2, 3)])   # после 1c -- не в наборе
    _write(d / "b00_live_replay_fuzz_report.jsonl", [_poly_row("F01-27", 1, "FAIL", ["mn"])])     # не batchNN -- пропуск
    return d


def _setup(tmp_path, new_rows_fn=None, manual=MANUAL):
    rows2d, v, t = _journal_2d()
    j = _write(tmp_path / "j.jsonl", rows2d)
    res = cal.calibrate(j, manual)
    par = tmp_path / "params.json"
    par.write_bytes(cal.dumps(res["doc"]))
    new = (new_rows_fn or _new_good)(v, t)[0]
    _write(j, rows2d + new)
    poly = _poly(tmp_path / "poly")
    return j, par, poly, res["doc"]["holdout_2e"]["last_seq"]


def _run(j, par, poly, latest=None):
    return cs2e.run(j, par, poly, tuple(latest or cs2e.DEFAULT_POLY_LATEST))


# ---------------------------------------------------------------- синтетика
def test_b07g_synthetic_four_failures_observe_five_successes_reset():
    sy = cs2e.synthetic(P1204, *BETA6)
    f4 = sy["fail4"]
    assert f4["alarm_at_fail"] == 4 and not f4["alarm_before_4"]
    assert f4["D_at_4"] == pytest.approx(1 - math.exp(-13 / 60) ** 4)
    assert round(f4["D_at_4"], 2) == 0.58
    assert f4["decide_at_4"] == "OBSERVE" and f4["decide_before_4"] == ["EXECUTE"]
    assert f4["reset_at_success"] == 5 and f4["D_at_reset"] < 0.2 <= f4["D_success_4"]
    assert round(f4["D_at_reset"], 3) == 0.196
    assert f4["decide_after_reset"] == "EXECUTE"
    assert sy["ok"] is True


def test_b07g_synthetic_single_failure_no_alarm_and_long_gap_decay():
    sy = cs2e.synthetic(P1204, *BETA6)
    assert sy["single"]["alarm"] is False and round(sy["single"]["D"], 2) in (0.19, 0.2)
    assert sy["long_gap"]["reset"] is True and sy["long_gap"]["D_after_gap"] < 1e-20
    # находка: первый сбой после долгой паузы -> λ ≈ 0 -> D = δ = 1 -> тревога сразу
    assert sy["long_gap"]["D_fail_after_gap"] == pytest.approx(1.0) and sy["long_gap"]["alarm_fail_after_gap"] is True


def test_b07g_beta_model_one_type_failure_never_alarms():
    sy = cs2e.synthetic(P1204, *BETA6)
    b = sy["beta"]
    assert b["delta_one_type"] == pytest.approx(1 / 6) and b["reachable_one_type"] is False
    assert b["alarms_50_fails"] == 0 and b["D_after_50"] < 0.5
    assert b["types_needed"] == 3 and b["types_total"] == 6


def test_b07g_synthetic_uses_kernel_monitor_semantics():
    # Θ_reset < Θ_crit -- из ядра; иное -> ValueError ядра (ядро не обходится)
    with pytest.raises(ValueError):
        al.AbstractionParams(tau_D=60.0, theta_crit=0.2, theta_reset=0.5)


# ---------------------------------------------------------------- живые данные
def test_b07g_sample_only_after_last_seq_and_threshold(tmp_path):
    j, par, poly, last = _setup(tmp_path, lambda v, t: _new_good(v, t, n=12, lounge=2, vol=2))
    smp = cs2e.sample(j, last)
    assert all(c["seq"] > last for c in smp["sigma"])
    assert len(smp["sigma"]) == 14 and len(smp["volume"]) == 2
    rc, lines = _run(j, par, poly)
    txt = "\n".join(lines)
    assert "in Sigma=14 by feature: lounge_brightness=2 setting_710=12" in txt
    assert "threshold A (>= 30 / setting_710 >= 10 / lounge_brightness >= 1): False" in txt
    assert "sample threshold A: False" in txt and rc == cs2e.EXIT_RED


def test_b07g_threshold_a_met_green(tmp_path):
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4))
    rc, lines = _run(j, par, poly)
    txt = "\n".join(lines)
    assert "threshold A (>= 30 / setting_710 >= 10 / lounge_brightness >= 1): True" in txt
    assert lines[-1] == "status: GREEN" and rc == cs2e.EXIT_GREEN


def test_b07g_volume_slice_ok_and_isolated(tmp_path):
    j, par, poly, last = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4))
    v = cs2e.volume_check(cs2e.sample(j, last)["volume"])
    assert v["commands"] == 4 and v["full_cycle"] == 4 and v["absolute"] == 4 and v["ceiling_ok"] == 4 and v["ok"]
    assert all(c["feature"] != "volume" for c in cs2e.sample(j, last)["sigma"])


@pytest.mark.parametrize("bad,field", [
    (dict(target=16), "ceiling_ok"),
    (dict(action="volume_up"), "absolute"),
    (dict(steps=("send", "ack", "outcome")), "full_cycle"),
])
def test_b07g_volume_slice_violations(tmp_path, bad, field):
    def rows(v, t):
        r, v, t = _new_good(v, t, n=28, lounge=3, vol=2)
        return r + _volume(t, **bad), v, t + 13_000
    j, par, poly, last = _setup(tmp_path, rows)
    vc = cs2e.volume_check(cs2e.sample(j, last)["volume"])
    assert vc["commands"] == 3 and vc[field] == 2 and not vc["ok"]
    rc, lines = _run(j, par, poly)
    assert "  volume slice: False" in lines and rc == cs2e.EXIT_RED


def test_b07g_end_to_end_halt_only_by_intent_no_data(tmp_path):
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4))
    doc = cs2e.load_params(par)
    p = cs2e.kernel_params(doc)
    assert {"theta_intent", "theta_margin", "theta_goal"} <= set(p.missing())
    lv = cs2e.live(doc, p, cs2e.sample(j, doc["holdout_2e"]["last_seq"]))
    assert lv["full"]["by"]["HALT"] == lv["n"] == lv["full"]["halt_intent_only"] == 31
    txt = "\n".join(_run(j, par, poly)[1])
    assert "end-to-end decide is ALWAYS non-EXECUTE (HALT NO_DATA intent): True" in txt


def test_b07g_state_branch_probe_execute_on_seen_good_commands(tmp_path):
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=0))
    doc = cs2e.load_params(par)
    p = cs2e.kernel_params(doc)
    lv = cs2e.live(doc, p, cs2e.sample(j, doc["holdout_2e"]["last_seq"]))
    assert lv["state_status"] == {"NO_DATA_FEATURE": 3, "OK": 28}
    assert lv["probe"]["by"]["EXECUTE"] == 28 and lv["probe"]["n"] == 28     # lounge (κ NO_DATA) не оценивается


def test_b07g_state_branch_observe_on_deviation_and_unseen(tmp_path):
    """сглаживание -- переход, не встреченный в 2d, больше не даёт PRIOR_ZERO (C_state = 0): статус OK и высокий
    C_state; отклонение > ε -- по-прежнему OBSERVE с малым C_state. Ложный OBSERVE остаётся только от положения порога
    (min C_state на однородной выборке 2d) и попадает в отчёт ложных OBSERVE с корзиной C_state."""
    def rows(v, t):
        r, v, t = _new_good(v, t, n=28, lounge=1, vol=0)
        r += _step(t, v, (v + 1) % 11, obs=(v + 5) % 11, outcome="FAIL", err="verify_fail")   # расхождение > ε
        r += _step(t + 13_000, v, (v + 2) % 11)                                                # переход не из 2d
        return r, v, t + 26_000
    j, par, poly, _ = _setup(tmp_path, rows)
    doc = cs2e.load_params(par)
    p = cs2e.kernel_params(doc)
    smp = cs2e.sample(j, doc["holdout_2e"]["last_seq"])
    lv = cs2e.live(doc, p, smp)
    assert lv["state_status"].get("PRIOR_ZERO", 0) == 0 and lv["state_status"].get("OBS_UNSEEN", 0) == 0
    fail_c, new_c = smp["sigma"][-2], smp["sigma"][-1]
    cs_fail, st_fail = cs2e.c_state(doc, p, fail_c)
    cs_new, st_new = cs2e.c_state(doc, p, new_c)
    assert st_fail == st_new == "OK"
    assert cs_fail < 0.1 < 0.9 < cs_new                       # различение: отклонение -- низко, хороший -- высоко
    assert lv["probe"]["by"]["OBSERVE"] == 2 and lv["probe"]["reasons"].get("STATE") == 2
    fo = lv["false_obs"]
    assert fo["n_good"] == 29 and fo["observe"] == 1 and fo["buckets"] == {"[0.9,1.0)": 1}
    assert lv["errors"] == 1
    assert lv["deviation"]["n"] > 0 and lv["deviation"]["detected"] == lv["deviation"]["n"]


def test_b07g_dissonance_live_four_failures_delta_one(tmp_path):
    def rows(v, t):
        r, v, t = _new_good(v, t, n=28, lounge=1, vol=0)
        for k in range(4):
            r += _step(t + k * 13_000, v, (v + 1) % 11, obs=v, outcome="FAIL", err="verify_fail")
        return r, v, t + 60_000
    j, par, poly, _ = _setup(tmp_path, rows)
    doc = cs2e.load_params(par)
    p = cs2e.kernel_params(doc)
    lv = cs2e.live(doc, p, cs2e.sample(j, doc["holdout_2e"]["last_seq"]))
    assert lv["errors"] == 4
    assert lv["dis1"]["alarms"] == 1 and lv["dis1"]["first_alarm"] == 33   # 29 штатных + 4-й сбой
    assert lv["dis"]["alarms"] == 0                                         # β-модель: δ = 1/6 -- тревоги нет
    assert lv["probe"]["by"]["EXECUTE"] == 28


# ---------------------------------------------------------------- полигон
def test_b07g_polygon_sets_and_zero_execute(tmp_path):
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4))
    sets, info = cs2e.poly_cases(poly, cs2e.DEFAULT_POLY_LATEST)
    assert len(sets["must_not"]) == 15 and len(sets["violated"]) == 5 and len(sets["f01_27_pre1c"]) == 6
    assert info["pre1c_batches"] == 2 and not info["latest_missing"]
    doc = cs2e.load_params(par)
    pc = cs2e.poly_check(doc, cs2e.kernel_params(doc), sets)
    for k, d in pc.items():
        assert d["probe"]["by"]["EXECUTE"] == 0 and d["probe"]["by"]["OBSERVE"] == d["n"], k
        assert d["full"]["halt_intent_only"] == d["n"], k
        assert d["alarms"] == 0, k                       # одиночный сбой -- без тревоги
    assert pc["violated"]["cf_delta1"]["by"]["EXECUTE"] == 5   # контрфактика: диссонанс один не держит


def test_b07g_polygon_missing_latest_is_red(tmp_path):
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4))
    rc, lines = _run(j, par, poly, latest=("batch99_b01_g", "batch999_none"))
    assert "  polygon sets found: False" in lines and rc == cs2e.EXIT_RED


# ---------------------------------------------------------------- вывод, детерминизм, ошибки
def test_b07g_stdout_counters_only(tmp_path, capsys):
    j, par, poly, last = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4))
    rc = cs2e.main(["--path", str(j), "--params", str(par), "--poly-dir", str(poly)])
    out = capsys.readouterr().out
    assert rc == 0 and out.strip().endswith("status: GREEN")
    for bad in (str(last), str(TS0), str(LAT), SECRET_IN, SECRET_ANS, "запрещено", "s_exp=", "s_obs=", '"ts"', "255"):
        assert bad not in out, bad


def test_b07g_deterministic_and_writes_nothing(tmp_path):
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4))
    before = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))
    a = _run(j, par, poly)
    b = _run(j, par, poly)
    assert a == b
    after = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))
    assert before == after


def test_b07g_input_errors_exit_2(tmp_path):
    j, par, poly, _ = _setup(tmp_path)
    rc, lines = _run(j, tmp_path / "absent.json", poly)
    assert rc == cs2e.EXIT_INPUT and lines[-1].startswith("status: INPUT_ERROR")
    doc = json.loads(par.read_text(encoding="utf-8"))
    del doc["holdout_2e"]["last_seq"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    assert _run(j, bad, poly)[0] == cs2e.EXIT_INPUT
    doc = json.loads(par.read_text(encoding="utf-8"))
    doc["params"]["tau_D"] = {"status": "NO_DATA", "value": None, "source": {"no_data": "x"}}
    bad.write_text(json.dumps(doc), encoding="utf-8")
    assert _run(j, bad, poly)[0] == cs2e.EXIT_INPUT


def test_b07g_params_without_source_is_red(tmp_path):
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4))
    doc = json.loads(par.read_text(encoding="utf-8"))
    doc["params"]["pi_0"]["source"] = {}
    bad = tmp_path / "nosrc.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    rc, lines = _run(j, bad, poly)
    assert "  params with source: False" in lines and rc == cs2e.EXIT_RED


def test_b07g_kernel_file_untouched_and_imported():
    src = Path(cs2e.ROOT) / "src" / "abstraction_layer.py"
    assert Path(al.__file__).resolve() == src.resolve()
    assert hasattr(al, "decide") and hasattr(al, "DissonanceMonitor")


# ---------------------------------------------------------------- (решение 16.11 12:59)
LEGACY = {k: v for k, v in MANUAL.items() if k != "delta_rule"}


def test_b07h_file_delta_rule_alarm_reachable_gate(tmp_path):
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4))
    doc = cs2e.load_params(par)
    assert cs2e.delta_rule(doc) == "any_fail_1" and doc["delta_rule"]["beta_k_used"] is False
    sy = cs2e.synthetic(cs2e.kernel_params(doc), *cs2e.betas(doc), "any_fail_1")
    assert sy["rule"] == {"rule": "any_fail_1", "alarm_at_fail": 4, "D_at_4": pytest.approx(1 - math.exp(-13 / 60) ** 4),
                          "reset_at_success": 5, "ok": True}
    rc, lines = _run(j, par, poly)
    assert "  alarm reachable under file delta_rule: True" in lines and rc == cs2e.EXIT_GREEN
    # без delta_rule (прежняя β-модель): тревога недостижима -> gate RED
    (tmp_path / "legacy").mkdir()
    j2, par2, poly2, _ = _setup(tmp_path / "legacy", lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4), manual=LEGACY)
    assert cs2e.delta_rule(cs2e.load_params(par2)) == "beta_k"
    rc2, lines2 = _run(j2, par2, poly2)
    assert "  alarm reachable under file delta_rule: False" in lines2 and rc2 == cs2e.EXIT_RED


def test_b07h_live_four_failures_alarm_by_file_rule(tmp_path):
    def rows(v, t):
        r, v, t = _new_good(v, t, n=28, lounge=1, vol=0)
        for k in range(4):
            r += _step(t + k * 13_000, v, (v + 1) % 11, obs=v, outcome="FAIL", err="verify_fail")
        return r, v, t + 60_000
    j, par, poly, _ = _setup(tmp_path, rows)
    doc = cs2e.load_params(par)
    p = cs2e.kernel_params(doc)
    lv = cs2e.live(doc, p, cs2e.sample(j, doc["holdout_2e"]["last_seq"]))
    assert lv["rule"] == "any_fail_1"
    assert lv["dis_rule"]["alarms"] == 1 and lv["dis_rule"]["first_alarm"] == 33
    assert lv["dis_rule"]["trace"] == lv["dis1"]["trace"]                  # any_fail_1 = модель δ = 1
    assert lv["dis"]["alarms"] == 0                                         # β-запись: тревоги нет
    assert lv["probe"]["reasons"].get("DISSONANCE", 0) >= 1


def test_b07h_params_without_states_rejected(tmp_path):
    j, par, poly, _ = _setup(tmp_path)
    doc = json.loads(par.read_text(encoding="utf-8"))
    del doc["params"]["T_theta"]["value"]["states"]
    bad = tmp_path / "v2.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    rc, lines = _run(j, bad, poly)
    assert rc == cs2e.EXIT_INPUT and "2d-v3" in lines[-1]


def test_b07h_lounge_with_manual_kappa_is_evaluated(tmp_path):
    m = dict(MANUAL, kappa={"lounge_brightness": 255})
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=0), manual=m)
    doc = cs2e.load_params(par)
    p = cs2e.kernel_params(doc)
    lv = cs2e.live(doc, p, cs2e.sample(j, doc["holdout_2e"]["last_seq"]))
    assert lv["state_status"].get("NO_DATA_FEATURE", 0) == 0 and lv["probe"]["n"] == 31
    assert lv["probe_by_feat"]["lounge_brightness"]["n"] == 3
    assert lv["deviation"]["by_feature"].get("lounge_brightness", 0) >= 3
    # в 2d теста одна команда lounge (π_0 почти без данных): первая команда 2e без «до» опирается на слабый π_0;
    # команды с «до» (поток 2e) отклонение ловят
    assert lv["deviation"]["detected_by_feature"]["lounge_brightness"] >= 2


def test_b07h_report_lines_counters_only(tmp_path, capsys):
    j, par, poly, last = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4),
                                manual=dict(MANUAL, kappa={"lounge_brightness": 255}))
    rc = cs2e.main(["--path", str(j), "--params", str(par), "--poly-dir", str(poly)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "false OBSERVE on good commands (PASS, no err):" in out and "synthetic deviation on good commands" in out
    assert "KNOWN DEFECT (phase 3" in out and "file delta_rule=any_fail_1" in out
    for bad in (str(last), str(TS0), str(LAT), SECRET_IN, SECRET_ANS, "s_exp=", "s_obs=", '"ts"', "255"):
        assert bad not in out, bad


def test_b07h_check_uses_calibrate_model_and_one_kernel_copy(tmp_path):
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=0))
    doc = cs2e.load_params(par)
    p = cs2e.kernel_params(doc)
    assert cs2e.al is cal.al
    c = cs2e.sample(j, doc["holdout_2e"]["last_seq"])["sigma"][0]
    mine = cs2e.c_state(doc, p, c)
    theirs = cal.c_state(cs2e.model_view(doc), p.eps_state, doc["params"]["alpha_j"]["value"],
                         doc["params"]["kappa_j"]["value"], c, p.tau_b)
    assert mine == theirs


# Сканеры вывода блоков (install_b07g/b07h.ps1, шаги check / calibrate): тот же regex, что в PowerShell («правки после 2e» v1 RED:
# строка «(s_obs = s_exp +/- ...)» в отчёте синтетических отклонений совпала с (s_exp|s_obs)\s*=).
import re as _re
BLOCK_SCANNER = _re.compile(r'(s_exp|s_obs)\s*=|"ts"|\b1[78]\d{11}\b')


def test_b07h_stdout_passes_block_value_scanner(tmp_path, capsys):
    j, par, poly, _ = _setup(tmp_path, lambda v, t: _new_good(v, t, n=28, lounge=3, vol=4),
                             manual=dict(MANUAL, kappa={"lounge_brightness": 255}))
    rc = cs2e.main(["--path", str(j), "--params", str(par), "--poly-dir", str(poly)])
    out = capsys.readouterr().out
    assert rc == 0
    hits = [ln for ln in out.splitlines() if BLOCK_SCANNER.search(ln)]
    assert hits == [], hits
    assert "synthetic deviation on good commands (observed = expected" in out


# ---------------------------------------------------------------- (решение 16.11 13:43)
MANUAL_2I = dict(MANUAL, kappa={"lounge_brightness": 255}, Theta_state=0.5,
                 eps_norm={"setting_710": 0.1, "lounge_brightness": 0.1})


def test_b07i_manual_theta_state_no_false_observe_deviation_caught(tmp_path, capsys):
    def rows(v, t):
        r, v, t = _new_good(v, t, n=28, lounge=3, vol=4)
        r += _step(t, v, (v + 2) % 11)                                       # переход не из 2d (ложный OBSERVE)
        return r, v, t + 13_000
    j, par, poly, _ = _setup(tmp_path, rows, manual=MANUAL_2I)
    doc = cs2e.load_params(par)
    p = cs2e.kernel_params(doc)
    assert p.theta_state == 0.5 and doc["params"]["Theta_state"]["status"] == "MANUAL"
    lv = cs2e.live(doc, p, cs2e.sample(j, doc["holdout_2e"]["last_seq"]))
    assert lv["false_obs"]["n_good"] == 32 and lv["false_obs"]["observe"] == 0
    assert lv["deviation"]["n"] > 0 and lv["deviation"]["detected"] == lv["deviation"]["n"]
    rc = cs2e.main(["--path", str(j), "--params", str(par), "--poly-dir", str(poly)])
    out = capsys.readouterr().out
    assert rc == 0 and "Theta_state=MANUAL (Operator 16.11 13:43)" in out and "LOO record of Theta_state" in out
    assert "alarm reachable under file delta_rule: True" in out and "0 EXECUTE on must_not/violated/F01-27 (PROBE): True" in out
    assert [ln for ln in out.splitlines() if BLOCK_SCANNER.search(ln)] == []
