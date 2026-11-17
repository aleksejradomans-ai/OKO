"""test_b08b.py -- этап 3b фазы 3: ядро Σ по реестру проверок.

Этап 3b (п.1 -- решение Оператора 16.11 16:16, интегратор λ_0).
Каждый пункт реестра -- свой тест на входном векторе: защёлка A_t, δ по любому сбою, распад D по паузе, допуск d_∞, темп Δt_0, длина серии,
R-02, R-05, R-06, R-07, R-08, R-09 (+ UNSEEN_TRANSITION, INVALID_CONFIG, сквозной decide).
Векторы синтетические; журнал logs\\sigma_shadow.jsonl не читается. Файл параметров config/sigma_params.json
читается только для τ_D и Θ_crit / Θ_reset (ручные значения 12:04), без записи.
(этап 3c): два теста изменены по заморозка D при A_t = 1, решение Оператора 18:21 -- R06_restart_after_pause_projection
и C1_pinned_alarm_exit_on_failure_after_long_pause (тревога переживает паузу; см. docstring тестов).
"""
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

import abstraction_layer as al
from abstraction_layer import (AbstractionParams, DecisionInputs, DissonanceIntegrator, Label, NoDataError, Outcome)

PARAMS_FILE = Path(__file__).resolve().parents[1] / "config" / "sigma_params.json"
DT0_DECISION = 13.0          # Δt_0 решения 16:16 (в файле параметров поля нет -- передаётся в AbstractionParams)
Q = 1.0 - math.exp(-13.0 / 60.0)


def _file_doc() -> dict:
    return json.loads(PARAMS_FILE.read_text(encoding="utf-8"))


def _file_value(doc: dict, key: str):
    v = doc["params"][key]["value"]
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _p_file() -> AbstractionParams:
    """Параметры ядра из файла (как kernel_params check_sigma_2e) + Δt_0 решения 16:16."""
    d = _file_doc()
    return AbstractionParams(tau_b=_file_value(d, "tau_b"), tau_D=_file_value(d, "tau_D"),
                             eps_state=_file_value(d, "eps_state"), theta_state=_file_value(d, "Theta_state"),
                             theta_intent=_file_value(d, "Theta_intent"), theta_margin=_file_value(d, "Theta_margin"),
                             theta_goal=_file_value(d, "Theta_goal"), theta_crit=_file_value(d, "Theta_crit"),
                             theta_reset=_file_value(d, "Theta_reset"), dt_0=DT0_DECISION)


P = AbstractionParams(tau_D=60.0, dt_0=DT0_DECISION, theta_crit=0.5, theta_reset=0.2)
# полный набор порогов только для тестов decide (не калибровка): намерение задано, чтобы проверить ветки
P_DEC = AbstractionParams(tau_D=60.0, dt_0=DT0_DECISION, theta_crit=0.5, theta_reset=0.2, theta_state=0.5,
                          theta_intent=0.6, theta_margin=0.1, theta_goal=0.5)
X_OK = DecisionInputs(c_state=1.0, q_best=1.0, q_second=0.0, c_goal=1.0, d_t=0.0,
                      authorization=1, safety_gate=1, budget_remaining=8)


def _run(it: DissonanceIntegrator, deltas, dt: float, eid0: int = 0, t0: float = 0.0):
    """Серия событий с постоянным Δt; -> (список (D, alarm), последний id, последний ts)."""
    out, eid, t = [], eid0, t0
    for dl in deltas:
        eid += 1
        t += dt
        r = it.step(eid, t, [float(dl)])
        out.append((r.d, r.alarm))
    return out, eid, t


def _alarm_at(trace) -> int:
    return next(i + 1 for i, (_, a) in enumerate(trace) if a)


def _reset_at(trace) -> int:
    return next(i + 1 for i, (_, a) in enumerate(trace) if not a)


# ======================================================================= параметры и λ_0 (п.1)
def test_b08b_params_file_manual_values():
    p = _p_file()
    assert p.tau_D == 60.0 and p.theta_crit == 0.5 and p.theta_reset == 0.2
    assert p.theta_intent is None and p.theta_margin is None and p.theta_goal is None   # намерение NO_DATA


