"""(фаза 3, этап 3a): тесты tools/sigma_patterns.py и tools/calibrate_integrator.py на синтетике.

Журнал -- синтетический (tmp_path), реальный logs/sigma_shadow.jsonl не читается. Этап
3a (УТВЕРЖДЁН Оператором 16.11 15:36); решение 14:51 п.1–2; реестр распад D по паузе, темп Δt_0.
"""
import importlib.util
import json
import math
import re
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, TOOLS / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sp = _load("sigma_patterns_b08a_test", "sigma_patterns.py")
ci = _load("calibrate_integrator_b08a_test", "calibrate_integrator.py")

T0 = 1795000000000                      # 13-значная метка мс, как в живом журнале (в вывод попадать не должна)
SEQ0 = 1795000000000
LEAK = re.compile(r'(s_exp|s_obs)\s*=|"ts"|\b1[78]\d{11}\b')


def cmd(seq, t_s, feature="setting_710", action="amb_step", outcome="PASS", target=5, after=None, pre=None, err=None):
    """Строки одной команды: [readback pre], send, ack, outcome."""
    t = T0 + int(round(t_s * 1000))
    rows = []
    if pre is not None:
        rows.append({"ts": t - 100, "step": "readback", "seq": seq, "feature": feature, "s_obs": pre})
    rows.append({"ts": t, "step": "send", "seq": seq, "feature": feature, "action": action, "s_exp": target})
    rows.append({"ts": t + 50, "step": "ack", "seq": seq})
    o = {"ts": t + 200, "step": "outcome", "seq": seq, "feature": feature, "action": action, "outcome": outcome,
         "s_exp": target, "s_obs": target if after is None else after}
    if err:
        o["err"] = err
    rows.append(o)
    return rows


def write_journal(path, cmds):
    with open(path, "w", encoding="utf-8") as f:
        for c in cmds:
            for r in c:
                f.write(json.dumps(r) + "\n")
    return path


def params_file(path, tau_D=60.0, crit=0.5, reset=0.2, last_seq=None, kappa=None, extra=None):
    doc = {"schema": "sigma_params/2d-v3",
           "params": {"tau_D": {"status": "MANUAL", "value": tau_D}, "Theta_crit": {"status": "MANUAL", "value": crit},
                      "Theta_reset": {"status": "MANUAL", "value": reset},
                      "kappa_j": {"status": "MANUAL", "value": kappa or {"setting_710": 10, "lounge_brightness": 255}}},
           "holdout_2e": {"rule": "x", "last_seq": last_seq}}
    if extra:
        doc.update(extra)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ sigma_patterns
def test_b08a_patterns_absent_journal_zero(tmp_path):
    st = sp.compute(tmp_path / "none.jsonl", None)
    assert st["commands"]["sigma_with_outcome"] == 0 and st["sessions"]["count"] == 0
    assert st["tempo_within"]["n"] == 0 and st["tempo_within"]["median"] is None
    assert st["session_rule"]["gap_s"] == 60.0


def test_b08a_patterns_session_split_gap_strict(tmp_path):
    # 0, 5, 10 | +60 ровно -> та же сессия (70) | пауза 61 -> новая (131, 136)
    j = write_journal(tmp_path / "j.jsonl", [cmd(SEQ0 + i, t) for i, t in enumerate((0, 5, 10, 70, 131, 136))])
    st = sp.compute(j, None)
    assert st["sessions"]["count"] == 2
    assert st["sessions"]["length_cmds"]["max"] == 4 and st["sessions"]["length_cmds"]["min"] == 2
    assert st["tempo_within"]["n"] == 4                       # 5, 5, 60, 5
    assert st["tempo_within"]["max"] == 60.0
    assert st["sessions"]["between_sessions_s"]["n"] == 1 and st["sessions"]["between_sessions_s"]["min"] == 61.0


