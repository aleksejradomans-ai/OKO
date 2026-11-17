"""«калибровка 2d» + (16.11, фаза 2, этап 2d): tools/calibrate_sigma.py -- детерминизм, изоляция volume, NO_DATA, MANUAL_PENDING,
Θ_reset < Θ_crit, строки T_θ сумма 1, stdout только счётчики; без holdout (весь журнал, holdout_2e.last_seq),
MANUAL eps_state / tau_b с journal_note, лишние поля --manual игнорируются (решение 16.11 12:04)."""
from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parent / "calibrate_sigma.py"
_spec = importlib.util.spec_from_file_location("calibrate_sigma_b07e", _TOOL)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

TS0 = 1794700000000
SEQ0 = 1794700000000
LAT = 4242427
MANUAL = {"ref": "решение 2026-11-16 — ручные значения 2d (тест)", "tau_D": 120, "Theta_crit": 0.6,
          "Theta_reset": 0.25, "R_t": {"N_obs": 2, "N_route": 1, "N_reflect": 1}}


def _step_cmd(i, before, target, obs=None, outcome="PASS", feature="setting_710", action="amb_step", err=None,
              gap=30_000):
    """Команда вида readback(до) -> send -> ack -> readback(после) -> outcome (как setting_N после )."""
    seq = SEQ0 + i * gap
    ts = TS0 + i * gap
    obs = target if obs is None else obs
    rows = [{"ts": ts, "step": "readback", "seq": seq, "cmd_kind": "verified", "action": action, "feature": feature,
             "s_obs": before, "latency_ms": LAT},
            {"ts": ts + 1, "step": "send", "seq": seq, "cmd_kind": "verified", "action": action, "feature": feature,
             "s_exp": target},
            {"ts": ts + 2, "step": "ack", "seq": seq, "cmd_kind": "verified", "action": action, "feature": feature,
             "s_exp": target, "latency_ms": LAT},
            {"ts": ts + 3, "step": "readback", "seq": seq, "cmd_kind": "verified", "action": action,
             "feature": feature, "s_obs": obs, "latency_ms": LAT}]
    o = {"ts": ts + 4, "step": "outcome", "seq": seq, "cmd_kind": "verified", "action": action, "feature": feature,
         "outcome": outcome, "s_exp": target, "s_obs": obs, "latency_ms": LAT}
    if err:
        o["err"] = err
    rows.append(o)
    return rows


def _volume_cmd(i):
    seq = SEQ0 + i * 30_000 + 7
    ts = TS0 + i * 30_000 + 7
    return [{"ts": ts, "step": "send", "seq": seq, "cmd_kind": "verified", "action": "volume_up", "feature": "volume"},
            {"ts": ts + 1, "step": "ack", "seq": seq, "cmd_kind": "verified", "action": "volume_up",
             "feature": "volume", "s_exp": 13, "latency_ms": LAT},
            {"ts": ts + 2, "step": "readback", "seq": seq, "cmd_kind": "verified", "action": "volume_up",
             "feature": "volume", "s_obs": 0, "latency_ms": LAT},
            {"ts": ts + 3, "step": "outcome", "seq": seq, "cmd_kind": "verified", "action": "volume_up",
             "feature": "volume", "outcome": "FAIL", "err": "verify_fail", "s_exp": 13, "s_obs": 0, "latency_ms": LAT}]


def _journal(n=20, start=0):
    rows, v = [], start
    for i in range(n):
        t = (v + 1) % 11
        rows += _step_cmd(i, v, t)
        v = t
    return rows


def _write(p: Path, rows) -> Path:
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def _run(tmp_path, rows, manual=MANUAL, name="j.jsonl", out="out.json", extra=()):
    j = _write(tmp_path / name, rows)
    argv = ["--path", str(j), "--out", str(tmp_path / out), *extra]
    if manual is not None:
        m = tmp_path / "manual.json"
        m.write_text(json.dumps(manual), encoding="utf-8")
        argv += ["--manual", str(m)]
    return cs.main(argv), tmp_path / out


def test_b07e_deterministic_byte_for_byte(tmp_path):
    rc1, o1 = _run(tmp_path, _journal(), out="a.json")
    rc2, o2 = _run(tmp_path, _journal(), out="b.json")
    assert rc1 == rc2 == 0
    assert o1.read_bytes() == o2.read_bytes()
    assert b"generated" not in o1.read_bytes()


def test_b07e_all_20_params_have_source(tmp_path):
    rc, o = _run(tmp_path, _journal())
    assert rc == 0
    p = json.loads(o.read_text(encoding="utf-8"))["params"]
    assert list(p) == sorted(cs.PARAMS) and len(p) == 20
    for k, v in p.items():
        assert v["status"] in ("JOURNAL", "MANUAL", "NO_DATA"), k
        s = v["source"]
        if v["status"] == "JOURNAL":
            assert s["file"] == "logs/sigma_shadow.jsonl" and isinstance(s["n"], int) and s["n"] > 0 and s["method"]
        elif v["status"] == "MANUAL":
            assert s["manual"]
        else:
            assert s["no_data"] and v["value"] is None
    for k in ("tau_h", "tau_i", "kappa_space", "eps_cov", "Theta_intent", "Theta_margin", "Theta_goal"):
        assert p[k]["status"] == "NO_DATA"
    assert p["T_theta"]["menu"]["status"] == "NO_DATA"
    for k in ("tau_D", "Theta_crit", "Theta_reset", "R_t"):
        assert p[k]["status"] == "MANUAL" and p[k]["source"]["manual"] == MANUAL["ref"]
    assert p["tau_D"]["value"] == 120.0 and p["R_t"]["value"] == MANUAL["R_t"]


