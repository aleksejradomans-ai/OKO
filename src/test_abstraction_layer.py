"""test_abstraction_layer.py -- граничные тесты M01–M18 ядра Σ (этап 1b) + property-набор
+ строгий отказ этапа 1d (решение Оператора 15.11 00:18/00:51): имена test_d1_….
M05/M16/M18 изменены в 1d: ∞ в d_Σ -> отказ; M16 включает Θ_reset; M18 -- строго HALT.

M-тесты: имена test_M01_… test_M18_… (ровно 18; блок считает их через -k test_M).
Property-based: библиотеки hypothesis в окружении службы нет (site-packages, 14.11) -> по правилу
не ставим, делаем на фиксированном наборе случайных входов (seed 20261114), имена test_prop_….
"""
import ast
import math
import random
from dataclasses import replace
from pathlib import Path

import pytest

import abstraction_layer as al
from abstraction_layer import (AbstractionParams, Budget, BudgetExhausted, CovarianceError, DecisionInputs,
                               DissonanceMonitor, NoDataError, Outcome)

SEED = 20261114
N_CASES = 300

# Полный набор параметров ТОЛЬКО для тестов (не калибровка; пороги в коде остаются NO_DATA).
P_FULL = AbstractionParams(tau_b=10.0, tau_D=5.0, tau_h=30.0, tau_i=30.0, kappa_space=0.5, eps_cov=1e-6,
                           eps_state=0.2, theta_state=0.7, theta_intent=0.6, theta_margin=0.1,
                           theta_goal=0.5, theta_crit=0.8, theta_reset=0.4)
X_MAX = DecisionInputs(c_state=1.0, q_best=1.0, q_second=0.0, c_goal=1.0, d_t=0.0,
                       authorization=1, safety_gate=1, budget_remaining=10)


def _rand_dist(rng, n):
    v = [rng.random() for _ in range(n)]
    s = sum(v)
    return [x / s for x in v]


def _rand_spd(rng, n, degenerate=False):
    if degenerate:
        return [[0.0] * n for _ in range(n)]
    a = [[rng.uniform(-1, 1) for _ in range(n)] for _ in range(n)]
    return [[sum(a[i][k] * a[j][k] for k in range(n)) for j in range(n)] for i in range(n)]


# ======================================================================= M01–M18
def test_M01_dt_zero_rho_one_pure_prediction():
    assert al.decay(0.0, 10.0) == 1.0
    assert al.decay(0.0, None) == 1.0          # ρ = 1 не зависит от τ_b
    b = [0.5, 0.3, 0.2]
    T = [[0.1, 0.9, 0.0], [0.0, 0.2, 0.8], [0.7, 0.0, 0.3]]
    pi0 = [1.0, 0.0, 0.0]                      # «сильная» π₀ -- примесь была бы видна
    got = al.predict_belief(b, T, pi0, dt=0.0, tau_b=10.0)
    pure = [sum(T[xp][x] * b[xp] for xp in range(3)) for x in range(3)]
    assert got == pytest.approx(pure, abs=1e-15)
    assert got[0] == pytest.approx(0.5 * 0.1 + 0.2 * 0.7, abs=1e-15)


def test_M02_dt_infinite_belief_tends_to_pi0():
    b = [0.0, 0.0, 1.0]
    T = [[0.0, 0.0, 1.0]] * 3
    pi0 = [0.6, 0.3, 0.1]
    assert al.predict_belief(b, T, pi0, dt=math.inf, tau_b=10.0) == pytest.approx(pi0, abs=1e-15)
    prev = None
    for dt in (1.0, 10.0, 100.0, 1e4, 1e6):
        got = al.predict_belief(b, T, pi0, dt=dt, tau_b=10.0)
        err = max(abs(got[i] - pi0[i]) for i in range(3))
        assert prev is None or err <= prev
        prev = err
    assert prev < 1e-12


def test_M03_after_update_sum_one_nonneg():
    r = al.update_belief([0.2, 0.5, 0.3], [0.9, 0.1, 0.4])
    assert r.outcome is None
    assert math.fsum(r.belief) == pytest.approx(1.0, abs=1e-12)
    assert all(x >= 0 for x in r.belief)