def test_b08a_patterns_quantile_type7():
    v = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert sp.quantile(v, 0.5) == pytest.approx(5.5)
    assert sp.quantile(v, 0.1) == pytest.approx(1.9)
    assert sp.quantile(v, 0.9) == pytest.approx(9.1)
    assert sp.quantile([], 0.5) is None and sp.quantile([3.0], 0.9) == 3.0


def test_b08a_patterns_tempo_values_sorted_within_only(tmp_path):
    j = write_journal(tmp_path / "j.jsonl", [cmd(SEQ0 + i, t) for i, t in enumerate((0, 4, 13, 500, 509.5))])
    assert sp.tempo_values(j, 60.0) == [4.0, 9.0, 9.5]


def test_b08a_patterns_volume_isolated(tmp_path):
    cs = [cmd(SEQ0, 0), cmd(SEQ0 + 1, 5, feature="volume", action="volume_set", outcome="FAIL", err="verify_fail"),
          cmd(SEQ0 + 2, 10)]
    st = sp.compute(write_journal(tmp_path / "j.jsonl", cs), None)
    assert st["commands"]["all_seq"] == 3 and st["commands"]["sigma_with_outcome"] == 2
    assert st["commands"]["isolated"] == {"volume": 1}
    assert st["errors"]["fail_commands_any_fail_1"] == 0 and "volume" not in st["features"]
    assert st["tempo_within"]["n"] == 1 and st["tempo_within"]["min"] == 10.0


def test_b08a_patterns_errors_and_fail_runs(tmp_path):
    cs = [cmd(SEQ0, 0), cmd(SEQ0 + 1, 5, outcome="FAIL"), cmd(SEQ0 + 2, 10, outcome="FAIL", err="net"),
          cmd(SEQ0 + 3, 15, outcome="WARN", err="warn"), cmd(SEQ0 + 4, 20), cmd(SEQ0 + 5, 25, outcome="FAIL", err="parse_fail")]
    st = sp.compute(write_journal(tmp_path / "j.jsonl", cs), None)
    e = st["errors"]
    assert e["fail_commands_any_fail_1"] == 4                 # WARN с err -- сбой по any_fail_1
    assert e["by_type"]["other"] == 1 and e["by_type"]["net"] == 1 and e["by_type"]["warn"] == 1
    assert e["by_type"]["parse_fail"] == 1
    assert e["fail_runs_within_sessions"] == {"count": 2, "max_len": 3, "hist": {"1": 1, "3": 1}}
    assert st["actions"]["amb_step"]["outcome"] == {"PASS": 2, "WARN": 1, "FAIL": 3}


def test_b08a_patterns_steps_pre_and_previous_after(tmp_path):
    cs = [cmd(SEQ0, 0, target=4, pre=2), cmd(SEQ0 + 1, 5, target=6), cmd(SEQ0 + 2, 10, target=5, pre=6),
          cmd(SEQ0 + 3, 15, feature="lounge_brightness", action="amb_colour", target=128)]   # нет «до» -> без шага
    st = sp.compute(write_journal(tmp_path / "j.jsonl", cs), None)
    assert st["steps"]["setting_710"] == {"n": 3, "hist": {"-1": 1, "2": 2}}
    assert "lounge_brightness" not in st["steps"]
    assert st["features"]["setting_710"]["share"] == 0.75


def test_b08a_patterns_out_of_scale_counter_ma15(tmp_path):
    cs = [cmd(SEQ0, 0, target=5, after=12), cmd(SEQ0 + 1, 5, target=3, pre=-1), cmd(SEQ0 + 2, 10, target=7)]
    pf = params_file(tmp_path / "p.json")
    st = sp.compute(write_journal(tmp_path / "j.jsonl", cs), pf)
    assert st["scale"]["setting_710"] == {"kappa": 10.0, "commands": 3, "values_out_of_scale": 2}