def test_b07e_manual_pending_no_file_nonzero(tmp_path, capsys):
    rc, o = _run(tmp_path, _journal(), manual=None)
    assert rc == cs.EXIT_PENDING != 0
    assert not o.exists()
    out = capsys.readouterr().out
    assert "MANUAL_PENDING" in out and "tau_D" in out and "Theta_crit" in out and "Theta_reset" in out
    partial = {k: v for k, v in MANUAL.items() if k != "Theta_reset"}
    rc, o = _run(tmp_path, _journal(), manual=partial)
    assert rc == cs.EXIT_PENDING and not o.exists()


def test_b07e_default_out_not_created_when_pending(tmp_path, monkeypatch):
    target = tmp_path / "config" / "sigma_params.json"
    monkeypatch.setattr(cs, "DEFAULT_OUT", target)
    j = _write(tmp_path / "j.jsonl", _journal())
    assert cs.main(["--path", str(j), "--out", str(target)]) == cs.EXIT_PENDING
    assert not target.exists() and not target.parent.exists()


@pytest.mark.parametrize("crit,reset", [(0.3, 0.3), (0.2, 0.5)])
def test_b07e_theta_reset_must_be_below_crit(tmp_path, crit, reset):
    m = dict(MANUAL, Theta_crit=crit, Theta_reset=reset)
    rc, o = _run(tmp_path, _journal(), manual=m)
    assert rc == cs.EXIT_INVALID and not o.exists()
    with pytest.raises(cs.ManualError):
        cs.validate_manual(m)


@pytest.mark.parametrize("bad", [{"tau_D": 0}, {"tau_D": "60"}, {"Theta_crit": float("nan")}, {"ref": ""},
                                 {"R_t": {"N_obs": 1, "N_route": 1}}, {"R_t": {"N_obs": -1, "N_route": 1, "N_reflect": 1}}])
def test_b07e_invalid_manual_rejected(tmp_path, bad):
    with pytest.raises(cs.ManualError):
        cs.validate_manual(dict(MANUAL, **bad))


def test_b07e_volume_isolated(tmp_path):
    base = _journal()
    with_vol = list(base)
    for i in range(0, 20, 3):
        with_vol += _volume_cmd(i)
    with_vol.sort(key=lambda r: (r["seq"], r["ts"]))
    r1 = cs.calibrate(_write(tmp_path / "a.jsonl", base), MANUAL)
    r2 = cs.calibrate(_write(tmp_path / "b.jsonl", with_vol), MANUAL)
    assert r2["doc"]["params"] == r1["doc"]["params"]
    assert r2["counts"]["error_types"]["verify_fail"] == 0
    assert r2["doc"]["journal"]["isolated"] == {"volume": 7}
    p = r2["doc"]["params"]
    assert "volume" not in json.dumps(p["T_theta"]["value"]) and "volume" not in json.dumps(p["Z_theta"]["value"])
    assert "volume" not in json.dumps(p["pi_0"]["value"])


def test_b07e_single_value_feature_no_data_not_zero(tmp_path):
    rows = []
    for i in range(20):
        rows += _step_cmd(i, 255, 255, feature="lounge_brightness", action="amb_colour")
    res = cs.calibrate(_write(tmp_path / "j.jsonl", rows), MANUAL)      # без κ из --manual (κ вручную -- в Σ)
    p = res["doc"]["params"]
    assert p["eps_state"]["status"] == "NO_DATA" and p["eps_state"]["value"] is None
    assert p["kappa_j"]["status"] == "NO_DATA"
    assert "lounge_brightness" in p["kappa_j"]["source"]["per_feature"]
    assert "no_data" in p["kappa_j"]["source"]["per_feature"]["lounge_brightness"]
    # тот же признак рядом с калибруемым: ε считается только по setting_710, lounge -- NO_DATA в разборе
    mixed = _journal() + [dict(r, seq=r["seq"] + 1, ts=r["ts"] + 11) for r in rows]
    mixed.sort(key=lambda r: (r["seq"], r["ts"]))
    p2 = cs.calibrate(_write(tmp_path / "m.jsonl", mixed), MANUAL)["doc"]["params"]
    assert p2["eps_state"]["status"] == "JOURNAL"
    assert "lounge_brightness" in p2["eps_state"]["source"]["per_feature_no_data"]
    assert list(p2["kappa_j"]["value"]) == ["setting_710"] and p2["alpha_j"]["value"] == {"setting_710": 1.0}


def test_b07e_t_theta_rows_sum_to_one(tmp_path):
    rows = _journal(30)
    rows += _step_cmd(31, 3, 4, obs=5, outcome="FAIL", err="verify_fail")
    rows += _step_cmd(32, 5, 6)
    res = cs.calibrate(_write(tmp_path / "j.jsonl", rows), MANUAL)
    p = res["doc"]["params"]
    view = cs.view_from_values(p["T_theta"]["value"], p["Z_theta"]["value"], p["pi_0"]["value"])
    states = cs.states_of(p["T_theta"]["value"]["states"]["setting_710"])
    assert states == [str(x) for x in range(11)]
    api = p["T_theta"]["value"]["api"]
    assert api
    for k, matrix in api.items():                       # строки T_θ (сглажены) -- распределения по S_f
        for x, row in list(matrix.items()) + [("unseen", None)]:
            vec = cs._lid_vec(row, states)
            assert abs(sum(vec) - 1.0) < 1e-12 and all(v > 0 for v in vec), (k, x)
    for x in states:                                    # Z_θ(·|x) -- распределение по s_obs для любого x
        col = [cs.z_likelihood(view, "setting_710", o, states)[states.index(x)] for o in states]
        assert abs(sum(col) - 1.0) < 1e-12 and all(v > 0 for v in col), x
    for f, d in p["pi_0"]["value"].items():
        vec = cs._lid_vec(d, cs.states_of(p["T_theta"]["value"]["states"][f]))
        assert abs(sum(vec) - 1.0) < 1e-12
    assert abs(sum(p["beta_k"]["value"].values()) - 1.0) < 1e-12 and len(p["beta_k"]["value"]) == 6
    assert "other" in p["beta_k"]["value"]