def test_b08b_lambda0_from_params_not_hardcoded():
    assert al.lambda_0(_p_file()) == pytest.approx(math.exp(-13.0 / 60.0), abs=1e-15)
    assert al.lambda_0(_p_file()) == pytest.approx(0.8052, abs=5e-5)
    assert al.lambda_0(replace(P, dt_0=6.0)) == pytest.approx(math.exp(-0.1), abs=1e-15)
    assert al.lambda_0(replace(P, tau_D=30.0)) == pytest.approx(math.exp(-13.0 / 30.0), abs=1e-15)
    with pytest.raises(NoDataError):
        DissonanceIntegrator(replace(P, dt_0=None))
    with pytest.raises(NoDataError):
        DissonanceIntegrator(replace(P, tau_D=None))
    with pytest.raises(ValueError):
        AbstractionParams(dt_0=0.0)
    assert AbstractionParams().dt_0 is None and "dt_0" in AbstractionParams().missing()


def test_b08b_step_formula_and_saturation():
    lam = al.lambda_0(P)
    assert al.dissonance_step_l0(0.3, 1, lam) == pytest.approx(lam * 0.3 + (1 - lam), abs=1e-15)
    assert al.dissonance_step_l0(0.3, 0, lam) == pytest.approx(lam * 0.3, abs=1e-15)
    assert al.dissonance_step_l0(1.0, 1, lam) == 1.0
    for bad in ((1.2, 1), (-0.1, 0), (math.nan, 1), (0.5, 2), (0.5, 0.5), (0.5, True)):
        with pytest.raises(ValueError):
            al.dissonance_step_l0(bad[0], bad[1], lam)
    for bad_lam in (0.0, 1.0, math.nan):
        with pytest.raises(ValueError):
            al.dissonance_step_l0(0.1, 1, bad_lam)


# ======================================================================= δ по любому сбою: δ = 1[∃k e_k > 0]
def test_b08b_MA06_delta_any_fail_reaches_theta_crit():
    assert al.delta_any_fail((0, 0, 0, 0, 1, 0)) == 1
    assert al.delta_any_fail((0, 0.3, 0)) == 1
    assert al.delta_any_fail((0, 0, 0)) == 0
    for bad in ((), (-1.0,), (math.nan,), (math.inf,)):
        with pytest.raises(ValueError):
            al.delta_any_fail(bad)
    it = DissonanceIntegrator(P)
    eid, t, ds = 0, 0.0, []
    for _ in range(4):                       # вектор δ по любому сбою: сбой одного типа из шести, шаг 13 с
        eid += 1
        t += 13.0
        r = it.step(eid, t, (0, 0, 0, 0, 1, 0))
        ds.append(r.d)
    assert ds == pytest.approx([0.1948, 0.3517, 0.4780, 0.5796], abs=1e-4)
    assert it.alarm and _alarm_at([(d, d >= 0.5) for d in ds]) == 4   # было: β = 1/6 -> 0,1667 навсегда


# ======================================================================= распад D по паузе: пауза ≫ τ_D
def test_b08b_MA07_hour_of_silence_one_failure_no_alarm():
    it = DissonanceIntegrator(P)
    it.step(1, 0.0, [0.0])                    # последнее событие, потом час тишины
    r = it.step(2, 3600.0, [1.0])
    assert r.session_break and r.d == pytest.approx(0.195, abs=5e-4) and r.d == pytest.approx(Q, abs=1e-12)
    assert not r.alarm                         # было (форма фазы 2): D ≈ 1 -> тревога
    old = al.dissonance_step(0.0, [1.0], [1.0], dt=3600.0, tau_D=60.0)
    assert old > 0.999                          # прежняя форма сохранена как форма фазы 2 (не путь решения)
    it2 = DissonanceIntegrator(P)              # то же после серии из 3 сбоев: прошлое почти забыто
    _run(it2, [1, 1, 1], 6.0)
    r2 = it2.step(100, it2.last_ts + 3600.0, [1.0])
    assert r2.session_break and r2.d == pytest.approx(Q, abs=1e-9) and not r2.alarm


def test_b08b_MA07_session_boundary_strict():
    it = DissonanceIntegrator(P)
    _run(it, [1, 1], 5.0)
    d2 = it.d
    r = it.step(3, it.last_ts + 60.0, [0.0])   # ровно τ_D -- та же сессия: реальное время в D не входит
    assert not r.session_break and r.d == pytest.approx(al.lambda_0(P) * d2, abs=1e-15)
    d3 = it.d
    r = it.step(4, it.last_ts + 60.5, [0.0])   # > τ_D -- граница сессии
    assert r.session_break
    assert r.d == pytest.approx(al.lambda_0(P) * d3 * math.exp(-60.5 / 60.0), abs=1e-15)