def test_M04_zero_likelihood_everywhere_no_nan_observe():
    bm = [0.2, 0.5, 0.3]
    r = al.update_belief(bm, [0.0, 0.0, 0.0])
    assert r.outcome is Outcome.OBSERVE
    assert r.normalizer == 0.0
    assert all(math.isfinite(x) for x in r.belief)
    assert list(r.belief) == pytest.approx(bm)  # вера не испорчена
    r2 = al.update_belief([1.0, 0.0], [0.0, 1.0])  # Z ≠ 0, но там, где b⁻ = 0
    assert r2.outcome is Outcome.OBSERVE and all(math.isfinite(x) for x in r2.belief)


def test_M05_component_saturates_at_alpha():
    alpha, kappa = [0.3], [2.0]
    for d in (2.0, 2.0001, 5.0, 1e9, 1e300):
        assert al.state_distance([d], [0.0], alpha, kappa) == pytest.approx(0.3)
    with pytest.raises(ValueError):                          # 1d: ∞ -- отказ, а не насыщение
        al.state_distance([math.inf], [0.0], alpha, kappa)
    assert al.state_distance([1.0], [0.0], alpha, kappa) == pytest.approx(0.15)  # ниже κ -- линейно
    # по всем компонентам сразу: сумма насыщается на Σ α_j
    assert al.state_distance([10, -10, 99], [0, 0, 0], [0.2, 0.3, 0.5], [1, 1, 1]) == pytest.approx(1.0)


def test_M06_s_equals_expected_distance_zero():
    s = [0.3, -4.0, 17.5, 0.0]
    assert al.state_distance(s, list(s), [0.1, 0.2, 0.3, 0.4], [1.0, 0.5, 2.0, 3.0]) == 0.0


def test_M07_any_invariant_zero_forces_zero():
    post = [1.0, 0.0, 0.0]
    dist = [0.0, 0.0, 0.0]                         # максимальное сходство
    assert al.state_admissibility(post, dist, 0.2, [1, 1, 1]) == pytest.approx(1.0)
    for inv in ([0], [1, 0], [0, 1, 1], [1, 1, 0]):
        assert al.state_admissibility(post, dist, 0.2, inv) == 0.0
    assert al.state_admissibility(post, dist, None, [0]) == 0.0   # H_k сильнее даже NO_DATA ε


def test_M08_c_state_in_unit_interval():
    cases = [([1, 0], [0, 0], 0.0, []), ([0.5, 0.5], [1, 1], 0.5, [1]), ([3, 7], [0.1, 9], 0.2, [1, 1]),
             ([1e-300, 1e-300], [0, 0], 0.0, []), ([0, 0], [0, 0], 0.1, [])]
    for post, dist, eps, inv in cases:
        c = al.state_admissibility(post, dist, eps, inv)
        assert 0.0 <= c <= 1.0


def test_M09_degenerate_covariance_regularized():
    z = [[0.0, 0.0], [0.0, 0.0]]
    with pytest.raises(ValueError):
        al.mahalanobis_sq([1.0, 2.0], z)                       # без ε_cov -- необратима
    sig = al.combined_covariance(z, z, z, eps_cov=1e-6)
    r2 = al.mahalanobis_sq([1.0, 2.0], sig)
    assert math.isfinite(r2) and r2 == pytest.approx((1 + 4) / 1e-6, rel=1e-9)
    rank1 = [[1.0, 1.0], [1.0, 1.0]]                           # вырождена ранга 1
    r2b = al.mahalanobis_sq([1.0, -1.0], al.combined_covariance(rank1, z, z, eps_cov=1e-3))
    assert math.isfinite(r2b) and r2b > 0
    with pytest.raises(NoDataError):
        al.combined_covariance(z, z, z, eps_cov=None)


def test_M10_r2_nonnegative():
    assert al.mahalanobis_sq([0.0, 0.0], [[1.0, 0.0], [0.0, 1.0]]) == 0.0
    sig = al.combined_covariance([[2.0, 0.9], [0.9, 0.5]], [[0.1, 0], [0, 0.1]], [[0, 0], [0, 0]], 1e-9)
    for d in ([1, 0], [0, 1], [1, -1], [-3, 7], [1e-12, -1e-12]):
        assert al.mahalanobis_sq(d, sig) >= 0.0