def test_b07e_eps_state_from_mismatch_on_pass(tmp_path):
    rows = _journal(9)
    rows += _step_cmd(9, 9, 10, obs=8)          # PASS c расхождением 2 при κ = 10 -> d = 0.2
    rows += _journal(30)[10 * 5:]               # ещё команды после расхождения
    rows.sort(key=lambda r: (r["seq"], r["ts"]))
    p = cs.calibrate(_write(tmp_path / "j.jsonl", rows), MANUAL)["doc"]["params"]
    assert p["eps_state"]["status"] == "JOURNAL"
    assert abs(p["eps_state"]["value"] - 0.2) < 1e-12
    assert 0.0 <= p["Theta_state"]["value"] <= 1.0


def test_b07e_stdout_counters_only(tmp_path):
    secret_exp, secret_obs = 987654321, 876543219
    rows = _journal(20)
    rows += _step_cmd(25, 7, secret_exp, obs=secret_obs, outcome="WARN", err="warn")
    rows.sort(key=lambda r: (r["seq"], r["ts"]))
    j = _write(tmp_path / "j.jsonl", rows)
    m = tmp_path / "m.json"
    m.write_text(json.dumps(MANUAL), encoding="utf-8")
    for extra in ([], ["--manual", str(m)], ["--manual", str(m), "--dry-run"]):
        p = subprocess.run([sys.executable, str(_TOOL), "--path", str(j), "--out", str(tmp_path / "o.json"), *extra],
                           capture_output=True, timeout=120)
        out = p.stdout.decode("utf-8")
        assert p.returncode in (0, 3), out
        for v in (str(secret_exp), str(secret_obs), str(TS0)[:9], str(SEQ0)[:9], str(LAT), "120", "0.6", "0.25"):
            assert v not in out, (v, extra)
        assert "counters only" in out
        for line in out.splitlines():
            assert "{" not in line and "s_exp=" not in line and "s_obs=" not in line


def test_b07e_empty_or_absent_journal(tmp_path):
    rc, o = _run(tmp_path, [], manual=MANUAL)
    assert rc == 0
    p = json.loads(o.read_text(encoding="utf-8"))["params"]
    for k in ("tau_b", "eps_state", "Theta_state", "T_theta", "Z_theta", "pi_0", "alpha_j", "kappa_j"):
        assert p[k]["status"] == "NO_DATA", k
    res = cs.calibrate(tmp_path / "absent.jsonl", None)
    assert res["missing"] == list(cs.MANUAL_REQUIRED) and res["counts"]["commands"] == 0


def test_b07e_bad_lines_skipped(tmp_path):
    j = tmp_path / "j.jsonl"
    good = "".join(json.dumps(r) + "\n" for r in _journal())
    j.write_text("not json\n" + '{"ts": 1, "step": "send", "text": "x"}\n' + good, encoding="utf-8")
    res = cs.calibrate(j, MANUAL)
    assert res["counts"]["lines_bad"] == 2 and res["counts"]["commands"] == 20


def test_b07e_uses_stats_isolate_default():
    assert cs.ISOLATE_DEFAULT is cs.sss.ISOLATE_DEFAULT and "volume" in cs.ISOLATE_DEFAULT


# ---------------------------------------------------------------- (решение 16.11 12:04)
MANUAL_2D = {"ref": "решение 2026-11-16 12:04 — ручные значения 2d (тест)", "tau_D": 60, "Theta_crit": 0.50,
             "Theta_reset": 0.20, "R_t": {"N_obs": 3, "N_route": 3, "N_reflect": 2},
             "eps_state": {"setting_710": 1}, "tau_b": 600,
             "holdout": "none (2e — новые команды после закрытия 2d)"}


def test_b07f_no_holdout_all_commands_used(tmp_path):
    rows = _journal(20)
    res = cs.calibrate(_write(tmp_path / "a.jsonl", rows), MANUAL)
    doc = res["doc"]
    assert "holdout" not in doc and not hasattr(cs, "split_holdout")
    assert doc["holdout_2e"] == {"rule": "новые команды, seq > last_seq", "last_seq": SEQ0 + 19 * 30_000}
    assert doc["sample"]["commands"] == 20 == res["counts"]["commands"]
    assert doc["params"]["Z_theta"]["source"]["n"] == 20       # все 20 пар в расчёте
    # последняя команда (раньше -- в holdout) влияет на параметры
    spoiled = [dict(r, s_obs=((r["s_exp"] if "s_exp" in r else r["s_obs"]) + 2) % 11)
               if r["seq"] == SEQ0 + 19 * 30_000 and r["step"] in ("outcome",) else r for r in rows]
    res2 = cs.calibrate(_write(tmp_path / "b.jsonl", spoiled), MANUAL)
    assert res2["doc"]["params"]["eps_state"]["value"] > res["doc"]["params"]["eps_state"]["value"]