# ======================================================================= темп Δt_0: темп 5 / 6 / 21 с
@pytest.mark.parametrize("dt", [5.0, 6.0, 21.0])
def test_b08b_MA16_alarm_4th_reset_5th_any_tempo(dt):
    it = DissonanceIntegrator(P)
    fails, eid, t = _run(it, [1, 1, 1, 1], dt)
    assert _alarm_at(fails) == 4 and [a for _, a in fails] == [False, False, False, True]
    assert fails[3][0] == pytest.approx(0.5796, abs=1e-4)
    succ, _, _ = _run(it, [0] * 6, dt, eid, t)
    assert _reset_at(succ) == 5 and [a for _, a in succ][:5] == [True, True, True, True, False]
    assert succ[4][0] < 0.2 <= succ[3][0]


def test_b08b_MA16_same_result_for_irregular_tempo_inside_session():
    a = DissonanceIntegrator(P)
    b = DissonanceIntegrator(P)
    ts = [0.0, 5.0, 11.0, 32.0, 37.0, 43.0, 64.0, 69.5, 75.0, 96.0]
    for i, t in enumerate(ts):
        e = [1.0] if i < 4 else [0.0]
        ra = a.step(i + 1, t, e)
        rb = b.step(i + 1, 13.0 * i, e)
        assert ra.d == pytest.approx(rb.d, abs=1e-15) and ra.alarm == rb.alarm


# ======================================================================= длина серии
@pytest.mark.parametrize("n_fail,n_reset", [(4, 5), (5, 6), (8, 7), (12, 8), (30, 8)])
def test_b08b_MA17_reset_depends_on_series_length(n_fail, n_reset):
    it = DissonanceIntegrator(P)
    fails, eid, t = _run(it, [1] * n_fail, 6.0)
    assert _alarm_at(fails) == 4
    succ, _, _ = _run(it, [0] * 12, 6.0, eid, t)
    assert _reset_at(succ) == n_reset          # норма безопасности (решение 16:16), потолок 8


# ======================================================================= защёлка A_t: защёлка -- автомат, не флаг
def test_b08b_MA05_latch_held_by_automaton_not_caller_flag():
    it = DissonanceIntegrator(P)
    fails, eid, t = _run(it, [1, 1, 1, 1], 13.0)
    succ, eid, t = _run(it, [0, 0], 13.0, eid, t)    # D 0,58 -> 0,47 -> 0,38: между Θ_reset и Θ_crit
    assert 0.2 <= it.d < 0.5 and it.alarm
    x = replace(X_OK, dissonance_alarm=False)
    assert al.decide(P_DEC, replace(x, d_t=it.d)).outcome is Outcome.EXECUTE   # дефект защёлка A_t на пути флага
    d = al.decide(P_DEC, it.decision_inputs(x))
    assert d.outcome is Outcome.OBSERVE and "DISSONANCE" in d.reasons
    dec = al.guarded_decide_event(P_DEC, it, eid + 1, t + 13.0, [0.0], lambda: x)
    assert dec.outcome is Outcome.OBSERVE and it.alarm          # 3-й успех: D ≈ 0,30 -- тревога держится
    d = al.decide(P_DEC, it.decision_inputs(replace(x, dissonance_alarm=None)))
    assert d.outcome is Outcome.HALT and "BAD_INPUT:dissonance_alarm" in d.reasons   # тип флага не маскируется
    quiet = DissonanceIntegrator(P)
    assert al.decide(P_DEC, quiet.decision_inputs(replace(x, dissonance_alarm=True))).outcome is Outcome.OBSERVE


# ======================================================================= R-06: автомат A_t, восстановление
def test_b08b_R06_state_roundtrip_equals_uninterrupted():
    seq = [1, 1, 1, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0]
    ref = DissonanceIntegrator(P)
    ref_tr, _, _ = _run(ref, seq, 6.0)
    for cut in range(1, len(seq)):
        it = DissonanceIntegrator(P)
        tr1, eid, t = _run(it, seq[:cut], 6.0)
        st = json.loads(json.dumps(it.to_state()))            # примитивы: переживает запись на диск
        it2 = DissonanceIntegrator.from_state(P, st)
        tr2, _, _ = _run(it2, seq[cut:], 6.0, eid, t)
        got = tr1 + tr2
        assert [a for _, a in got] == [a for _, a in ref_tr]
        assert [d for d, _ in got] == pytest.approx([d for d, _ in ref_tr], abs=1e-15)
        assert it2.k_high == ref.k_high