def test_b08a_patterns_2e_segments_and_no_leak(tmp_path):
    cs = [cmd(SEQ0 + i, t) for i, t in enumerate((0, 5, 10, 20, 29))]
    pf = params_file(tmp_path / "p.json", last_seq=SEQ0 + 2)
    st = sp.compute(write_journal(tmp_path / "j.jsonl", cs), pf)
    sg = st["tempo_segments_2e"]
    assert sg["commands_upto"] == 3 and sg["commands_after"] == 2
    assert sg["upto_last_seq"]["n"] == 2 and sg["after_last_seq"]["n"] == 2 and sg["after_last_seq"]["max"] == 10.0
    txt = sp.render(st) + "\n" + sp.dumps(st).decode("utf-8")
    assert not LEAK.search(txt), LEAK.search(txt)
    assert str(SEQ0 + 2) not in txt


def test_b08a_patterns_sensitivity_gaps(tmp_path):
    cs = [cmd(SEQ0 + i, t) for i, t in enumerate((0, 40, 80, 180, 185))]
    st = sp.compute(write_journal(tmp_path / "j.jsonl", cs), None)
    s = st["tempo_sensitivity"]
    assert s["30"]["sessions"] == 4 and s["60"]["sessions"] == 2 and s["120"]["sessions"] == 1
    assert s["120"]["n"] == 4


def test_b08a_patterns_gap_from_params(tmp_path):
    cs = [cmd(SEQ0 + i, t) for i, t in enumerate((0, 25, 50))]
    j = write_journal(tmp_path / "j.jsonl", cs)
    st = sp.compute(j, params_file(tmp_path / "p.json", tau_D=20.0))
    assert st["session_rule"]["gap_s"] == 20.0 and st["sessions"]["count"] == 3
    assert sp.compute(j, None, gap_s=30.0)["sessions"]["count"] == 1


def test_b08a_patterns_bad_lines_skipped(tmp_path):
    j = write_journal(tmp_path / "j.jsonl", [cmd(SEQ0, 0), cmd(SEQ0 + 1, 3)])
    with open(j, "a", encoding="utf-8") as f:
        f.write("not json\n" + json.dumps({"ts": T0, "step": "send", "text": "x"}) + "\n")
    st = sp.compute(j, None)
    assert st["journal"]["lines_bad"] == 2 and st["commands"]["sigma_with_outcome"] == 2


def test_b08a_patterns_cli_deterministic(tmp_path, capsys):
    cs = [cmd(SEQ0 + i, t, outcome="FAIL" if i == 2 else "PASS") for i, t in enumerate((0, 5, 11, 18, 300, 306))]
    j = write_journal(tmp_path / "j.jsonl", cs)
    pf = params_file(tmp_path / "p.json", last_seq=SEQ0 + 3)
    o1, o2 = tmp_path / "a.json", tmp_path / "b.json"
    assert sp.main(["--path", str(j), "--params", str(pf), "--out", str(o1)]) == 0
    t1 = capsys.readouterr().out
    assert sp.main(["--path", str(j), "--params", str(pf), "--out", str(o2)]) == 0
    t2 = capsys.readouterr().out
    assert o1.read_bytes() == o2.read_bytes() and t1 == t2
    assert not LEAK.search(t1) and not LEAK.search(o1.read_text(encoding="utf-8"))
    assert json.loads(o1.read_text(encoding="utf-8"))["schema"] == "sigma_patterns/3a-v1"
    assert sp.main(["--path", str(j), "--gap", "0"]) == 2


# ------------------------------------------------------------------ calibrate_integrator
Q_NOM = 1 - math.exp(-13 / 60)


def test_b08a_integrator_nominal_13s_four_five():
    assert ci.simulate(Q_NOM, 60, 13) == (4, 5)
    lo, hi = ci.dt_band(Q_NOM, 60)
    assert lo < 13 <= hi
    assert lo == pytest.approx(12.815, abs=2e-3) and hi == pytest.approx(15.274, abs=2e-3)