def test_b07f_last_seq_whole_journal_incl_isolated(tmp_path):
    rows = _journal(5) + _volume_cmd(10)                     # volume -- последний по seq
    rows.sort(key=lambda r: (r["seq"], r["ts"]))
    doc = cs.calibrate(_write(tmp_path / "j.jsonl", rows), MANUAL)["doc"]
    assert doc["holdout_2e"]["last_seq"] == SEQ0 + 10 * 30_000 + 7
    assert doc["sample"]["commands"] == 5
    assert cs.calibrate(tmp_path / "absent.jsonl", MANUAL)["doc"]["holdout_2e"]["last_seq"] is None


def test_b07f_manual_eps_state_and_tau_b(tmp_path):
    rc, o = _run(tmp_path, _journal(30), manual=MANUAL_2D)
    assert rc == 0
    p = json.loads(o.read_text(encoding="utf-8"))["params"]
    e, t = p["eps_state"], p["tau_b"]
    assert e["status"] == "MANUAL" and e["source"]["manual"] == MANUAL_2D["ref"]
    assert e["source"]["per_feature"] == {"setting_710": 1.0}
    assert abs(e["value"] - 0.1) < 1e-12                      # α = 1, κ = 10: 1 деление -> d_Σ 0,1
    assert e["source"]["journal_note"]["status"] == "JOURNAL" and e["source"]["journal_note"]["value"] == 0.0
    assert e["source"]["journal_note"]["source"]["n"] == 30
    assert t["status"] == "MANUAL" and t["value"] == 600.0 and t["source"]["manual"] == MANUAL_2D["ref"]
    assert t["source"]["journal_note"]["status"] == "JOURNAL" and t["source"]["journal_note"]["source"]["n"] == 29
    assert "spread_s" in t["source"]["journal_note"]["source"]
    assert "MANUAL" in p["Theta_state"]["source"]["method"]
    for k in ("tau_D", "Theta_crit", "Theta_reset", "R_t"):
        assert p[k]["status"] == "MANUAL" and p[k]["source"]["manual"] == MANUAL_2D["ref"]
    assert (p["tau_D"]["value"], p["Theta_crit"]["value"], p["Theta_reset"]["value"]) == (60.0, 0.5, 0.2)
    assert p["R_t"]["value"] == {"N_obs": 3, "N_reflect": 2, "N_route": 3}


def test_b07f_manual_eps_widens_theta_state_set(tmp_path):
    rows = _journal(30)
    p0 = cs.calibrate(_write(tmp_path / "a.jsonl", rows), MANUAL)["doc"]["params"]
    p1 = cs.calibrate(_write(tmp_path / "b.jsonl", rows), MANUAL_2D)["doc"]["params"]
    assert p0["eps_state"]["status"] == "JOURNAL" and p1["eps_state"]["status"] == "MANUAL"
    assert p1["eps_state"]["value"] >= p0["eps_state"]["value"]
    assert p1["Theta_state"]["value"] >= p0["Theta_state"]["value"]   # A_ε шире -> C_state не меньше
    for k in ("T_theta", "Z_theta", "pi_0", "kappa_j", "alpha_j", "beta_k"):
        assert p1[k] == p0[k], k                                       # остальное не меняется


def test_b07f_manual_eps_for_non_sigma_feature_no_data(tmp_path):
    rows = []
    for i in range(20):
        rows += _step_cmd(i, 255, 255, feature="lounge_brightness", action="amb_colour")
    m = dict(MANUAL_2D, eps_state={"lounge_brightness": 3})
    p = cs.calibrate(_write(tmp_path / "j.jsonl", rows), m)["doc"]["params"]
    e = p["eps_state"]
    assert e["status"] == "NO_DATA" and e["value"] is None and e["source"]["no_data"]
    assert "lounge_brightness" in e["source"]["per_feature_no_data"]
    assert e["source"]["journal_note"]["status"] == "NO_DATA"
    assert p["Theta_state"]["status"] == "NO_DATA"


def test_b07f_extra_manual_fields_ignored(tmp_path):
    m = dict(MANUAL, holdout="none (2e — новые команды после закрытия 2d)", comment={"x": 1}, note=[1, 2])
    rc, o = _run(tmp_path, _journal(), manual=m)
    rc0, o0 = _run(tmp_path, _journal(), manual=MANUAL, out="o0.json")
    assert rc == rc0 == 0 and o.read_bytes() == o0.read_bytes()
    man, missing = cs.validate_manual(m)
    assert missing == [] and "holdout" not in man and "comment" not in man


@pytest.mark.parametrize("bad", [{"tau_b": 0}, {"tau_b": "600"}, {"tau_b": float("inf")}, {"eps_state": {}},
                                 {"eps_state": [1]}, {"eps_state": {"setting_710": -1}},
                                 {"eps_state": {"setting_710": "1"}}, {"eps_state": {"setting_710": True}}])
def test_b07f_invalid_manual_eps_tau_b_rejected(tmp_path, bad):
    with pytest.raises(cs.ManualError):
        cs.validate_manual(dict(MANUAL_2D, **bad))
    rc, o = _run(tmp_path, _journal(), manual=dict(MANUAL_2D, **bad))
    assert rc == cs.EXIT_INVALID and not o.exists()


def test_b07f_optional_manual_needs_ref():
    with pytest.raises(cs.ManualError):
        cs.validate_manual({"tau_b": 600})
    man, missing = cs.validate_manual({"ref": "r", "tau_b": 600})
    assert man["tau_b"] == 600.0 and missing == list(cs.MANUAL_REQUIRED)


def test_b07f_deterministic_byte_for_byte_with_manual_2d(tmp_path):
    rows = _journal(30) + _volume_cmd(31)
    rows.sort(key=lambda r: (r["seq"], r["ts"]))
    rc1, o1 = _run(tmp_path, rows, manual=MANUAL_2D, out="a.json")
    rc2, o2 = _run(tmp_path, rows, manual=MANUAL_2D, out="b.json")
    assert rc1 == rc2 == 0 and o1.read_bytes() == o2.read_bytes()
    assert o1.read_bytes().endswith(b"\n") and not o1.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("crit,reset,ok", [(0.5, 0.2, True), (0.5, 0.5, False), (0.2, 0.5, False), (0.5, 0.0, True)])