def test_b08b_R06_restore_refuses_corrupt_or_foreign_state():
    it = DissonanceIntegrator(P)
    _run(it, [1, 1, 1, 1], 6.0)
    good = it.to_state()
    assert good["schema"] == al.STATE_SCHEMA and good["alarm"] is True and good["k_high"] == 1
    bad_state = [dict(good, d=1.5), dict(good, d=math.nan), dict(good, alarm=1), dict(good, alarm=False),
                 dict(good, d=0.1), dict(good, k_high=0), dict(good, k_high=True), dict(good, schema="x"),
                 dict(good, last_ts=None), dict(good, last_event_id=-1), dict(good, last_ts=math.inf), "x"]
    for st in bad_state:
        with pytest.raises(al.InvalidStateError):
            DissonanceIntegrator.from_state(P, st)
    for k, v in (("tau_D", 30.0), ("dt_0", 6.0), ("theta_crit", 0.6), ("theta_reset", 0.1), ("dt_0", None)):
        with pytest.raises(al.InvalidConfigError):
            DissonanceIntegrator.from_state(P, dict(good, **{k: v}))
    d = al.guarded_decide(P_DEC, lambda: DissonanceIntegrator.from_state(P, dict(good, d=0.1)).decision_inputs(X_OK))
    assert d.outcome is Outcome.HALT and d.reasons == ("INVALID_STATE",)
    d = al.guarded_decide(P_DEC, lambda: DissonanceIntegrator.from_state(P, dict(good, tau_D=1.0)).decision_inputs(X_OK))
    assert d.outcome is Outcome.HALT and d.reasons == ("INVALID_CONFIG",)
    fresh = DissonanceIntegrator.from_state(P, DissonanceIntegrator(P).to_state())
    assert fresh.d == 0.0 and not fresh.alarm and fresh.last_ts is None


def test_b08b_R06_restart_after_pause_projection():
    """Изменён в 3c по заморозка D при A_t = 1, решение Оператора 18:21 (fail-closed): было (3b) -- проекция через
    час снимала тревогу и decide давал EXECUTE; стало -- тревога и D переживают паузу, decide = OBSERVE."""
    it = DissonanceIntegrator(P)
    _, eid, t = _run(it, [1, 1, 1, 1], 6.0)
    it2 = DissonanceIntegrator.from_state(P, it.to_state())
    assert it2.project(t + 30.0) == (it2.d, True)              # внутри сессии -- без изменений
    d_p, a_p = it2.project(t + 3600.0)                         # после часа -- тревога НЕ снята (заморозка D при A_t = 1)
    assert d_p == it2.d and a_p is True and it2.alarm           # D заморожен; состояние не меняется
    dec = al.decide(P_DEC, it2.decision_inputs(X_OK, now_ts=t + 3600.0))
    assert dec.outcome is Outcome.OBSERVE and "DISSONANCE" in dec.reasons
    with pytest.raises(al.InvalidTimestampError):
        it2.project(t - 1.0)


# ======================================================================= R-08: Δt < 0
def test_b08b_R08_negative_dt_invalid_timestamp_halt():
    it = DissonanceIntegrator(P)
    _run(it, [1, 1], 6.0)
    before = it.to_state()
    with pytest.raises(al.InvalidTimestampError):
        it.step(3, it.last_ts - 0.001, [1.0])
    assert it.to_state() == before                              # состояние не тронуто
    for bad_ts in (math.nan, math.inf, None, True, "12"):
        with pytest.raises(al.InvalidTimestampError):
            it.step(3, bad_ts, [1.0])
    assert it.to_state() == before
    d = al.guarded_decide_event(P_DEC, it, 3, it.last_ts - 5.0, [0.0], lambda: X_OK)
    assert d.outcome is Outcome.HALT and d.reasons == ("INVALID_TIMESTAMP",)
    with pytest.raises(al.InvalidTimestampError):
        al.decay(-1.0, 60.0)
    assert issubclass(al.InvalidTimestampError, ValueError)     # контракт 1d (ValueError) сохранён
    d = al.guarded_decide(P_DEC, lambda: replace(X_OK, d_t=al.decay(-1.0, 60.0)))
    assert d.outcome is Outcome.HALT and d.reasons == ("INVALID_TIMESTAMP",)