def test_M11_very_old_frame_utility_to_zero():
    base = dict(q=1.0, c_h=1.0, c_i=1.0, r2=0.0, tau_h=30.0, tau_i=30.0, kappa_space=0.5)
    fresh = al.spatial_utility(a_h=0.0, a_i=0.0, **base)
    assert fresh == 1.0
    for a in (1e3, 1e5, 1e308, math.inf):
        assert al.spatial_utility(a_h=a, a_i=0.0, **base) == pytest.approx(0.0, abs=1e-12)
        assert al.spatial_utility(a_h=0.0, a_i=a, **base) == pytest.approx(0.0, abs=1e-12)
    seq = [al.spatial_utility(a_h=a, a_i=a, **base) for a in (0, 10, 100, 1000)]
    assert seq == sorted(seq, reverse=True)


def test_M12_all_utilities_zero_clarify():
    r = al.intent_weights([0.0, 0.0, 0.0])
    assert r.outcome is Outcome.CLARIFY
    assert r.weights == (0.0, 0.0, 0.0) and all(math.isfinite(w) for w in r.weights)
    assert al.intent_weights([]).outcome is Outcome.CLARIFY


def test_M13_weights_sum_one():
    r = al.intent_weights([0.2, 0.0, 1.3, 0.5])
    assert r.outcome is None
    assert math.fsum(r.weights) == pytest.approx(1.0, abs=1e-12)
    W = al.intent_vectors(r.weights, [[3, 4], [1, 0], [0, -2], [0, 0]])
    assert W[0] == pytest.approx((r.weights[0] * 0.6, r.weights[0] * 0.8))
    assert W[3] == (0.0, 0.0)


def test_M14_small_margin_not_execute():
    x = replace(X_MAX, q_best=0.9, q_second=0.85)          # зазор 0.05 < Θ_margin 0.1
    d = al.decide(P_FULL, x)
    assert d.outcome is not Outcome.EXECUTE and "MARGIN" in d.reasons
    assert al.decide(P_FULL, replace(X_MAX, q_best=0.9, q_second=0.79)).outcome is Outcome.EXECUTE


def test_M15_no_authorization_or_safety_not_execute():
    assert al.decide(P_FULL, X_MAX).outcome is Outcome.EXECUTE            # контроль: всё максимально
    for kw in (dict(authorization=0), dict(safety_gate=0), dict(authorization=0, safety_gate=0),
               dict(authorization=0.999), dict(safety_gate=None), dict(authorization="1")):
        d = al.decide(P_FULL, replace(X_MAX, **kw))
        assert d.outcome is not Outcome.EXECUTE, kw


def test_M16_any_threshold_no_data_not_execute():
    assert AbstractionParams().missing()                    # по умолчанию всё NO_DATA
    assert al.decide(AbstractionParams(), X_MAX).outcome is Outcome.HALT
    assert "theta_reset" in al.DECISION_THRESHOLDS                 # 1d: Θ_reset обязателен
    assert set(al.DECISION_THRESHOLDS) == {f for f in AbstractionParams._UNIT} | {"theta_margin"}
    for name in al.DECISION_THRESHOLDS:
        d = al.decide(replace(P_FULL, **{name: None}), X_MAX)
        assert d.outcome is Outcome.HALT and ("NO_DATA:" + name) in d.reasons, name


def test_M17_hysteresis():
    with pytest.raises(ValueError):
        AbstractionParams(theta_crit=0.5, theta_reset=0.5)
    with pytest.raises(ValueError):
        AbstractionParams(theta_crit=0.5, theta_reset=0.7)
    mon = DissonanceMonitor(P_FULL)                         # Θ_crit 0.8, Θ_reset 0.4
    trace = [0.1, 0.79, 0.8, 0.6, 0.5, 0.41, 0.4, 0.39, 0.7, 0.81]
    got = [mon.update(d) for d in trace]
    assert got == [False, False, True, True, True, True, True, False, False, True]
    mon2 = DissonanceMonitor(P_FULL)
    mon2.update(0.9)
    alarm = mon2.update(0.5)                                # ниже Θ_crit, но выше Θ_reset
    assert alarm and mon2.k_high == 2
    d = al.decide(P_FULL, replace(X_MAX, d_t=0.5, dissonance_alarm=alarm))
    assert d.outcome is Outcome.OBSERVE and "DISSONANCE" in d.reasons
    with pytest.raises(NoDataError):
        DissonanceMonitor(AbstractionParams())


