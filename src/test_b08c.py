"""test_b08c.py -- этап 3c фазы 3: модель переходов и журнал (остаток SET/STEP), заморозка D при A_t = 1, точные границы порогов, Δt_0.

Этап 3c п.1-4 (решения Оператора 15:36 -- поля журнала; 18:21 -- заморозка D при A_t = 1,
Δt_0 в файл параметров). Каждый пункт реестра проверок -- свой тест на входном векторе (имя содержит id):
  MA10 -- остаток r = x_obs − x_target, SET -> clamp(v_cmd), STEP -> clamp(x' + Δ_cmd);
  MA15 -- наблюдение вне шкалы / не число -> INVALID_OBSERVATION (до расчёта переходов) -> HALT;
  MA08 -- Дирихле α = 1 по переходам с backoff: непройденный переход получает вероятность > 0;
  MA18 -- вариант (б), fail-closed: при A_t = 1 пауза > τ_D не распадает D, тревогу снимают только успехи;
  MA17 -- вектор длины серии через паузу (5 / 6 / 7 / 8 успехов);
  MA19 -- точные границы Θ_reset / Θ_crit (мутации «<»→«≤», «≥»→«>» ловятся);
  DT0  -- Δt_0 переносит calibrate_sigma.py в файл параметров (MANUAL), ядро читает его params_from_doc;
  JOURNAL -- писатель теневого журнала пишет d_cmd (Δ_cmd) и s_raw (сырое значение); старые строки -- NO_DATA.
Векторы синтетические. Боевой журнал logs\\sigma_shadow.jsonl не читается: писатель -- во временный файл,
калибровка -- на синтетическом журнале во временной папке. config/sigma_params.json читается без записи.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import abstraction_layer as al
from abstraction_layer import AbstractionParams, CmdMode, DecisionInputs, DissonanceIntegrator, Label, Outcome

ROOT = Path(__file__).resolve().parents[1]
PARAMS_FILE = ROOT / "config" / "sigma_params.json"


def _load_tool(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, ROOT / "tools" / name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


cs = _load_tool("calibrate_sigma.py", "calibrate_sigma_b08c")
sss = cs.sss

P = AbstractionParams(tau_D=60.0, dt_0=13.0, theta_crit=0.5, theta_reset=0.2)
P_DEC = AbstractionParams(tau_D=60.0, dt_0=13.0, theta_crit=0.5, theta_reset=0.2, theta_state=0.5,
                          theta_intent=0.6, theta_margin=0.1, theta_goal=0.5)
X_OK = DecisionInputs(c_state=1.0, q_best=1.0, q_second=0.0, c_goal=1.0, d_t=0.0,
                      authorization=1, safety_gate=1, budget_remaining=8)
LO, HI = 0, 10            # шкала setting_710 (яркость подсветки, close_2c п.2)
HOUR = 3600.0


def _run(it: DissonanceIntegrator, deltas, dt: float, eid0: int = 0, t0: float = 0.0):
    out, eid, t = [], eid0, t0
    for dl in deltas:
        eid += 1
        t += dt
        r = it.step(eid, t, [float(dl)])
        out.append(r)
    return out, eid, t


def _first(trace, alarm: bool) -> int:
    return next(i + 1 for i, r in enumerate(trace) if r.alarm is alarm)


# ======================================================================= остаток SET/STEP: остаток SET / STEP + clamp
def test_b08c_MA10_step_clamped_at_top_and_bottom():
    r = al.transition_residual(CmdMode.STEP, 10, LO, HI, x_prev=9, d_cmd=3)      # «плюс 3» от 9: ТВ встаёт на 10
    assert (r.target, r.residual, r.observed) == (10.0, 0.0, 10.0)                 # без clamp было бы r = −2 (ложный сбой)
    r = al.transition_residual(CmdMode.STEP, 0, LO, HI, x_prev=1, d_cmd=-6)
    assert (r.target, r.residual) == (0.0, 0.0)
    r = al.transition_residual(CmdMode.STEP, 6, LO, HI, x_prev=4, d_cmd=2)
    assert (r.target, r.residual) == (6.0, 0.0)
    r = al.transition_residual(CmdMode.STEP, 5, LO, HI, x_prev=4, d_cmd=2)        # недоход на 1 деление
    assert (r.target, r.residual) == (6.0, -1.0)


def test_b08c_MA10_set_clamped_and_independent_of_x_prev():
    assert al.transition_target(CmdMode.SET, LO, HI, v_cmd=15) == 10.0
    assert al.transition_target(CmdMode.SET, LO, HI, v_cmd=-3) == 0.0
    assert al.transition_target(CmdMode.SET, LO, HI, v_cmd=7) == 7.0
    assert al.transition_target(CmdMode.SET, LO, HI, x_prev=2, v_cmd=7) == 7.0
    assert al.transition_target(CmdMode.SET, 0, 255, v_cmd=300) == 255.0           # яркость цвета 0..255
    assert al.transition_residual(CmdMode.SET, 7, LO, HI, v_cmd=7).residual == 0.0


def test_b08c_MA10_step_size_separates_rows():
    """остаток SET/STEP: «яркость +1» и «+6» из одного x' были одной строкой T_θ(·|x', amb_step); теперь цель зависит от Δ_cmd."""
    t1 = al.transition_target(CmdMode.STEP, LO, HI, x_prev=4, d_cmd=1)
    t6 = al.transition_target(CmdMode.STEP, LO, HI, x_prev=4, d_cmd=6)
    assert (t1, t6) == (5.0, 10.0)
    m1, _ = al.transition_matrix_dirichlet(range(11), CmdMode.STEP, LO, HI, {}, {0: 33}, d_cmd=1)
    m6, _ = al.transition_matrix_dirichlet(range(11), CmdMode.STEP, LO, HI, {}, {0: 33}, d_cmd=6)
    assert max(range(11), key=lambda y: m1[4][y]) == 5 and max(range(11), key=lambda y: m6[4][y]) == 10


def test_b08c_MA10_bad_inputs():
    with pytest.raises(ValueError):
        al.transition_target(CmdMode.SET, LO, HI)                                   # SET без v_cmd
    with pytest.raises(ValueError):
        al.transition_target(CmdMode.STEP, LO, HI, x_prev=4)                        # STEP без Δ_cmd
    with pytest.raises(ValueError):
        al.transition_target(CmdMode.STEP, LO, HI, d_cmd=1)                         # STEP без x'
    with pytest.raises(ValueError):
        al.transition_target("STEP", LO, HI, x_prev=4, d_cmd=1)                     # режим -- только CmdMode
    for v in (math.nan, math.inf, True, "3"):
        with pytest.raises(ValueError):
            al.transition_target(CmdMode.SET, LO, HI, v_cmd=v)
    for lo, hi in ((10, 0), (5, 5), (0, math.inf), (math.nan, 10), (True, 10)):
        with pytest.raises(al.InvalidConfigError):
            al.clamp(3, lo, hi)


# ======================================================================= наблюдение вне шкалы: INVALID_OBSERVATION
@pytest.mark.parametrize("x", [12, -1, 255, 10.5, -0.001, math.nan, math.inf, -math.inf, True, "7", None],
                         ids=repr)
def test_b08c_MA15_out_of_scale_or_not_number_invalid_observation(x):
    with pytest.raises(al.InvalidObservationError) as e:
        al.check_observation(x, LO, HI)
    assert e.value.label is Label.INVALID_OBSERVATION and isinstance(e.value, ValueError)   # контракт 1d
    with pytest.raises(al.InvalidObservationError):
        al.transition_residual(CmdMode.SET, x, LO, HI, v_cmd=5)                    # до расчёта перехода
    d = al.guarded_decide(P_DEC, lambda: replace(X_OK, c_state=0.0 * al.check_observation(x, LO, HI)))
    assert d.outcome is Outcome.HALT and d.reasons == ("INVALID_OBSERVATION",)


def test_b08c_MA15_scale_edges_valid_not_clipped():
    assert al.check_observation(0, LO, HI) == 0.0 and al.check_observation(10, LO, HI) == 10.0
    assert al.check_observation(9.6, LO, HI) == 9.6                                # не округляется
    with pytest.raises(al.InvalidObservationError):                                # x' вне шкалы -- тоже наблюдение
        al.transition_target(CmdMode.STEP, LO, HI, x_prev=12, d_cmd=-1)
    with pytest.raises(al.InvalidObservationError):                                # состояния матрицы -- в шкале
        al.transition_matrix_dirichlet([0, 5, 11], CmdMode.SET, LO, HI, {}, {}, v_cmd=5)


# ======================================================================= Дирихле α = 1 с backoff
def test_b08c_MA08_unseen_row_gets_backoff_not_only_label():
    states = list(range(11))
    m, labels = al.transition_matrix_dirichlet(states, CmdMode.STEP, LO, HI, {}, {0: 33}, d_cmd=1)
    row = m[4]
    assert labels[4] is Label.UNSEEN_TRANSITION and all(lb is Label.UNSEEN_TRANSITION for lb in labels)
    assert math.fsum(row) == pytest.approx(1.0, abs=1e-12) and min(row) > 0
    assert row[5] == pytest.approx((33 + 1 / 11) / 34, abs=1e-12)                 # пик на x_target
    assert row[7] == pytest.approx((1 / 11) / 34, abs=1e-12)                      # любой переход > 0


def test_b08c_MA08_seen_row_formula_and_no_data_uniform():
    g = al.residual_backoff(range(11), 5.0, {0: 33})
    r = al.dirichlet_row([0, 0, 0, 0, 0, 3, 0, 0, 0, 0, 0], g)
    assert r.label is None
    assert r.probs[5] == pytest.approx((3 + g[5]) / 4, abs=1e-12)
    assert r.probs[6] == pytest.approx(g[6] / 4, abs=1e-12) and r.probs[6] > 0
    u = al.residual_backoff(range(11), 5.0, {})                                    # остатков нет -> равномерный
    assert u == pytest.approx(tuple([1 / 11] * 11), abs=1e-15)
    assert al.dirichlet_row([0] * 11, u).probs == pytest.approx(u, abs=1e-15)
    gm = al.residual_backoff(range(11), 5.0, {0: 30, -1: 2, 1: 1})                 # остатки ±1 -- соседи цели
    assert gm[4] > gm[6] > gm[7] and gm[5] == max(gm)


def test_b08c_MA08_unseen_transition_keeps_belief_no_model_mismatch():
    """Вектор Дирихле α = 1 (2e: переходы, которых не было в 2d): по сырым частотам T(6|4) = 0 -> наблюдение 6 даёт
    Z·b⁻ = 0 -> MODEL_MISMATCH; с Дирихле -- вера обновляется, метки нет."""
    states = list(range(11))
    b0 = [0.0] * 11
    b0[4] = 1.0
    z = [0.0] * 11
    z[6] = 1.0
    raw = [[1.0 if y == min(10, x + 1) else 0.0 for y in states] for x in states]  # частоты: только x + 1
    assert al.update_belief(al.predict_belief(b0, raw, [1 / 11] * 11, 0.0, 600.0), z).label is Label.MODEL_MISMATCH
    counts = {x: [3.0 if y == min(10, x + 1) else 0.0 for y in states] for x in states}
    m, labels = al.transition_matrix_dirichlet(states, CmdMode.STEP, LO, HI, counts, {0: 33}, d_cmd=1)
    assert all(lb is None for lb in labels)
    up = al.update_belief(al.predict_belief(b0, m, [1 / 11] * 11, 0.0, 600.0), z)
    assert up.label is None and up.outcome is None and up.belief[6] == pytest.approx(1.0, abs=1e-12)


def test_b08c_MA08_invalid_alpha_and_backoff():
    for a in (0.0, -1.0, math.nan, math.inf, True):
        with pytest.raises(al.InvalidConfigError):
            al.dirichlet_row([1, 0], [0.5, 0.5], alpha=a)
    for g in ([0.0, 0.0], [math.nan, 1.0], [-0.1, 1.1], []):
        with pytest.raises(al.InvalidDistributionError):
            al.dirichlet_row([1, 0][:len(g)] if g else [], g)
    with pytest.raises(ValueError):
        al.dirichlet_row([1, 0, 0], [0.5, 0.5])
    with pytest.raises(al.InvalidConfigError):
        al.residual_backoff([1, 1, 2], 1.0, {})
    with pytest.raises(ValueError):
        al.residual_backoff([0, 1], 0.0, {0: -1})


# ======================================================================= заморозка D при A_t = 1: тревога переживает паузу
def test_b08c_MA18_failure_after_pause_keeps_alarm():
    it = DissonanceIntegrator(P)
    tr, eid, t = _run(it, [1, 1, 1, 1], 6.0)
    assert _first(tr, True) == 4 and it.alarm
    d4 = it.d
    r = it.step(eid + 1, t + HOUR, [1.0])                       # 4 сбоя -> тревога -> пауза 3600 с -> 1 сбой
    assert r.session_break and r.frozen and r.alarm is True
    lam = al.lambda_0(P)
    assert r.d == pytest.approx(lam * d4 + (1 - lam), abs=1e-15) and r.d == pytest.approx(0.6616, abs=1e-4)
    # 3b давало: D·e^(−60) + 0,1948 = 0,1948 < 0,20 -> тревога снималась сбоем (C1 )


def test_b08c_MA18_reset_after_pause_on_fifth_success_not_earlier():
    it = DissonanceIntegrator(P)
    _, eid, t = _run(it, [1, 1, 1, 1], 6.0)
    first = it.step(eid + 1, t + HOUR, [0.0])                   # пауза -> 1-й успех
    assert first.session_break and first.frozen and first.alarm
    rest, _, _ = _run(it, [0, 0, 0, 0, 0], 6.0, eid + 1, t + HOUR)
    tr = [first] + rest
    assert [r.alarm for r in tr] == [True, True, True, True, False, False]   # снята на 5-м, не раньше
    assert tr[3].d >= 0.2 > tr[4].d


def test_b08c_MA18_silence_projection_and_restore_do_not_reset():
    it = DissonanceIntegrator(P)
    _, eid, t = _run(it, [1, 1, 1, 1], 6.0)
    for pause in (61.0, HOUR, 7 * 24 * HOUR):
        assert it.project(t + pause) == (it.d, True)            # пауза без событий тревогу не снимает
        d = al.decide(P_DEC, it.decision_inputs(X_OK, now_ts=t + pause))
        assert d.outcome is Outcome.OBSERVE and "DISSONANCE" in d.reasons
    st = json.loads(json.dumps(it.to_state()))                  # перезапуск службы после паузы
    it2 = DissonanceIntegrator.from_state(P, st)
    assert it2.alarm and it2.project(t + HOUR) == (it.d, True)
    r = it2.step(eid + 1, t + HOUR, [0.0])
    assert r.frozen and r.alarm and r.d == pytest.approx(al.lambda_0(P) * it.d, abs=1e-15)


def test_b08c_MA18_no_alarm_session_boundary_as_in_3b():
    """A_t = 0: граница сессии -- как в 3b (распад D по паузе): D ← D·e^(−Δt/τ_D); одиночный сбой после часа -> 0,195."""
    it = DissonanceIntegrator(P)
    _, eid, t = _run(it, [1, 1, 1], 6.0)                        # D 0,478 < Θ_crit -- тревоги нет
    assert not it.alarm and it.project(t + HOUR)[1] is False
    assert it.project(t + HOUR)[0] == pytest.approx(it.d * math.exp(-60.0), abs=1e-30)
    r = it.step(eid + 1, t + HOUR, [1.0])
    assert r.session_break and not r.frozen and not r.alarm
    assert r.d == pytest.approx(1 - al.lambda_0(P), abs=1e-9)


@pytest.mark.parametrize("n_fail,n_reset", [(4, 5), (5, 6), (8, 7), (12, 8)])
def test_b08c_MA17_vector_through_pause(n_fail, n_reset):
    it = DissonanceIntegrator(P)
    tr, eid, t = _run(it, [1] * n_fail, 6.0)
    assert _first(tr, True) == 4
    succ, _, _ = _run(it, [0] * 12, HOUR + 1.0, eid, t)         # КАЖДЫЙ успех -- после паузы > τ_D
    assert all(r.session_break for r in succ)
    assert _first(succ, False) == n_reset                       # как без пауз (длина серии): 5 / 6 / 7 / 8


# ======================================================================= точные границы порогов
def test_b08c_MA19_exit_requires_strictly_below_theta_reset():
    """Θ_reset задан равным D после 5-го успеха (то же float-значение): D == Θ_reset -> тревога ДЕРЖИТСЯ (строго «<»);
    мутация «<» -> «≤» снимала бы на 5-м."""
    lam = al.lambda_0(P)
    d = 0.0
    for _ in range(4):
        d = al.dissonance_step_l0(d, 1, lam)
    for _ in range(5):
        d = al.dissonance_step_l0(d, 0, lam)
    pb = replace(P, theta_reset=d)
    it = DissonanceIntegrator(pb)
    _, eid, t = _run(it, [1, 1, 1, 1], 6.0)
    succ, _, _ = _run(it, [0] * 6, 6.0, eid, t)
    assert succ[4].d == pb.theta_reset and succ[4].alarm is True
    assert succ[5].alarm is False
    ok = DissonanceIntegrator.from_state(pb, dict(it.to_state(), d=pb.theta_reset, alarm=True, k_high=1))
    assert ok.alarm                                             # D == Θ_reset при A_t = 1 -- согласованное состояние
    with pytest.raises(al.InvalidStateError):
        DissonanceIntegrator.from_state(pb, dict(it.to_state(), d=math.nextafter(pb.theta_reset, 0.0),
                                                 alarm=True, k_high=1))


def test_b08c_MA19_entry_at_exactly_theta_crit():
    """Θ_crit задан равным D после 4-го сбоя: D == Θ_crit -> тревога ВКЛЮЧАЕТСЯ (≥); мутация «≥» -> «>» -- нет."""
    lam = al.lambda_0(P)
    d = 0.0
    for _ in range(4):
        d = al.dissonance_step_l0(d, 1, lam)
    pc = replace(P, theta_crit=d)
    it = DissonanceIntegrator(pc)
    tr, _, _ = _run(it, [1, 1, 1, 1], 6.0)
    assert tr[3].d == pc.theta_crit and [r.alarm for r in tr] == [False, False, False, True]
    assert al.decide(replace(P_DEC, theta_crit=d), replace(X_OK, d_t=d)).outcome is Outcome.OBSERVE


def test_b08c_MA19_file_thresholds_boundary_vector():
    """Те же границы на ручных Θ файла параметров 0,50 / 0,20 (12:04): сброс на 5-м, D_5 < 0,20 ≤ D_4."""
    it = DissonanceIntegrator(P)
    _, eid, t = _run(it, [1, 1, 1, 1], 13.0)
    succ, _, _ = _run(it, [0] * 5, 13.0, eid, t)
    assert succ[3].d >= 0.2 > succ[4].d and [r.alarm for r in succ] == [True] * 4 + [False]


# ======================================================================= Δt_0: calibrate_sigma -> файл -> ядро
SEQ0 = 7_000_000_000
T0 = 4_000_000_000


def _journal_rows():
    """Синтетический журнал 2d: 20 команд amb_step / setting_710, PASS (форма как в test_calibrate_sigma)."""
    rows, val = [], 4
    for i in range(20):
        seq, t = SEQ0 + i * 30_000, T0 + i * 30_000
        nxt = 6 if val == 4 else 4
        base = {"seq": seq, "cmd_kind": "verified", "action": "amb_step"}
        rows.append({**base, "step": "readback", "feature": "setting_710", "s_obs": val, "latency_ms": 5,
                     "t" "s": t})
        rows.append({**base, "step": "send", "feature": "setting_710", "s_exp": nxt, "t" "s": t + 100})
        rows.append({**base, "step": "outcome", "outcome": "PASS", "feature": "setting_710", "s_exp": nxt,
                     "s_obs": nxt, "latency_ms": 900, "t" "s": t + 1000})
        val = nxt
    return rows


def _manual(**extra):
    m = {
        "ref": "решение тест B08c", "tau_D": 60, "Theta_crit": 0.5, "Theta_reset": 0.2,
        "R_t": {"N_obs": 3, "N_route": 3, "N_reflect": 2}, "eps_state": {"setting_710": 1}, "tau_b": 600,
        "delta_rule": "any_fail_1", "Theta_state": 0.5, "eps_norm": {"setting_710": 0.1}}
    m.update(extra)
    return m


@pytest.fixture
def journal(tmp_path):
    p = tmp_path / "j" / "sigma_shadow.jsonl"
    p.parent.mkdir()
    p.write_text("".join(json.dumps(r) + "\n" for r in _journal_rows()), encoding="utf-8")
    return p


def test_b08c_DT0_calibrate_transfers_dt0_manual(journal, tmp_path):
    mf = tmp_path / "manual.json"
    mf.write_text(json.dumps(_manual(dt_0=13), ensure_ascii=False), encoding="utf-8")
    outs = []
    for k in (1, 2):
        out = tmp_path / f"p{k}.json"
        assert cs.main(["--path", str(journal), "--manual", str(mf), "--out", str(out)]) == cs.EXIT_OK
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]                                   # два прогона байт-в-байт
    doc = json.loads(outs[0])
    assert doc["dt_0"]["status"] == "MANUAL" and doc["dt_0"]["value"] == 13.0
    assert doc["dt_0"]["source"]["manual"] == "решение тест B08c" and "18:21" in doc["dt_0"]["source"]["decision"]
    assert set(doc["params"]) == set(cs.PARAMS) and len(doc["params"]) == 20     # раскладка 20 параметров прежняя
    plain = cs.calibrate(journal, _manual())["doc"]             # без dt_0 -- раздела нет, остальное то же
    assert "dt_0" not in plain and {k: v for k, v in doc.items() if k != "dt_0"} == plain


@pytest.mark.parametrize("bad", [0, -13, "13", True, math.inf], ids=repr)
def test_b08c_DT0_invalid_manual_dt0_exit_2(journal, tmp_path, bad):
    mf = tmp_path / "manual.json"
    mf.write_text(json.dumps(_manual(dt_0=bad)), encoding="utf-8")
    out = tmp_path / "p.json"
    assert cs.main(["--path", str(journal), "--manual", str(mf), "--out", str(out)]) == cs.EXIT_INVALID
    assert not out.exists()


def test_b08c_DT0_kernel_reads_dt0_from_params_doc(journal):
    doc = cs.calibrate(journal, _manual(dt_0=13))["doc"]
    p = al.params_from_doc(json.loads(json.dumps(doc)))
    assert p.dt_0 == 13.0 and p.tau_D == 60.0 and p.theta_crit == 0.5 and p.theta_reset == 0.2
    assert al.lambda_0(p) == pytest.approx(math.exp(-13.0 / 60.0), abs=1e-15)
    assert p.theta_intent is None and p.theta_margin is None and p.theta_goal is None   # NO_DATA -> None
    it = DissonanceIntegrator(p)
    tr, eid, t = _run(it, [1, 1, 1, 1], 6.0)
    assert _first(tr, True) == 4
    p0 = al.params_from_doc({k: v for k, v in doc.items() if k != "dt_0"})              # нет раздела -> NO_DATA
    assert p0.dt_0 is None
    d = al.guarded_decide(P_DEC, lambda: DissonanceIntegrator(p0).decision_inputs(X_OK))
    assert d.outcome is Outcome.HALT and d.reasons == ("NO_DATA:kernel",)
    for bad in ({"status": "MANUAL", "value": 0}, {"status": "MANUAL", "value": -1}):
        with pytest.raises(al.InvalidConfigError):
            al.params_from_doc(dict(doc, dt_0=bad))
    assert al.params_from_doc(dict(doc, dt_0={"status": "MANUAL_PENDING", "value": None})).dt_0 is None
    for bad_doc in (None, [], {}, {"params": []}):
        with pytest.raises(al.InvalidConfigError):
            al.params_from_doc(bad_doc)


def test_b08c_DT0_real_params_file_has_dt0_manual():
    """Файл config/sigma_params.json после пересчёта блоком раздел dt_0 = 13 с MANUAL; ядро читает его."""
    doc = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
    assert doc["dt_0"]["status"] == "MANUAL" and doc["dt_0"]["value"] == 13.0
    p = al.params_from_doc(doc)
    assert p.dt_0 == 13.0 and p.tau_D == 60.0 and p.theta_crit == 0.5 and p.theta_reset == 0.2
    it = DissonanceIntegrator(p)
    tr, eid, t = _run(it, [1, 1, 1, 1], 6.0)
    succ, _, _ = _run(it, [0] * 6, 6.0, eid, t)
    assert _first(tr, True) == 4 and _first(succ, False) == 5


# ======================================================================= журнал: d_cmd и s_raw (синтетика)
import tv_bot_handler as H                     # noqa: E402
import tv_intent                               # noqa: E402
from tv_control import TVClient                # noqa: E402

ADMIN = 4242


class WireTV(TVClient):
    """Настоящий клиент API v6, транспорт в памяти (как test_b07d). value -- сырое значение узла 710 как есть."""

    def __init__(self, start=4, accept: bool = True) -> None:
        super().__init__(host="192.0.2.10", username="u", password="p", session=object())
        self.wire: list = []
        self.accept = accept
        self.state = {710: {"value": start}, 720: {"value": 0}}

    def _call(self, method, path, payload=None, want_json=None):
        self.wire.append((method, path, json.dumps(payload, sort_keys=True)))
        if path == "menuitems/settings/current":
            return {"values": [{"value": {"Nodeid": n["nodeid"], "Controllable": True, "Available": True,
                                          "data": dict(self.state[n["nodeid"]])}} for n in payload["nodes"]
                               if n["nodeid"] in self.state]}
        if path == "ambilight/currentconfiguration" and method == "GET":   # B20: режим «за картинкой» -> ползунок 710
            return {"styleName": "FOLLOW_VIDEO", "menuSetting": "STANDARD"}
        if path == "menuitems/settings/update":
            v = payload["values"][0]["value"]
            if self.accept:
                self.state[v["Nodeid"]] = dict(v["data"])
            return {}
        return {}


@pytest.fixture
def jw(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "SHADOW_DEFAULT_PATH", tmp_path / "default" / "sigma_shadow.jsonl")
    path = tmp_path / "jw" / "sigma_shadow.jsonl"
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(path))
    monkeypatch.delenv(H.SHADOW_MAX_ENV, raising=False)
    monkeypatch.delenv("OKO_COLLECTOR", raising=False)
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")
    monkeypatch.setenv("TV_SETTING_SETTLE_S", "0")
    monkeypatch.setattr(tv_intent, "understand", lambda *a, **k: None)
    monkeypatch.setattr(H, "VERIFY", {})
    monkeypatch.setattr(H, "PRE", {})
    monkeypatch.setattr(H, "_stop_music", lambda name: "")
    H._SHADOW_WARNED.clear()
    return path


def _play(monkeypatch, flag, script, make=WireTV):
    if flag is None:
        monkeypatch.delenv(H.SHADOW_FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(H.SHADOW_FLAG_ENV, flag)
    tvs = []

    def factory():
        tv = make()
        tvs.append(tv)
        return tv
    store = H.PendingStore()
    answers = [H.handle(ADMIN, t, factory, pending=store, admin_ids=[ADMIN], now=float(i))
               for i, t in enumerate(script)]
    return tvs, answers


def _rows(path: Path) -> list:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def _outs(path: Path) -> list:
    return [r for r in _rows(path) if r["step"] == "outcome"]


def test_b08c_JOURNAL_d_cmd_signed_from_command(monkeypatch, jw):
    script = ["подсветка яркость плюс 2", "подсветка тусклее", "подсветка ярче", "подсветка яркость 7"]
    tvs, _ = _play(monkeypatch, "1", script)
    outs = _outs(jw)
    assert [o["action"] for o in outs] == ["amb_step", "amb_dimmer", "amb_brighter", "amb_brightness_set"]
    assert [o.get("d_cmd") for o in outs] == [2, -1, 1, None]           # SET -- без Δ_cmd (NO_DATA)
    for o, tv in zip(outs, tvs):
        assert o["feature"] == "setting_710" and o["outcome"] == "PASS"
        assert o["s_raw"] == o["s_obs"] == tv.state[710]["value"]       # то же чтение после записи
    for r in _rows(jw):
        assert set(r) <= H.SHADOW_FIELDS and H.shadow_validate(r) == r


def test_b08c_JOURNAL_s_raw_before_normalization(monkeypatch, jw):
    """Сырое значение ТВ 9,6 (до int() клиента): s_obs 9, s_raw 9,6 в строке readback до записи; «12» строкой -- вне шкалы."""
    _play(monkeypatch, "1", ["подсветка ярче"], make=lambda: WireTV(start=9.6))
    rb = [r for r in _rows(jw) if r["step"] == "readback" and r.get("feature") == "setting_710"]   # B20: до него -- чтение режима
    assert rb[0]["s_obs"] == 9 and rb[0]["s_raw"] == 9.6
    (o,) = _outs(jw)
    assert o["s_raw"] == o["s_obs"] == 10 and o["d_cmd"] == 1
    jw.unlink()
    _play(monkeypatch, "1", ["подсветка яркость"], make=lambda: WireTV(start="12"))   # только чтение
    (o,) = _outs(jw)
    assert o["action"] == "amb_brightness_get" and o["s_raw"] == 12 and "d_cmd" not in o
    with pytest.raises(al.InvalidObservationError):                     # наблюдение вне шкалы: ядро помечает выброс
        al.check_observation(o["s_raw"], LO, HI)


def test_b08c_JOURNAL_rows_feed_residual_model(monkeypatch, jw):
    """Журнал -> модель остаток SET/STEP: «яркость 9» (SET), «плюс 3» (STEP, ТВ упирается в 10): r = 0 с clamp."""
    _play(monkeypatch, "1", ["подсветка яркость 9", "подсветка яркость плюс 3"],
          make=lambda: WireTV(start=4))
    o1, o2 = _outs(jw)
    assert "d_cmd" not in o1 and o2["d_cmd"] == 3
    r1 = al.transition_residual(CmdMode.SET, o1["s_raw"], LO, HI, v_cmd=o1["s_exp"])
    assert r1.residual == 0.0
    # второй ТВ-клиент стартует с 4 (двойник), поэтому x' берём из строки readback до записи той же команды
    rb2 = [r for r in _rows(jw) if r["step"] == "readback" and r["seq"] == o2["seq"] and r.get("feature") == "setting_710"][0]
    r2 = al.transition_residual(CmdMode.STEP, o2["s_raw"], LO, HI, x_prev=rb2["s_raw"], d_cmd=o2["d_cmd"])
    assert (r2.target, r2.residual) == (7.0, 0.0)


def test_b08c_JOURNAL_same_wire_and_answers_on_off_hooks_removed(monkeypatch, jw):
    script = ["подсветка яркость плюс 2", "подсветка яркость", "подсветка ярче", "подсветка яркость 7"]
    tv_off, ans_off = _play(monkeypatch, None, script)
    assert not jw.exists()
    tv_on, ans_on = _play(monkeypatch, "1", script)
    assert [t.wire for t in tv_on] == [t.wire for t in tv_off] and ans_on == ans_off
    assert all("_setting" not in vars(t) and "_write_setting" not in vars(t) for t in tv_on)


def test_b08c_JOURNAL_whitelist_rejects_bad_new_fields():
    base = {"step": "outcome", "t" "s": 5}
    assert H.shadow_validate(dict(base, d_cmd=-3, s_raw=9.6)) == dict(base, d_cmd=-3, s_raw=9.6)
    for bad in ({"d_cmd": True}, {"d_cmd": 1.5}, {"d_cmd": "2"}, {"s_raw": "9"}, {"s_raw": True},
                {"s_raw": math.nan}, {"s_raw": math.inf}, {"raw": 1}):
        assert H.shadow_validate(dict(base, **bad)) is None
    assert H._raw_num("12") == 12 and H._raw_num(" 9.5 ") == 9.5 and H._raw_num("x") is None
    assert H._raw_num(True) is None and H._raw_num(math.nan) is None


def test_b08c_JOURNAL_old_rows_no_data_tools_unchanged(monkeypatch, jw, tmp_path):
    """Старые строки (без d_cmd / s_raw) и новые -- в одном журнале: битых строк 0, прежние счётчики те же."""
    old = tmp_path / "old.jsonl"
    old.write_text("".join(json.dumps(r) + "\n" for r in _journal_rows()), encoding="utf-8")
    st_old = sss.compute(old, sss.ISOLATE_DEFAULT)
    assert st_old["lines_bad"] == 0 and st_old["fields_3c"] == {"outcome_rows": 20, "with_d_cmd": 0, "with_s_raw": 0}
    _play(monkeypatch, "1", ["подсветка яркость плюс 2", "подсветка яркость 7"])
    mixed = tmp_path / "mixed.jsonl"
    mixed.write_bytes(old.read_bytes() + jw.read_bytes())
    st = sss.compute(mixed, sss.ISOLATE_DEFAULT)
    assert st["lines_bad"] == 0 and st["lines_ok"] == st["lines_total"]
    assert st["fields_3c"] == {"outcome_rows": 22, "with_d_cmd": 1, "with_s_raw": 2}
    rows, stl, _ = cs.load_rows(mixed, sss.ISOLATE_DEFAULT)              # калибровка читает смешанный журнал
    assert stl["lines_bad"] == 0
    assert "fields 3c" in sss.render(st)