# ======================================================================= R-09: дедупликация по event_id
def test_b08b_R09_duplicate_event_does_not_change_D():
    it = DissonanceIntegrator(P)
    r1 = it.step(10, 100.0, [1.0])
    for _ in range(5):                                          # повтор того же сбоя (ретрай чтения журнала)
        r = it.step(10, 100.0, [1.0])
        assert r.label is Label.DUPLICATE_EVENT and r.delta is None and r.d == r1.d and not r.alarm
    assert it.d == pytest.approx(Q, abs=1e-12) and it.k_high == 0
    r = it.step(9, 50.0, [1.0])                                 # старый seq (и ts раньше) -- не учитывается
    assert r.label is Label.DUPLICATE_EVENT and it.d == pytest.approx(Q, abs=1e-12)
    r = it.step(11, 106.0, [1.0])
    assert r.label is None and r.delta == 1 and it.d == pytest.approx(Q + al.lambda_0(P) * Q, abs=1e-12)
    for bad in (-1, 1.5, True, None):
        with pytest.raises(ValueError):
            it.step(bad, 200.0, [0.0])
    d0 = it.d
    dec = al.guarded_decide_event(P_DEC, it, 11, 106.0, [1.0], lambda: X_OK)
    assert it.d == d0 and dec.outcome is Outcome.EXECUTE


# ======================================================================= R-02: Z = 0 -> MODEL_MISMATCH
def test_b08b_R02_zero_likelihood_model_mismatch():
    bm = [0.2, 0.5, 0.3]
    r = al.update_belief(bm, [0.0, 0.0, 0.0])
    assert r.belief == pytest.approx(tuple(bm), abs=1e-15)      # b = b⁻, без outlier-шума
    assert r.outcome is Outcome.OBSERVE and r.label is Label.MODEL_MISMATCH and r.normalizer == 0.0
    r = al.update_belief([1.0, 0.0], [0.0, 1.0])                # Z ≠ 0 только там, где b⁻ = 0
    assert r.label is Label.MODEL_MISMATCH and r.outcome is Outcome.OBSERVE
    r = al.update_belief(bm, [0.9, 0.1, 0.4])
    assert r.label is None and r.outcome is None


# ======================================================================= R-05: INVALID_DISTRIBUTION
@pytest.mark.parametrize("bad", [[0.0, 0.0], [], [math.nan, 1.0], [-0.1, 1.1], [math.inf, 1.0]], ids=repr)
def test_b08b_R05_invalid_distribution_label_halt(bad):
    T = [[1.0] * max(len(bad), 1)] * max(len(bad), 1)
    with pytest.raises(al.InvalidDistributionError):
        al.predict_belief(bad, T, [1.0] * len(bad) or [1.0], 0.0, 600.0)
    with pytest.raises(ValueError):                             # подкласс ValueError: контракт 1d
        al.update_belief(bad, [1.0] * len(bad))
    d = al.guarded_decide(P_DEC, lambda: replace(X_OK, c_state=al.update_belief(bad, [1.0] * len(bad)).belief[0]))
    assert d.outcome is Outcome.HALT and d.reasons == ("INVALID_DISTRIBUTION",)


def test_b08b_R05_transition_row_invalid():
    with pytest.raises(al.InvalidDistributionError):
        al.predict_belief([0.5, 0.5], [[0.0, 0.0], [0.5, 0.5]], [0.5, 0.5], 1.0, 600.0)


# ======================================================================= UNSEEN_TRANSITION
def test_b08b_unseen_transition_label():
    assert al.transition_status([0, 0, 0]) is Label.UNSEEN_TRANSITION
    assert al.transition_status([0, 3, 1], x_next=0) is Label.UNSEEN_TRANSITION
    assert al.transition_status([0, 3, 1], x_next=1) is None
    assert al.transition_status([2, 0, 0]) is None
    for bad in ([], [-1, 2], [math.nan]):
        with pytest.raises(ValueError):
            al.transition_status(bad)
    for bad_x in (3, -1, True, 1.0):
        with pytest.raises(ValueError):
            al.transition_status([1, 1, 1], x_next=bad_x)


# ======================================================================= допуск d_∞ / R-04: d_∞, d_Σ -- телеметрия
ALPHA = (0.5, 0.5)
KAPPA = (10.0, 255.0)
EPS = (0.1, 0.1)