def test_b07f_theta_reset_below_crit_with_manual_2d(tmp_path, crit, reset, ok):
    m = dict(MANUAL_2D, Theta_crit=crit, Theta_reset=reset)
    rc, o = _run(tmp_path, _journal(), manual=m)
    if ok:
        p = json.loads(o.read_text(encoding="utf-8"))["params"]
        assert rc == 0 and p["Theta_reset"]["value"] < p["Theta_crit"]["value"]
    else:
        assert rc == cs.EXIT_INVALID and not o.exists()


def test_b07f_stdout_counters_only_with_manual_2d(tmp_path):
    secret_exp, secret_obs = 987654321, 876543219
    rows = _journal(20) + _step_cmd(25, 7, secret_exp, obs=secret_obs, outcome="WARN", err="warn")
    rows.sort(key=lambda r: (r["seq"], r["ts"]))
    j = _write(tmp_path / "j.jsonl", rows)
    m = tmp_path / "m.json"
    m.write_text(json.dumps(MANUAL_2D, ensure_ascii=False), encoding="utf-8")
    p = subprocess.run([sys.executable, str(_TOOL), "--path", str(j), "--out", str(tmp_path / "o.json"),
                        "--manual", str(m)], capture_output=True, timeout=120)
    out = p.stdout.decode("utf-8")
    assert p.returncode == 0, out
    for v in (str(secret_exp), str(secret_obs), str(TS0)[:9], str(SEQ0)[:9], str(LAT)):
        assert v not in out, v
    assert "no holdout" in out and "last_seq set" in out and "journal_note_n=" in out
    assert "[MANUAL] eps_state" in out and "[MANUAL] tau_b" in out
    for line in out.splitlines():
        assert "{" not in line and "s_exp=" not in line and "s_obs=" not in line


def test_b07f_real_manual_file_shape_accepted(tmp_path):
    """Тот же состав, что ручной файл sigma_manual_2d.json (utf-8, с BOM и без)."""
    for enc in ("utf-8", "utf-8-sig"):
        j = _write(tmp_path / "j.jsonl", _journal(30))
        m = tmp_path / f"m_{enc}.json"
        m.write_text(json.dumps(MANUAL_2D, ensure_ascii=False, indent=2), encoding=enc)
        out = tmp_path / f"o_{enc}.json"
        assert cs.main(["--path", str(j), "--out", str(out), "--manual", str(m)]) == 0
        p = json.loads(out.read_text(encoding="utf-8"))["params"]
        st = {k: v["status"] for k, v in p.items()}
        assert [k for k, v in st.items() if v == "MANUAL_PENDING"] == []
        assert st["eps_state"] == st["tau_b"] == "MANUAL"


# ---------------------------------------------------------------- (решение 16.11 12:59)
MANUAL_2E = dict(MANUAL_2D, ref="решение 2026-11-16 12:04 + 12:59 — ручные значения 2d и правки после 2e (тест)",
                 kappa={"lounge_brightness": 255}, delta_rule="any_fail_1")


def _lounge_cmd(i, gap=30_000):
    return [dict(r, seq=r["seq"] + 3, ts=r["ts"] + 3) for r in
            _step_cmd(i, 255, 255, feature="lounge_brightness", action="amb_colour", gap=gap)]


def _journal_2e(n=30, lounge_every=5):
    rows = _journal(n)
    for i in range(0, n, lounge_every):
        rows += _lounge_cmd(i)
    rows.sort(key=lambda r: (r["seq"], r["ts"]))
    return rows


def test_b07h_delta_rule_written_beta_k_kept(tmp_path):
    rc, o = _run(tmp_path, _journal(), manual=MANUAL_2E)
    assert rc == 0
    doc = json.loads(o.read_text(encoding="utf-8"))
    d = doc["delta_rule"]
    assert d["rule"] == "any_fail_1" and d["delta_fail"] == 1 and d["delta_success"] == 0 and d["beta_k_used"] is False
    assert d["source"]["manual"] == MANUAL_2E["ref"] and "12:59" in d["source"]["decision"]
    b = doc["params"]["beta_k"]
    assert b["status"] == "MANUAL" and abs(sum(b["value"].values()) - 1.0) < 1e-12 and len(b["value"]) == 6
    assert "delta_rule=any_fail_1" in b["source"]["note"]
    legacy = cs.calibrate(_write(tmp_path / "l.jsonl", _journal()), MANUAL_2D)["doc"]
    assert legacy["delta_rule"]["rule"] == "beta_k"
    assert legacy["params"]["beta_k"]["value"] == b["value"]


@pytest.mark.parametrize("bad", ["any_fail", "", 1, True, ["any_fail_1"], {"rule": "any_fail_1"}])
def test_b07h_delta_rule_invalid_rejected(tmp_path, bad):
    with pytest.raises(cs.ManualError):
        cs.validate_manual(dict(MANUAL_2E, delta_rule=bad))
    rc, o = _run(tmp_path, _journal(), manual=dict(MANUAL_2E, delta_rule=bad))
    assert rc == cs.EXIT_INVALID and not o.exists()
    with pytest.raises(cs.ManualError):                  # число / правило без источника не пишется
        cs.validate_manual({"delta_rule": "any_fail_1"})