@pytest.mark.parametrize("q,tau", [(0.165, 20.0), (0.18, 33.5), (0.1948, 60.0), (0.2, 100.0), (0.21, 7.0)])
def test_b08a_integrator_band_matches_simulation(q, tau):
    b = ci.dt_band(q, tau)
    assert b is not None
    lo, hi = b
    for x in (lo + (hi - lo) * f for f in (0.02, 0.5, 0.98)):
        assert ci.simulate(q, tau, x) == (4, 5), (q, tau, x)
    assert ci.simulate(q, tau, hi * 1.01) != (4, 5)
    assert ci.simulate(q, tau, lo * 0.99) != (4, 5)


def test_b08a_integrator_band_scales_with_tau_ratio_invariant():
    b1, b2 = ci.dt_band(0.19, 30.0), ci.dt_band(0.19, 90.0)
    assert b2[0] == pytest.approx(3 * b1[0]) and b2[1] == pytest.approx(3 * b1[1])
    assert b2[1] / b2[0] == pytest.approx(b1[1] / b1[0])


def test_b08a_integrator_no_band_outside_q_range():
    assert ci.u_band(0.10) is None            # 4q < Θ_crit -> тревога недостижима
    assert ci.u_band(0.5) is None and ci.u_band(0.0) is None
    assert ci.u_band(0.3) is None             # тревога раньше 4-го при любом шаге, где сброс на 5-м


def test_b08a_integrator_max_ratio_below_1_25():
    r = [u[1] / u[0] for u in (ci.u_band(q) for q in ci.Q_GRID) if u is not None]
    assert r and max(r) < 1.25


def test_b08a_integrator_ma16_vector_stop_wide_tempo():
    # темп Δt_0: форма 2d -- q10 4,7 / медиана 5,2 / q90 34,2 с -> одной пары нет, СТОП
    dts = [4.7] * 3 + [5.0] * 5 + [5.2] * 8 + [6.0] * 4 + [9.0] * 3 + [20.0] * 2 + [34.2] * 3
    res = ci.calibrate(dts)
    assert res["status"] == "STOP" and res["chosen"] is None and res["covering_pairs"] == 0
    assert res["need_ratio_q90_q10"] > res["u_band"]["max_ratio_hi_lo"]
    assert res["best_share"]["behaviour"]["median"]["alarm_at"] == 4


def test_b08a_integrator_mind_check_tau60_5_2s():
    m = ci.mind_check(60, 5.2)
    assert m["resets_at_q_ends"] == [11, 14] and m["alarm_at_q_ends"] == [4, 4]
    f = ci.feasible_tau(5.2)
    assert f["tau_D_sup"] == pytest.approx(28.17, abs=0.05)
    assert ci.feasible_tau(9.0)["tau_D_sup"] == pytest.approx(48.76, abs=0.05)


def test_b08a_integrator_pair_chosen_narrow_tempo():
    dts = [5.0, 5.05, 5.1, 5.15, 5.2, 5.2, 5.25, 5.3, 5.35, 5.4, 5.45, 5.5]
    res = ci.calibrate(dts)
    assert res["status"] == "PAIR_CHOSEN", res["reason"]
    c = res["chosen"]
    lo, hi = c["band_s"]
    t = res["tempo_within"]
    assert lo < t["q10"] and hi >= t["q90"]
    for k in ("q10", "median", "q90"):
        assert (c["behaviour"][k]["alarm_at"], c["behaviour"][k]["reset_after"]) == (4, 5)
    assert c["q"] in ci.Q_GRID and c["tau_D"] in ci.TAU_GRID


def test_b08a_integrator_min_n_stop():
    res = ci.calibrate([13.0] * (ci.MIN_N - 1))
    assert res["status"] == "STOP" and res["reason"].startswith("NO_DATA")