def test_b08b_MA11_d_inf_does_not_mask_feature():
    s, ref = (5.0, 100.0), (5.0, 150.0)                         # яркость точна, цвет на 50 из 255
    assert al.state_distance(s, ref, ALPHA, KAPPA) == pytest.approx(0.5 * 50 / 255, abs=1e-15)
    assert al.state_distance(s, ref, ALPHA, KAPPA) <= 0.1       # d_Σ маскирует (было бы «допустимо»)
    assert al.state_distance_inf(s, ref, KAPPA) == pytest.approx(50 / 255, abs=1e-15)
    assert al.state_admissible_inf(s, ref, KAPPA, EPS) is False
    assert al.state_distance_telemetry(s, ref, ALPHA, KAPPA) == pytest.approx(0.5 * 50 / 255, abs=1e-15)


def test_b08b_MA11_boundaries_slack_1e9():
    assert al.state_admissible_inf((6.0, 0.0), (5.0, 0.0), KAPPA, EPS) is True        # 1 деление = 0,1
    assert al.state_admissible_inf((0.0, 25.5), (0.0, 0.0), KAPPA, EPS) is True       # 10 % шкалы цвета
    assert al.state_admissible_inf((0.0, 0.1 * 255), (0.0, 0.0), KAPPA, EPS) is True  # ε_norm·κ во float
    assert al.state_admissible_inf((0.0, 25.6), (0.0, 0.0), KAPPA, EPS) is False
    assert al.state_admissible_inf((7.0, 0.0), (5.0, 0.0), KAPPA, EPS) is False
    assert al.state_admissible_inf((7.0, 0.0), (5.0, 0.0), KAPPA, (0.2, 0.1)) is True  # ε по признакам
    assert al.state_admissible_inf((5.0, 30.0), (5.0, 0.0), KAPPA, (0.2, 0.1)) is False
    assert al.state_distance_inf((99.0, 0.0), (0.0, 0.0), KAPPA) == 1.0              # насыщение min(1, ·)


def test_b08b_MA11_c_state_inf():
    states = [(4.0, 0.0), (5.0, 0.0), (5.0, 60.0), (8.0, 0.0)]
    post = [0.1, 0.6, 0.2, 0.1]
    c = al.state_admissibility_inf(post, states, (5.0, 0.0), KAPPA, EPS, invariants=[1])
    assert c == pytest.approx(0.7, abs=1e-12)
    assert al.state_admissibility_inf(post, states, (5.0, 0.0), KAPPA, EPS, invariants=[1, 0]) == 0.0
    with pytest.raises(al.InvalidDistributionError):
        al.state_admissibility_inf([0, 0, 0, 0], states, (5.0, 0.0), KAPPA, EPS, invariants=[1])


@pytest.mark.parametrize("alpha", [(0.5, 0.4), (1.2, -0.2), (0.5, math.nan), (), (True, False), (0.6, 0.6)],
                         ids=repr)
def test_b08b_MA11_alpha_invalid_config(alpha):
    with pytest.raises(al.InvalidConfigError):
        al.validate_alpha(alpha)
    d = al.guarded_decide(P_DEC, lambda: replace(
        X_OK, c_state=1.0 - al.state_distance_telemetry((1.0, 1.0), (1.0, 1.0), alpha, KAPPA)))
    assert d.outcome is Outcome.HALT and d.reasons == ("INVALID_CONFIG",)


def test_b08b_MA11_eps_kappa_invalid_config():
    assert al.validate_alpha((0.25, 0.75)) == (0.25, 0.75)
    for eps in ((1.1, 0.1), (-0.1, 0.1), (math.nan, 0.1), (0.1,)):
        with pytest.raises(al.InvalidConfigError):
            al.state_admissible_inf((1.0, 1.0), (1.0, 1.0), KAPPA, eps)
    for kap in ((0.0, 255.0), (-1.0, 255.0), (math.inf, 255.0)):
        with pytest.raises(al.InvalidConfigError):
            al.state_distance_inf((1.0, 1.0), (1.0, 1.0), kap)
    with pytest.raises(ValueError):
        al.state_distance_inf((math.inf, 1.0), (1.0, 1.0), KAPPA)