def test_b07h_real_manual_file_shape_accepted(tmp_path):
    """Состав ручного файла sigma_manual_2d.json после 12:59 (kappa + delta_rule), utf-8 с BOM и без."""
    for enc in ("utf-8", "utf-8-sig"):
        j = _write(tmp_path / "j.jsonl", _journal_2e())
        m = tmp_path / f"m_{enc}.json"
        m.write_text(json.dumps(MANUAL_2E, ensure_ascii=False, indent=2), encoding=enc)
        out = tmp_path / f"o_{enc}.json"
        assert cs.main(["--path", str(j), "--out", str(out), "--manual", str(m)]) == 0
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert [k for k, v in doc["params"].items() if v["status"] == "MANUAL_PENDING"] == []
        assert doc["delta_rule"]["rule"] == "any_fail_1"


def test_b07h_manual_kappa_brings_single_value_feature_into_sigma(tmp_path):
    p = cs.calibrate(_write(tmp_path / "j.jsonl", _journal_2e()), MANUAL_2E)["doc"]["params"]
    assert p["kappa_j"]["value"] == {"lounge_brightness": 255.0, "setting_710": 10.0}
    pf = p["kappa_j"]["source"]["per_feature"]["lounge_brightness"]
    assert pf["manual"] == MANUAL_2E["ref"] and "< 2" in pf["note"]
    assert p["alpha_j"]["value"] == {"lounge_brightness": 0.5, "setting_710": 0.5}
    e = p["eps_state"]
    assert e["status"] == "MANUAL" and abs(e["value"] - 0.05) < 1e-12        # α = 1/2, κ = 10, 1 деление
    assert e["source"]["implicit_per_feature"]["lounge_brightness"]["divisions"] == pytest.approx(25.5)
    assert cs.states_of(p["T_theta"]["value"]["states"]["lounge_brightness"]) == [str(x) for x in range(256)]
    assert p["Theta_state"]["status"] == "JOURNAL"
    # без κ из --manual -- признак по-прежнему NO_DATA (решение 16.11 01:41)
    p0 = cs.calibrate(_write(tmp_path / "k.jsonl", _journal_2e()), MANUAL_2D)["doc"]["params"]
    assert list(p0["kappa_j"]["value"]) == ["setting_710"] and "no_data" in p0["kappa_j"]["source"]["per_feature"]["lounge_brightness"]


def test_b07h_smoothing_new_values_and_transitions_small_nonzero(tmp_path):
    p = cs.calibrate(_write(tmp_path / "j.jsonl", _journal(30)), MANUAL_2E)["doc"]["params"]
    T, Z, PI = p["T_theta"]["value"], p["Z_theta"]["value"], p["pi_0"]["value"]
    states = cs.states_of(T["states"]["setting_710"])
    view = cs.view_from_values(T, Z, PI)
    row = T["api"]["setting_710|amb_step"]["3"]                  # в журнале 3 -> 4 (всегда +1)
    assert row["n"] >= 2 and set(row["seen"]) == {"4"}
    assert 0 < row["unseen"] < row["seen"]["4"]
    assert row["unseen"] == pytest.approx((1 / 11) / (row["n"] + 1))   # Лидстон α = 1/|S|, масса 1
    zl = cs.z_likelihood(view, "setting_710", "7", states)       # s_obs = 7 при s_exp != 7 -- не встречалось
    assert all(v > 0 for v in zl) and zl[states.index("7")] == max(zl)
    assert T["smoothing"]["alpha"]["setting_710"] == pytest.approx(1 / 11) and T["smoothing"]["mass_per_row"] == 1.0
    assert Z["beta"] == 1.0 and "setting_710" in Z["base"]


def test_b07h_state_space_scale_and_outlier_cap():
    assert cs.state_space({"3", "5"}, 10) == {"min": 0, "max": 10}
    assert cs.state_space({"-1", "12"}, 10) == {"min": -1, "max": 12}
    big = cs.state_space({"3", "987654321"}, 10)
    assert "list" in big and "987654321" in big["list"] and len(big["list"]) == 12
    assert cs.state_space({"2.5"}, 10)["list"][0] == "0"


def test_b07h_theta_state_leave_one_out_and_real_dt(tmp_path):
    rows = _journal(30)
    p = cs.calibrate(_write(tmp_path / "j.jsonl", rows), MANUAL_2E)["doc"]["params"]
    th = p["Theta_state"]
    assert th["status"] == "JOURNAL" and 0.0 < th["value"] <= th["source"]["in_sample_note"]["value"] <= 1.0
    assert "leave-one-out" in th["source"]["method"] and "Δt реальный" in th["source"]["method"]
    # ρ_t: Δt = ∞ -> b⁻ = π_0 (то же, что без «до»); Δt = 0 -> b⁻ = T_θ(·|до)
    view = cs.view_from_values(p["T_theta"]["value"], p["Z_theta"]["value"], p["pi_0"]["value"])
    cmds = cs.build_commands(cs.load_rows(tmp_path / "j.jsonl", cs.ISOLATE_DEFAULT)[0])
    c = cmds[5]
    a, k, e, tb = p["alpha_j"]["value"], p["kappa_j"]["value"], p["eps_state"]["value"], p["tau_b"]["value"]
    inf = cs.c_state(view, e, a, k, c, tb, dt_override=math.inf)
    nob = cs.c_state(view, e, a, k, dict(c, before=None), tb)
    assert inf == nob and inf[1] == "OK"
    assert c["dt_s"] is not None and c["dt_s"] >= 0
    assert cs.al.__name__ == "abstraction_layer_for_calibrate"
    assert Path(cs.al.__file__).resolve() == (Path(cs.ROOT) / "src" / "abstraction_layer.py").resolve()