def test_b08a_integrator_lambda0_variant_contains_nominal():
    lv = ci.calibrate([13.0] * 12)["lambda0_variant"]
    assert lv["nominal_13s_inside"] is True
    lo, hi = lv["dt0_range_at_tau60_s"]
    assert lo < 13 < hi


def test_b08a_integrator_ma07_vector_single_fail_after_hour():
    # распад D по паузе: D_0 = 0, пауза 3600 с, один сбой -> импульсная форма: D = q < Θ_crit (старая форма давала D ≈ 1)
    d = ci.step(0.0, 3600.0, Q_NOM, 60.0, 1)
    assert d == pytest.approx(Q_NOM) and d < ci.THETA_CRIT_MANUAL
    old = math.exp(-3600 / 60) * 0 + (1 - math.exp(-3600 / 60)) * 1
    assert old >= ci.THETA_CRIT_MANUAL


def test_b08a_integrator_ma16_vector_nominal_pair_at_5_2s():
    # темп Δt_0: номинальная пара (q 13/60, τ_D 60) при реальном темпе 5,2 с -- тревога на 3-м, сброс через 12 успехов
    assert ci.simulate(Q_NOM, 60, 5.2) == (3, 12)


def _journal_tempo(tmp_path, steps):
    t, cs = 0.0, []
    for i, s in enumerate(steps):
        t += s
        cs.append(cmd(SEQ0 + i, t))
    return write_journal(tmp_path / "j.jsonl", cs)


def test_b08a_integrator_cli_stop_params_untouched(tmp_path, capsys):
    j = _journal_tempo(tmp_path, [5.0] * 6 + [30.0] * 6 + [5.2] * 6)
    pf = params_file(tmp_path / "p.json")
    before = pf.read_bytes()
    rc = ci.main(["--path", str(j), "--params", str(pf), "--out", str(tmp_path / "i.json"), "--write-params", str(pf)])
    out = capsys.readouterr().out
    assert rc == ci.EXIT_STOP and pf.read_bytes() == before
    assert "status: STOP" in out and "params: NOT touched" in out
    assert not LEAK.search(out) and not LEAK.search((tmp_path / "i.json").read_text(encoding="utf-8"))


def test_b08a_integrator_cli_pair_written_byte_for_byte(tmp_path, capsys):
    j = _journal_tempo(tmp_path, [0.0] + [5.0, 5.1, 5.2, 5.3, 5.4] * 3)
    pf = params_file(tmp_path / "p.json", extra={"delta_rule": {"rule": "any_fail_1"}})
    keep = {k: v for k, v in json.loads(pf.read_text(encoding="utf-8")).items()}
    assert ci.main(["--path", str(j), "--params", str(pf), "--write-params", str(pf)]) == ci.EXIT_OK
    b1 = pf.read_bytes()
    assert ci.main(["--path", str(j), "--params", str(pf), "--write-params", str(pf)]) == ci.EXIT_OK
    assert pf.read_bytes() == b1
    doc = json.loads(b1.decode("utf-8"))
    sec = doc.pop("integrator_3a")
    assert doc == keep
    assert sec["status"] == "CALIBRATED_3A" and sec["Theta_crit"] == 0.5 and sec["Theta_reset"] == 0.2
    assert sec["session_gap_s"] == 60.0 and sec["targets"] == {"alarm_at": 4, "reset_after": 5}
    out = capsys.readouterr().out
    assert "params: integrator_3a written" in out and not LEAK.search(out)


def test_b08a_integrator_theta_mismatch_input_error(tmp_path, capsys):
    j = _journal_tempo(tmp_path, [5.0] * 12)
    pf = params_file(tmp_path / "p.json", crit=0.55)
    assert ci.main(["--path", str(j), "--params", str(pf)]) == ci.EXIT_INPUT
    assert "INPUT_ERROR" in capsys.readouterr().out
    assert ci.main(["--path", str(j), "--params", str(tmp_path / "absent.json")]) == ci.EXIT_INPUT