# ======================================================================= R-07: N_hyp < 2, NO_DATA порога
def test_b08b_R07_n_hyp_below_two_clarify():
    assert al.decide(P_DEC, X_OK).outcome is Outcome.EXECUTE                         # n_hyp не передан (1d)
    assert al.decide(P_DEC, replace(X_OK, n_hyp=2)).outcome is Outcome.EXECUTE
    for n in (0, 1):
        d = al.decide(P_DEC, replace(X_OK, n_hyp=n))
        assert d.outcome is Outcome.CLARIFY and d.reasons == ("N_HYP",)
    for bad in (-1, 1.0, True, "2"):
        d = al.decide(P_DEC, replace(X_OK, n_hyp=bad))
        assert d.outcome is Outcome.HALT and "BAD_INPUT:n_hyp" in d.reasons
    d = al.decide(P_DEC, replace(X_OK, n_hyp=1, c_state=0.1))                         # OBSERVE жёстче CLARIFY
    assert d.outcome is Outcome.OBSERVE


def test_b08b_R07_no_data_threshold_halt_even_with_one_hypothesis():
    for name in al.DECISION_THRESHOLDS:
        d = al.decide(replace(P_DEC, **{name: None}), replace(X_OK, n_hyp=1))
        assert d.outcome is Outcome.HALT and ("NO_DATA:" + name) in d.reasons


def test_b08b_end_to_end_file_params_intent_no_data_halt():
    """Сквозной decide на параметрах файла: намерение NO_DATA -> HALT (ожидаемо до решения 3d)."""
    p = _p_file()
    it = DissonanceIntegrator(p)
    d = al.guarded_decide_event(p, it, 1, 0.0, [0.0], lambda: replace(X_OK, n_hyp=3))
    assert d.outcome is Outcome.HALT
    assert set(d.reasons) == {"NO_DATA:theta_intent", "NO_DATA:theta_margin", "NO_DATA:theta_goal"}
    assert it.last_event_id == 1 and it.d == 0.0                 # событие учтено, решение -- HALT


def test_b08b_guarded_event_errors_never_execute():
    it = DissonanceIntegrator(P)
    cases = [(lambda: al.guarded_decide_event(P_DEC, None, 1, 0.0, [0.0], lambda: X_OK), "INVALID_CONFIG"),
             (lambda: al.guarded_decide_event(P_DEC, it, 1, 0.0, [math.nan], lambda: X_OK), "KERNEL_ERROR:ValueError"),
             (lambda: al.guarded_decide_event(P_DEC, it, 2, 1.0, [0.0], lambda: 42), "KERNEL_ERROR:TypeError")]
    for f, reason in cases:
        d = f()
        assert d.outcome is Outcome.HALT and d.reasons == (reason,)
    assert al.guarded_decide(P_DEC, lambda: DissonanceIntegrator(AbstractionParams()).decision_inputs(X_OK)
                             ).reasons == ("NO_DATA:kernel",)


def test_b08b_phase2_form_unchanged():
    """dissonance_step фазы 2 (λ_t по реальному Δt) не изменён: его вызывают check_sigma_2e и тесты 1d."""
    lam = math.exp(-13.0 / 60.0)
    assert al.dissonance_step(0.0, [1.0], [1.0], dt=13.0, tau_D=60.0) == pytest.approx(1 - lam, abs=1e-15)
    assert al.dissonance_step(0.5, [0.0], [1.0], dt=13.0, tau_D=60.0) == pytest.approx(0.5 * lam, abs=1e-15)


def test_b08b_C1_pinned_alarm_exit_on_failure_after_long_pause():
    """Изменён в 3c по заморозка D при A_t = 1, решение Оператора 18:21. Было (3b, C1): после паузы ≫ τ_D сбой давал
    D = q = 0,1948 < Θ_reset и защёлка снималась на событии-сбое (при Δt_0 13 с; при 13,4 с -- нет). Стало: при A_t = 1
    распад на границе сессии не применяется -> сбой после паузы тревогу не снимает ни при каком Δt_0."""
    for dt0 in (13.0, 13.4):
        it = DissonanceIntegrator(replace(P, dt_0=dt0))
        _, eid, t = _run(it, [1, 1, 1, 1], 6.0)
        assert it.alarm
        d4 = it.d
        r = it.step(eid + 1, t + 3600.0, [1.0])
        lam = al.lambda_0(replace(P, dt_0=dt0))
        assert r.session_break and r.frozen and r.alarm is True
        assert r.d == pytest.approx(lam * d4 + (1.0 - lam), abs=1e-15)   # без множителя e^(−Δt/τ_D)