def test_b07h_last_seq_from_freezes_2d(tmp_path):
    rows2d = _journal_2e(30)
    j = _write(tmp_path / "j.jsonl", rows2d)
    m = tmp_path / "m.json"
    m.write_text(json.dumps(MANUAL_2E, ensure_ascii=False), encoding="utf-8")
    first = tmp_path / "first.json"
    assert cs.main(["--path", str(j), "--out", str(first), "--manual", str(m)]) == 0
    last = json.loads(first.read_text(encoding="utf-8"))["holdout_2e"]["last_seq"]
    # журнал дописан командами 2e (другие переходы и значения) -- пересчёт с --last-seq-from их не видит
    extra = []
    for i in range(40, 52):
        extra += _step_cmd(i, (i * 3) % 11, (i * 7) % 11, obs=(i * 5) % 11)
    _write(j, rows2d + extra)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert cs.main(["--path", str(j), "--out", str(a), "--manual", str(m), "--last-seq-from", str(first)]) == 0
    assert cs.main(["--path", str(j), "--out", str(b), "--manual", str(m), "--last-seq-from", str(a)]) == 0
    assert a.read_bytes() == b.read_bytes()                                  # два прогона -> байт-в-байт
    da, d1 = json.loads(a.read_text(encoding="utf-8")), json.loads(first.read_text(encoding="utf-8"))
    assert da["holdout_2e"] == d1["holdout_2e"] and da["holdout_2e"]["last_seq"] == last
    assert da["params"] == d1["params"]                                      # только данные 2d
    assert da["sample"]["commands"] == d1["sample"]["commands"] and "upto" in da["sample"]
    assert "sha16_scope" in da["journal"]
    _write(j, rows2d + extra + _step_cmd(60, 1, 2))                          # ещё строки после last_seq
    c = tmp_path / "c.json"
    assert cs.main(["--path", str(j), "--out", str(c), "--manual", str(m), "--last-seq-from", str(a)]) == 0
    assert c.read_bytes() == a.read_bytes()
    # без --last-seq-from 2e попали бы в расчёт
    nofz = cs.calibrate(j, MANUAL_2E)["doc"]
    assert nofz["holdout_2e"]["last_seq"] != last and nofz["params"]["Z_theta"] != da["params"]["Z_theta"]


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"holdout_2e": {"last_seq": None}}), json.dumps([1])])
def test_b07h_last_seq_from_invalid_no_file(tmp_path, content):
    j = _write(tmp_path / "j.jsonl", _journal())
    m = tmp_path / "m.json"
    m.write_text(json.dumps(MANUAL_2E, ensure_ascii=False), encoding="utf-8")
    src = tmp_path / "prev.json"
    if content is not None:
        src.write_text(content, encoding="utf-8")
    out = tmp_path / "o.json"
    assert cs.main(["--path", str(j), "--out", str(out), "--manual", str(m), "--last-seq-from", str(src)]) == cs.EXIT_INVALID
    assert not out.exists()


def test_b07h_stdout_counters_only(tmp_path):
    secret_exp, secret_obs = 987654321, 876543219
    rows = _journal_2e(20) + _step_cmd(25, 7, secret_exp, obs=secret_obs, outcome="WARN", err="warn")
    rows.sort(key=lambda r: (r["seq"], r["ts"]))
    j = _write(tmp_path / "j.jsonl", rows)
    m = tmp_path / "m.json"
    m.write_text(json.dumps(MANUAL_2E, ensure_ascii=False), encoding="utf-8")
    first = tmp_path / "f.json"
    assert cs.main(["--path", str(j), "--out", str(first), "--manual", str(m)]) == 0
    p = subprocess.run([sys.executable, str(_TOOL), "--path", str(j), "--out", str(tmp_path / "o.json"),
                        "--manual", str(m), "--last-seq-from", str(first)], capture_output=True, timeout=120)
    out = p.stdout.decode("utf-8")
    assert p.returncode == 0, out
    for v in (str(secret_exp), str(secret_obs), str(TS0)[:9], str(SEQ0)[:9], str(LAT), "255", "0.05"):
        assert v not in out, v
    assert "delta_rule: any_fail_1" in out and "frozen from --last-seq-from" in out and "leave-one-out" in out
    for line in out.splitlines():
        assert "{" not in line and "s_exp=" not in line and "s_obs=" not in line


def test_b07h_deterministic_byte_for_byte_manual_2e(tmp_path):
    rows = _journal_2e(30) + _volume_cmd(31)
    rows.sort(key=lambda r: (r["seq"], r["ts"]))
    rc1, o1 = _run(tmp_path, rows, manual=MANUAL_2E, out="a.json")
    rc2, o2 = _run(tmp_path, rows, manual=MANUAL_2E, out="b.json")
    assert rc1 == rc2 == 0 and o1.read_bytes() == o2.read_bytes()
    assert o1.read_bytes().endswith(b"\n") and not o1.read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads(o1.read_text(encoding="utf-8"))["schema"] == "sigma_params/2d-v3"


# ---------------------------------------------------------------- (решение 16.11 13:43)
MANUAL_2I = dict(MANUAL_2E, ref="решение 2026-11-16 12:04 + 12:59 + 13:43 — ручные значения 2d и закрытие 2e (тест)",
                 Theta_state=0.5, eps_norm={"setting_710": 0.1, "lounge_brightness": 0.1})
CAL_SCANNER = __import__("re").compile(r'(s_exp|s_obs)\s*=|"ts"|\{')