def test_M18_budget_strictly_decreasing_zero_never_execute():
    b = Budget(n_obs=3, n_route=2, n_reflect=1)
    kinds = ["obs", "route", "reflect", "obs", "route", "obs"]
    seen = [b.remaining]
    for k in kinds:
        b = b.spend(k)
        seen.append(b.remaining)
    assert all(seen[i + 1] < seen[i] for i in range(len(seen) - 1))
    assert b.remaining == 0
    for k in ("obs", "route", "reflect"):
        with pytest.raises(BudgetExhausted):
            b.spend(k)
    d = al.decide(P_FULL, replace(X_MAX, budget_remaining=b.remaining))
    assert d.outcome is Outcome.HALT and d.reasons == ("BUDGET_EXHAUSTED",)   # 1d: строго HALT
    assert al.decide(P_FULL, replace(X_MAX, budget_remaining=-1)).outcome is Outcome.HALT


# ======================================================================= property (фиксированный набор)
def test_prop_M03_belief_distribution():
    rng = random.Random(SEED)
    for _ in range(N_CASES):
        n = rng.randint(1, 6)
        b = _rand_dist(rng, n)
        T = [_rand_dist(rng, n) for _ in range(n)]
        pi0 = _rand_dist(rng, n)
        dt = rng.choice([0.0, rng.uniform(0, 50), math.inf])
        bm = al.predict_belief(b, T, pi0, dt=dt, tau_b=rng.uniform(0.1, 20))
        assert math.fsum(bm) == pytest.approx(1.0, abs=1e-9) and all(x >= 0 for x in bm)
        z = [rng.choice([0.0, rng.random()]) for _ in range(n)]
        r = al.update_belief(bm, z)
        assert all(math.isfinite(x) and x >= 0 for x in r.belief)
        assert math.fsum(r.belief) == pytest.approx(1.0, abs=1e-9)
        assert (r.outcome is Outcome.OBSERVE) == (math.fsum(z[i] * bm[i] for i in range(n)) == 0)


def test_prop_M08_c_state_range():
    rng = random.Random(SEED + 8)
    for _ in range(N_CASES):
        n = rng.randint(1, 6)
        post = [rng.choice([0.0, rng.uniform(0, 10)]) for _ in range(n)]
        dist = [rng.uniform(0, 2) for _ in range(n)]
        inv = [rng.choice([0, 1, 1, 1]) for _ in range(rng.randint(0, 3))]
        c = al.state_admissibility(post, dist, rng.uniform(0, 1.5), inv)
        assert 0.0 <= c <= 1.0
        if 0 in inv:
            assert c == 0.0


def test_prop_M10_r2_nonnegative():
    rng = random.Random(SEED + 10)
    for _ in range(N_CASES):
        n = rng.randint(1, 4)
        sig = al.combined_covariance(_rand_spd(rng, n, rng.random() < 0.2), _rand_spd(rng, n), _rand_spd(rng, n),
                                     eps_cov=rng.choice([1e-9, 1e-6, 1e-3]))
        d = [rng.uniform(-100, 100) for _ in range(n)]
        r2 = al.mahalanobis_sq(d, sig)
        assert math.isfinite(r2) and r2 >= 0.0


def test_prop_M13_weights_normalized():
    rng = random.Random(SEED + 13)
    for _ in range(N_CASES):
        n = rng.randint(1, 8)
        u = []
        for _ in range(n):
            r2 = rng.uniform(0, 50)
            u.append(al.spatial_utility(q=rng.random(), c_h=rng.random(), c_i=rng.random(),
                                        a_h=rng.uniform(0, 1e4), a_i=rng.uniform(0, 1e4), r2=r2,
                                        tau_h=rng.uniform(1, 100), tau_i=rng.uniform(1, 100),
                                        kappa_space=rng.uniform(0, 2)))
        r = al.intent_weights(u)
        assert all(math.isfinite(w) and w >= 0 for w in r.weights)
        if r.outcome is None:
            assert math.fsum(r.weights) == pytest.approx(1.0, abs=1e-9)
        else:
            assert r.outcome is Outcome.CLARIFY and math.fsum(u) == 0