def test_b07i_manual_theta_state_with_journal_and_loo_note(tmp_path):
    rows = _journal_2e(30)
    p = cs.calibrate(_write(tmp_path / "j.jsonl", rows), MANUAL_2I)["doc"]["params"]
    p0 = cs.calibrate(_write(tmp_path / "k.jsonl", rows), MANUAL_2E)["doc"]["params"]
    t = p["Theta_state"]
    assert t["status"] == "MANUAL" and t["value"] == 0.5 and t["source"]["manual"] == MANUAL_2I["ref"]
    assert "13:43" in t["source"]["decision"]
    assert t["source"]["journal_note"] == {k: v for k, v in p0["Theta_state"].items()}   # прежняя оценка -- для сравнения
    assert t["source"]["loo_note"] == p0["Theta_state"]["source"]["loo_note"]
    loo = t["source"]["loo_note"]
    assert 0 < loo["n"] <= t["source"]["journal_note"]["source"]["n"]
    assert loo["min"] == p0["Theta_state"]["value"] and loo["min"] <= loo["q05"] <= loo["q10"] <= loo["median"] <= loo["max"]
    assert loo["guard"] == "ok" and loo["robust_lower"] == loo["q05"] and loo["excluded"] == 0
    for k in ("T_theta", "Z_theta", "pi_0", "alpha_j"):
        assert p[k] == p0[k], k                                              # остальное без изменений
    for k in ("eps_state", "kappa_j"):                                       # отличается только ссылка ref
        assert (p[k]["status"], p[k]["value"]) == (p0[k]["status"], p0[k]["value"]), k


@pytest.mark.parametrize("bad", [1.5, -0.1, "0.5", True, float("nan")])
def test_b07i_manual_theta_state_invalid(tmp_path, bad):
    with pytest.raises(cs.ManualError):
        cs.validate_manual(dict(MANUAL_2I, Theta_state=bad))
    rc, o = _run(tmp_path, _journal(), manual=dict(MANUAL_2I, Theta_state=bad))
    assert rc == cs.EXIT_INVALID and not o.exists()


def test_b07i_eps_norm_record_for_phase3(tmp_path):
    doc = cs.calibrate(_write(tmp_path / "j.jsonl", _journal_2e(30)), MANUAL_2I)["doc"]
    e = doc["eps_norm"]
    assert e["per_feature"] == {"lounge_brightness": 0.1, "setting_710": 0.1} and e["source"]["manual"] == MANUAL_2I["ref"]
    assert e["kernel_scalar_equivalent"] == {"lounge_brightness": pytest.approx(0.1), "setting_710": pytest.approx(0.1)}
    assert e["consistent_with_eps_state"] is True and "допуск d_∞" in e["use"] and "\x01" not in e["use"]
    assert doc["params"]["eps_state"]["value"] == pytest.approx(0.05)    # ядро фазы 2: скаляр ε_state не меняется
    off = cs.calibrate(_write(tmp_path / "k.jsonl", _journal_2e(30)), dict(MANUAL_2I, eps_norm={"setting_710": 0.2}))["doc"]
    assert off["eps_norm"]["consistent_with_eps_state"] is False
    assert "eps_norm" not in cs.calibrate(_write(tmp_path / "l.jsonl", _journal()), MANUAL_2E)["doc"]


@pytest.mark.parametrize("bad", [{}, [0.1], {"setting_710": 1.5}, {"setting_710": -0.1}, {"setting_710": "0.1"}, {"": 0.1}])
def test_b07i_eps_norm_invalid(bad):
    with pytest.raises(cs.ManualError):
        cs.validate_manual(dict(MANUAL_2I, eps_norm=bad))


def test_b07i_loo_record_guards():
    assert cs.loo_record([], 2, None)["guard"].startswith("вырождено") and cs.loo_record([], 2, None)["robust_lower"] is None
    r = cs.loo_record([0.9, 0.9, 0.9], 0, 0.95)
    assert r["guard"].startswith("вырождено") and r["spread"] == 0 and r["robust_lower"] == 0.9
    r = cs.loo_record([0.5, 0.9, 0.8], 1, None)
    assert "не робастен" in r["guard"] and r["robust_lower"] == 0.5 and r["excluded"] == 1 and r["median"] == 0.8
    vals = [i / 100 for i in range(100, 0, -1)]                          # 0,01 .. 1,00
    r = cs.loo_record(vals, 0, 1.0)
    assert r["guard"] == "ok" and r["n"] == 100 and r["q05"] == 0.05 and r["q10"] == 0.10 and r["robust_lower"] == 0.05
    assert r["min"] == 0.01 and r["max"] == 1.0 and r["median"] == pytest.approx(0.505)


def test_b07i_real_manual_shape_two_runs_and_scanner(tmp_path):
    for enc in ("utf-8", "utf-8-sig"):
        j = _write(tmp_path / "j.jsonl", _journal_2e(30))
        m = tmp_path / f"m_{enc}.json"
        m.write_text(json.dumps(MANUAL_2I, ensure_ascii=False, indent=2), encoding=enc)
        first = tmp_path / f"f_{enc}.json"
        assert cs.main(["--path", str(j), "--out", str(first), "--manual", str(m)]) == 0
        outs = []
        for k in range(2):
            p = subprocess.run([sys.executable, str(_TOOL), "--path", str(j), "--out", str(first), "--manual", str(m),
                                "--last-seq-from", str(first)], capture_output=True, timeout=120)
            assert p.returncode == 0
            outs.append((p.stdout, first.read_bytes()))
        assert outs[0] == outs[1]                                            # два прогона -> байт-в-байт
        text = outs[1][0].decode("utf-8")
        assert [ln for ln in text.splitlines() if CAL_SCANNER.search(ln)] == []
        assert "Theta_state MANUAL" in text and "consistent with eps_state=True" in text