def test_prop_decide_execute_only_when_all_conditions():
    rng = random.Random(SEED + 99)
    p = P_FULL
    for _ in range(N_CASES * 3):
        x = DecisionInputs(c_state=rng.random(), q_best=rng.random(), q_second=rng.random(), c_goal=rng.random(),
                           d_t=rng.random(), authorization=rng.choice([0, 1, 1]), safety_gate=rng.choice([0, 1, 1]),
                           budget_remaining=rng.choice([0, 1, 5]), dissonance_alarm=rng.random() < 0.2)
        ok = (x.c_state >= p.theta_state and x.q_best >= p.theta_intent and x.q_best - x.q_second >= p.theta_margin
              and x.c_goal >= p.theta_goal and x.d_t < p.theta_crit and x.authorization == 1 and x.safety_gate == 1
              and x.budget_remaining > 0 and not x.dissonance_alarm)
        assert (al.decide(p, x).outcome is Outcome.EXECUTE) == ok


# ======================================================================= контракт модуля
def test_contract_stdlib_only_no_hardware():
    tree = ast.parse(Path(al.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    assert mods <= {"__future__", "math", "dataclasses", "enum", "typing"}, mods


def test_contract_defaults_all_no_data():
    # (этап 3b): + dt_0 -- номинальный шаг Δt_0 интегратора λ_0 (решение Оператора 16.11 16:16, темп Δt_0);
    # по умолчанию тоже NO_DATA. Прочие 13 параметров -- без изменений.
    p = AbstractionParams()
    assert set(p.missing()) == {"tau_b", "tau_D", "tau_h", "tau_i", "kappa_space", "eps_cov", "eps_state",
                                "theta_state", "theta_intent", "theta_margin", "theta_goal", "theta_crit",
                                "theta_reset", "dt_0"}
    with pytest.raises(NoDataError):
        al.decay(1.0, None)
    with pytest.raises(ValueError):
        AbstractionParams(tau_b=0.0)
    with pytest.raises(ValueError):
        AbstractionParams(theta_state=float("nan"))


def test_contract_nan_input_never_execute():
    for kw in (dict(c_state=float("nan")), dict(d_t=float("nan")), dict(q_best=math.inf)):
        assert al.decide(P_FULL, replace(X_MAX, **kw)).outcome is Outcome.HALT


# ======================================================================= 1d: строгий отказ
BAD_UNIT = (1.5, -0.5, 7.0, 2.0, 1.0 + 1e-12, -1e-300, math.nan, math.inf, -math.inf, True, None, "0.5")


def test_d1_theta_reset_no_data_halt():
    """A1: Θ_reset = NO_DATA при максимальных входах -- HALT с причиной NO_DATA:theta_reset (было EXECUTE)."""
    d = al.decide(replace(P_FULL, theta_reset=None), X_MAX)
    assert d.outcome is Outcome.HALT and d.reasons == ("NO_DATA:theta_reset",)


@pytest.mark.parametrize("field", al.DECISION_INPUTS_UNIT)
@pytest.mark.parametrize("value", BAD_UNIT, ids=repr)
def test_d1_decide_out_of_range_halt(field, value):
    """A2: вход вне [0, 1] / NaN / ±Inf / не число -> HALT BAD_INPUT, без обрезания."""
    d = al.decide(P_FULL, replace(X_MAX, **{field: value}))
    assert d.outcome is Outcome.HALT and "BAD_INPUT" in d.reasons and ("BAD_INPUT:" + field) in d.reasons


def test_d1_findings_A2_cases_halt():
    """Ровно случаи из FINDINGS A2 (было EXECUTE во всех четырёх)."""
    for kw in (dict(c_state=1.5), dict(d_t=-0.5), dict(c_goal=7.0), dict(q_best=2.0)):
        assert al.decide(P_FULL, replace(X_MAX, **kw)).outcome is Outcome.HALT, kw


def test_d1_decide_unit_boundaries_accepted():
    """0 и 1 -- допустимые значения; ошибки входа нет."""
    assert al.decide(P_FULL, X_MAX).outcome is Outcome.EXECUTE         # c_state=1, q_second=0, d_t=0
    d = al.decide(P_FULL, replace(X_MAX, c_state=0.0, c_goal=0.0))
    assert d.outcome is Outcome.OBSERVE and not any(r.startswith("BAD_INPUT") for r in d.reasons)
    assert al.decide(P_FULL, replace(X_MAX, d_t=1.0)).outcome is Outcome.OBSERVE


def test_d1_dissonance_alarm_must_be_bool():
    for v in (None, 1, 0, "yes", 0.0):
        d = al.decide(P_FULL, replace(X_MAX, dissonance_alarm=v))
        assert d.outcome is Outcome.HALT and "BAD_INPUT:dissonance_alarm" in d.reasons, v


def test_d1_decide_wrong_types_halt():
    assert al.decide(None, X_MAX).outcome is Outcome.HALT
    assert al.decide(P_FULL, None).outcome is Outcome.HALT


def test_d1_dissonance_delta_out_of_range_refused_not_clipped():
    """FINDINGS C2: e=5, β=1 давало D_t = 1.0 срезом. Теперь -- ошибка входа."""
    with pytest.raises(ValueError):
        al.dissonance_step(0.0, [5.0], [1.0], dt=1.0, tau_D=5.0)
    with pytest.raises(ValueError):
        al.dissonance_step(0.0, [0.6, 0.6], [1.0, 1.0], dt=1.0, tau_D=5.0)
    d = al.dissonance_step(0.2, [0.5, 1.0], [0.4, 0.6], dt=5.0, tau_D=5.0)     # δ = 0.8
    lam = math.exp(-1.0)
    assert d == pytest.approx(lam * 0.2 + (1 - lam) * 0.8, abs=1e-15)
    assert al.dissonance_step(0.3, [1.0], [1.0], dt=math.inf, tau_D=5.0) == 1.0   # граница δ = 1 допустима
    mon = DissonanceMonitor(P_FULL)
    for bad in (1.2, -0.1):
        with pytest.raises(ValueError):
            mon.update(bad)


def test_d1_state_distance_infinity_refused():
    """FINDINGS C3: state_distance(inf, inf) давало α_j. Теперь -- отказ."""
    for s, e in (([math.inf], [math.inf]), ([math.inf], [0.0]), ([0.0], [-math.inf])):
        with pytest.raises(ValueError):
            al.state_distance(s, e, [0.3], [1.0])
    with pytest.raises(ValueError):                                   # переполнение разности
        al.state_distance([1.7e308], [-1.7e308], [0.3], [1.0])


def test_d1_cov_not_psd_refused_not_zero():
    """B2: [[1,0],[0,−2]] давало r² = 0.0. Теперь CovarianceError -> HALT, не 0 и не EXECUTE."""
    bad = [[1.0, 0.0], [0.0, -2.0]]
    with pytest.raises(CovarianceError):
        al.mahalanobis_sq([0.0, 1.0], bad)
    z = [[0.0, 0.0], [0.0, 0.0]]
    with pytest.raises(CovarianceError):                              # невалидное слагаемое не маскируется ε и суммой
        al.combined_covariance([[5.0, 0.0], [0.0, 5.0]], bad, z, eps_cov=1e-6)

    def build():
        r2 = al.mahalanobis_sq([0.0, 1.0], bad)
        u = al.spatial_utility(1.0, 1.0, 1.0, 0.0, 0.0, r2, 30.0, 30.0, 0.5)
        return replace(X_MAX, q_best=u)
    d = al.guarded_decide(P_FULL, build)
    assert d.outcome is Outcome.HALT and d.reasons == ("KERNEL_ERROR:CovarianceError",)


def test_d1_cov_not_symmetric_refused():
    with pytest.raises(CovarianceError):
        al.mahalanobis_sq([1.0, 1.0], [[2.0, 0.5], [0.4, 2.0]])
    z = [[0.0, 0.0], [0.0, 0.0]]
    with pytest.raises(CovarianceError):
        al.combined_covariance([[1.0, 0.3], [0.0, 1.0]], z, z, eps_cov=1e-3)
    assert al.mahalanobis_sq([1.0, 1.0], [[2.0, 0.5], [0.5, 2.0]]) > 0          # контроль: симметричная -- ок


def test_d1_kernel_error_bad_scale_wrapped_halt():
    """B3: Σ_host = 1e4 ранга 1, ε_cov = 1e-9 -- исключение ядра не выходит наружу, исход HALT с причиной."""
    host = [[1e4, 1e4], [1e4, 1e4]]
    z = [[0.0, 0.0], [0.0, 0.0]]

    def build():
        sig = al.combined_covariance(host, z, z, eps_cov=1e-9)
        r2 = al.mahalanobis_sq([1.0, -1.0], sig)
        return replace(X_MAX, q_best=al.spatial_utility(1.0, 1.0, 1.0, 0.0, 0.0, r2, 30.0, 30.0, 0.5))
    d = al.guarded_decide(P_FULL, build)                 # не бросает
    assert d.outcome is Outcome.HALT and len(d.reasons) == 1 and d.reasons[0].startswith("KERNEL_ERROR:")
    with pytest.raises(ValueError):                      # напрямую -- ошибка, а не тихий r²
        al.mahalanobis_sq([1.0, -1.0], al.combined_covariance(host, z, z, eps_cov=1e-9))


def test_d1_guarded_decide_isolates_all_kernel_errors():
    def raiser(exc):
        def f():
            raise exc
        return f
    cases = [(ZeroDivisionError("x"), "KERNEL_ERROR:ZeroDivisionError"),
             (OverflowError("x"), "KERNEL_ERROR:OverflowError"),
             (CovarianceError("x"), "KERNEL_ERROR:CovarianceError"),
             (BudgetExhausted("x"), "KERNEL_ERROR:BudgetExhausted"),
             (TypeError("x"), "KERNEL_ERROR:TypeError"),
             (NoDataError("x"), "NO_DATA:kernel")]
    for exc, reason in cases:
        d = al.guarded_decide(P_FULL, raiser(exc))
        assert d.outcome is Outcome.HALT and d.reasons == (reason,), exc
    assert al.guarded_decide(P_FULL, lambda: 42).reasons == ("KERNEL_ERROR:BAD_RESULT",)
    assert al.guarded_decide(P_FULL, lambda: Budget(0, 0, 0).spend("obs")).outcome is Outcome.HALT
    assert al.guarded_decide(P_FULL, lambda: al.decay(1.0, None)).reasons == ("NO_DATA:kernel",)


def test_d1_guarded_decide_passes_through_normal_decision():
    assert al.guarded_decide(P_FULL, lambda: X_MAX).outcome is Outcome.EXECUTE
    d = al.guarded_decide(P_FULL, lambda: replace(X_MAX, budget_remaining=0))
    assert d.outcome is Outcome.HALT and d.reasons == ("BUDGET_EXHAUSTED",)
    assert al.guarded_decide(replace(P_FULL, theta_reset=None), lambda: X_MAX).reasons == ("NO_DATA:theta_reset",)


def test_d1_prop_out_of_range_never_execute():
    rng = random.Random(SEED + 101)
    for _ in range(N_CASES * 2):
        kw = {n: rng.random() for n in al.DECISION_INPUTS_UNIT}
        kw["q_second"] = 0.0
        field = rng.choice(al.DECISION_INPUTS_UNIT)
        kw[field] = rng.choice([rng.uniform(1.0 + 1e-9, 1e6), rng.uniform(-1e6, -1e-9), math.nan, math.inf])
        x = replace(X_MAX, **kw)
        d = al.decide(P_FULL, x)
        assert d.outcome is Outcome.HALT and ("BAD_INPUT:" + field) in d.reasons


def test_d1_prop_covariance_valid_ok_indefinite_refused():
    rng = random.Random(SEED + 102)
    n_bad = 0
    for _ in range(N_CASES):
        n = rng.randint(1, 4)
        spd = _rand_spd(rng, n)
        sig = al.combined_covariance(spd, _rand_spd(rng, n, rng.random() < 0.3), _rand_spd(rng, n), eps_cov=1e-3)
        d = [rng.uniform(-10, 10) for _ in range(n)]
        r2 = al.mahalanobis_sq(d, sig)
        assert math.isfinite(r2) and r2 >= 0.0
        if any(x != 0 for x in d):
            assert r2 > 0.0
        # явно неопределённая: −c на диагонали
        k = rng.randrange(n)
        ind = [list(r) for r in spd]
        ind[k][k] = -1.0 - rng.random()
        try:
            al.combined_covariance(ind, [[0.0] * n for _ in range(n)], [[0.0] * n for _ in range(n)], eps_cov=1e-3)
        except CovarianceError:
            n_bad += 1
        else:
            pytest.fail("неопределённое слагаемое принято")
    assert n_bad == N_CASES


def test_d1_kernel_isolated_no_new_imports():
    """B1 -- фаза 3: ядро изолировано; 1d не добавляет импорт бота/роутера/железа."""
    src = Path(al.__file__).read_text(encoding="utf-8")
    for bad in ("router", "bot", "telegram", "sqlite3", "socket", "requests"):
        assert ("import " + bad) not in src and ("from " + bad) not in src, bad
