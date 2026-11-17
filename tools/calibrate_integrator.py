"""tools/calibrate_integrator.py -- совместная калибровка (q, τ_D) импульсного интегратора диссонанса (фаза 3,
этап 3a п.2). Только stdlib. Ядро src/abstraction_layer.py и router.py не импортируются и не меняются.

Основание: решение Оператора 16.11 14:51 п.1–2 с оговоркой; реестр проверок распад D по паузе, темп Δt_0; этап 3a (УТВЕРЖДЁН 15:36).
Модель: D_t = min(1, e^(−Δt/τ_D)·D_{t−1} + q·δ_t), δ = 1 на сбое (any_fail_1), 0 на успехе; автомат A_t: вход D ≥ Θ_crit,
выход D < Θ_reset. Θ_crit = 0,50, Θ_reset = 0,20 -- ручные (12:04), берутся из config/sigma_params.json и сверяются.
Цели: при постоянном шаге Δt из D_0 = 0 тревога впервые на N_ALARM = 4-м сбое подряд, затем (успехи подряд) выход из
тревоги впервые на N_RESET = 5-м успехе.

Полоса (аналитически). Обозначим λ = e^(−Δt/τ_D), S_k(λ) = q·Σ_{i<k} λ^i, D_N = min(1, S_N). До тревоги насыщения нет.
  тревога на N-м  ⇔  S_{N−1}(λ) < Θ_crit ≤ S_N(λ);   сброс на M-м  ⇔  D_N·λ^M < Θ_reset ≤ D_N·λ^(M−1).
Все четыре функции растут по λ -> каждое условие -- полуинтервал по λ, пересечение -- полуинтервал [λ_lo, λ_hi).
В координате u = Δt/τ_D = −ln λ полоса (u_lo, u_hi] зависит только от q; для пары (q, τ_D) полоса по Δt -- (τ_D·u_lo,
τ_D·u_hi]. Отношение концов u_hi/u_lo от τ_D не зависит: покрыть [q10, q90] темпа можно, только если u_hi/u_lo ≥ q90/q10.
Перебор: сетка q × τ_D (Q_GRID × TAU_GRID); пара покрывает темп, если τ_D·u_lo < q10 и τ_D·u_hi ≥ q90. Выбор среди
покрывающих -- максимум запаса min(ln(q10/Δt_lo), ln(Δt_hi/q90)); равенство -> меньший q, затем меньший τ_D.
Нет покрывающей пары -> СТОП (код 4): пара не выбирается, файл параметров не трогается, в выводе -- диагностика для
вопроса Оператору (отношение концов, лучшая доля темпа в полосе, допустимые τ_D для q10/медианы/q90, вариант λ_0).
Проверка перебором: simulate -- прямой счёт рекурсии (используется в выводе поведения и в тестах).

Выход: JSON (--out) и текст (stdout) -- только агрегаты темпа (n, квантили) и параметры модели; значений журнала нет.
--write-params F: ТОЛЬКО если пара выбрана -- в F пишется раздел integrator_3a (остальное без изменений, формат как у
calibrate_sigma.dumps, детерминированно: два прогона -> байт-в-байт). При СТОП файл не трогается.
Коды: 0 -- пара выбрана; 4 -- СТОП (нет пары или мало данных, n < MIN_N); 2 -- ошибка входа (параметры не читаются,
Θ не совпадают с ручными).
Запуск: <python> tools\\calibrate_integrator.py [--path P] [--params F] [--out J] [--write-params F]
"""
from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import math
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("sigma_patterns_for_integrator", _HERE / "sigma_patterns.py")
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)

ROOT = _HERE.parent
DEFAULT_PATH = ROOT / "logs" / "sigma_shadow.jsonl"
DEFAULT_PARAMS = ROOT / "config" / "sigma_params.json"
SCHEMA = "sigma_integrator/3a-v1"
THETA_CRIT_MANUAL, THETA_RESET_MANUAL, TAU_D_MANUAL = 0.5, 0.2, 60.0     # решение 16.11 12:04 п.1 (ручные)
N_ALARM, N_RESET = 4, 5                                                  # решение 14:51 п.2
DT0_NOMINAL = 13.0                                                       # номинальный шаг расчёта 12:04 (темп Δt_0)
Q_GRID = tuple(round(0.0025 * i, 4) for i in range(2, 200))              # 0,005 .. 0,4975
TAU_GRID = tuple(round(0.5 * i, 1) for i in range(2, 1201))              # 1 .. 600 с
MIN_N = 10                                                               # меньше интервалов -- квантили не держат
BISECT_ITERS = 64
EXIT_OK, EXIT_INPUT, EXIT_STOP = 0, 2, 4
REF = "решение 2026-11-16 14:51 п.1–2 (импульсный интегратор, Δt_0 по темпу сессий); этап 3a"


class InputError(ValueError):
    pass


# ---------------------------------------------------------------- модель
def step(D: float, dt: float, q: float, tau: float, delta: int) -> float:
    return min(1.0, math.exp(-dt / tau) * D + q * delta)


def simulate(q: float, tau: float, dt: float, th_c: float = THETA_CRIT_MANUAL, th_r: float = THETA_RESET_MANUAL,
             max_fail: int = 200, max_succ: int = 5000) -> "tuple[int | None, int | None]":
    """Прямой счёт: сбои подряд с шагом dt из D=0 до тревоги (A: D ≥ Θ_crit), затем успехи до выхода (D < Θ_reset).
    -> (номер сбоя, на котором тревога, | None; номер успеха, на котором сброс, | None)."""
    D, alarm = 0.0, None
    for k in range(1, max_fail + 1):
        D = step(D, dt, q, tau, 1)
        if D >= th_c:
            alarm = k
            break
    if alarm is None:
        return None, None
    for k in range(1, max_succ + 1):
        D = step(D, dt, q, tau, 0)
        if D < th_r:
            return alarm, k
    return alarm, None


def _S(q: float, lam: float, k: int) -> float:
    return q * sum(lam ** i for i in range(k))


def _root(f, target: float) -> float:
    """Наименьшее λ ∈ [0, 1] с f(λ) ≥ target для растущей f (f(0) < target ≤ f(1) проверяет вызывающий)."""
    lo, hi = 0.0, 1.0
    for _ in range(BISECT_ITERS):
        mid = (lo + hi) / 2
        if f(mid) >= target:
            hi = mid
        else:
            lo = mid
    return hi


def u_band(q: float, th_c: float = THETA_CRIT_MANUAL, th_r: float = THETA_RESET_MANUAL,
           n_alarm: int = N_ALARM, n_reset: int = N_RESET) -> "tuple[float, float] | None":
    """Полоса u = Δt/τ_D (u_lo, u_hi] (нижний конец открыт, верхний закрыт), где тревога на n_alarm-м сбое и сброс на
    n_reset-м успехе; None -- полосы нет."""
    if not (0 < q < th_c) or not (0 <= th_r < th_c) or n_alarm < 1 or n_reset < 1:
        return None
    sN = lambda lam: _S(q, lam, n_alarm)                       # noqa: E731
    sN1 = lambda lam: _S(q, lam, n_alarm - 1)                  # noqa: E731
    if sN(1.0) < th_c:                                         # тревога недостижима даже при Δt → 0
        return None
    lam_a = _root(sN, th_c) if sN(0.0) < th_c else 0.0         # λ ≥ λ_a: S_N ≥ Θ_crit
    lam_b = 1.0 if sN1(1.0) < th_c else _root(sN1, th_c)       # λ < λ_b: S_{N−1} < Θ_crit
    dN = lambda lam: min(1.0, sN(lam))                         # noqa: E731
    g1 = lambda lam: dN(lam) * lam ** (n_reset - 1)            # noqa: E731
    g = lambda lam: dN(lam) * lam ** n_reset                   # noqa: E731
    if g1(1.0) < th_r:
        return None
    lam_c = _root(g1, th_r) if g1(0.0) < th_r else 0.0         # λ ≥ λ_c: D_N·λ^(M−1) ≥ Θ_reset
    lam_d = _root(g, th_r) if g(1.0) >= th_r else 1.0          # λ < λ_d: D_N·λ^M < Θ_reset
    lo, hi = max(lam_a, lam_c), min(lam_b, lam_d)
    if not (lo < hi) or lo <= 0.0:
        return None
    return (-math.log(hi) if hi < 1.0 else 0.0), -math.log(lo)


def dt_band(q: float, tau: float, **kw) -> "tuple[float, float] | None":
    u = u_band(q, **kw)
    return None if u is None else (tau * u[0], tau * u[1])


def mind_check(tau: float, dt: float, th_c: float = THETA_CRIT_MANUAL, th_r: float = THETA_RESET_MANUAL) -> dict:
    """При заданных τ_D и Δt: диапазон q, дающих тревогу на 4-м сбое, и число успехов до сброса на его концах
    (проверка оговорки 14:51: τ_D 60 с, темп 5,2 с -> ~11–14 успехов)."""
    lam = math.exp(-dt / tau)
    q_lo = th_c / sum(lam ** i for i in range(N_ALARM))
    q_hi = th_c / sum(lam ** i for i in range(N_ALARM - 1))
    r_lo = simulate(q_lo * (1 + 1e-9), tau, dt, th_c, th_r)      # внутрь полуинтервала: без краевого округления
    r_hi = simulate(q_hi * (1 - 1e-9), tau, dt, th_c, th_r)
    return {"tau_D": tau, "dt": dt, "q_alarm4": [round(q_lo, 4), round(q_hi, 4)], "q_alarm4_open_right": True,
            "resets_at_q_ends": [r_lo[1], r_hi[1]], "alarm_at_q_ends": [r_lo[0], r_hi[0]]}


def feasible_tau(dt: float, th_c: float = THETA_CRIT_MANUAL, th_r: float = THETA_RESET_MANUAL) -> dict:
    """Для одного темпа Δt: при каких τ_D (по всем q сетки) выполняются обе цели: τ_D ∈ [Δt/u_hi(q), Δt/u_lo(q))."""
    lo = hi = None
    qs = []
    for q in Q_GRID:
        u = u_band(q, th_c, th_r)
        if u is None:
            continue
        qs.append(q)
        t_lo = dt / u[1]
        t_hi = dt / u[0] if u[0] > 0 else math.inf
        lo = t_lo if lo is None else min(lo, t_lo)
        hi = t_hi if hi is None else max(hi, t_hi)
    return {"dt": dt, "tau_D_min": None if lo is None else round(lo, 2), "tau_D_sup": None if hi is None else round(hi, 2),
            "q_range": [qs[0], qs[-1]] if qs else None}


# ---------------------------------------------------------------- калибровка
def behaviour(q: float, tau: float, points: dict) -> dict:
    return {k: dict(zip(("alarm_at", "reset_after"), simulate(q, tau, v))) | {"dt": round(v, 3)}
            for k, v in points.items()}


def share_in(sorted_dts: list, lo: float, hi: float) -> float:
    if not sorted_dts:
        return 0.0
    i = bisect.bisect_right(sorted_dts, lo)
    j = bisect.bisect_right(sorted_dts, hi)
    return (j - i) / len(sorted_dts)


def calibrate(dts: list, th_c: float = THETA_CRIT_MANUAL, th_r: float = THETA_RESET_MANUAL) -> dict:
    """dts -- Δt внутри сессий (с). -> результат (без значений журнала: только n и квантили)."""
    d = sorted(float(x) for x in dts)
    tempo = sp.summary(d)
    res: dict = {"schema": SCHEMA, "model": "D = min(1, e^(−Δt/τ_D)·D + q·δ); δ any_fail_1",
                 "theta": {"crit": th_c, "reset": th_r}, "targets": {"alarm_at": N_ALARM, "reset_after": N_RESET},
                 "grid": {"q": [Q_GRID[0], Q_GRID[-1], 0.0025], "tau_D_s": [TAU_GRID[0], TAU_GRID[-1], 0.5],
                          "pairs": len(Q_GRID) * len(TAU_GRID)},
                 "tempo_within": tempo, "status": None, "chosen": None, "reason": None}
    ub = {q: u_band(q, th_c, th_r) for q in Q_GRID}
    valid = {q: u for q, u in ub.items() if u is not None}
    ratios = {q: (u[1] / u[0] if u[0] > 0 else math.inf) for q, u in valid.items()}
    qr = max(sorted(ratios), key=lambda q: ratios[q]) if ratios else None
    res["u_band"] = {"q_with_band": len(valid), "q_range": [min(valid), max(valid)] if valid else None,
                     "max_ratio_hi_lo": round(ratios[qr], 4) if qr is not None else None, "q_at_max_ratio": qr,
                     "u_at_max_ratio": [round(valid[qr][0], 5), round(valid[qr][1], 5)] if qr is not None else None}
    # номинал (q = 1 − e^(−Δt_0/τ_D), Δt_0 13 с, τ_D 60 с) и вариант λ_0 (альтернативный) -- справка
    q_nom = 1 - math.exp(-DT0_NOMINAL / TAU_D_MANUAL)
    bn = dt_band(q_nom, TAU_D_MANUAL, th_c=th_c, th_r=th_r)
    res["nominal"] = {"q": round(q_nom, 4), "tau_D": TAU_D_MANUAL, "band_s": [round(bn[0], 3), round(bn[1], 3)] if bn else None}
    us = [i / 2000 for i in range(1, 4000)]
    lam0 = [u for u in us if (lambda b: b is not None and b[0] < u <= b[1])(u_band(1 - math.exp(-u), th_c, th_r))]
    res["lambda0_variant"] = {"rule": "вариант: внутри серии λ_0 = e^(−Δt_0/τ_D) на событие (не по реальному Δt), q = 1 − λ_0",
                              "u0_range": [lam0[0], lam0[-1]] if lam0 else None,
                              "dt0_range_at_tau60_s": [round(60 * lam0[0], 2), round(60 * lam0[-1], 2)] if lam0 else None,
                              "nominal_13s_inside": bool(lam0 and lam0[0] <= DT0_NOMINAL / 60 <= lam0[-1])}
    res["mind_check_tau60"] = {k: mind_check(TAU_D_MANUAL, v, th_c, th_r) for k, v in (("dt_5_2", 5.2), ("dt_9_0", 9.0))}
    res["mind_check_feasible"] = {k: feasible_tau(v, th_c, th_r) for k, v in (("dt_5_2", 5.2), ("dt_9_0", 9.0))}
    if tempo["n"] < MIN_N:
        res["status"] = "STOP"
        res["reason"] = f"NO_DATA: интервалов внутри сессий {tempo['n']} < {MIN_N}"
        return res
    a, b, med = tempo["q10"], tempo["q90"], tempo["median"]
    points = {"q10": a, "median": med, "q90": b}
    res["need_ratio_q90_q10"] = round(b / a, 4) if a > 0 else None
    best = None
    covering = 0
    best_share = None
    per_point: dict = {k: {"pairs": 0, "tau_D": None, "q": None} for k in points}
    for q in Q_GRID:
        u = valid.get(q)
        if u is None:
            continue
        for tau in TAU_GRID:
            lo, hi = tau * u[0], tau * u[1]
            for k, v in points.items():
                if lo < v <= hi:
                    pp = per_point[k]
                    pp["pairs"] += 1
                    pp["tau_D"] = [tau, tau] if pp["tau_D"] is None else [min(pp["tau_D"][0], tau), max(pp["tau_D"][1], tau)]
                    pp["q"] = [q, q] if pp["q"] is None else [min(pp["q"][0], q), max(pp["q"][1], q)]
            sh = share_in(d, lo, hi)
            key = (round(sh, 12), -q, -tau)
            if best_share is None or key > best_share[0]:
                best_share = (key, q, tau, lo, hi, sh)
            if lo < a and hi >= b and a > 0:
                covering += 1
                m = min(math.log(a / lo) if lo > 0 else math.inf, math.log(hi / b))
                k2 = (round(m, 12), -q, -tau)
                if best is None or k2 > best[0]:
                    best = (k2, q, tau, lo, hi)
    res["covering_pairs"] = covering
    res["per_point_feasible"] = per_point
    if best_share is not None:
        _, q, tau, lo, hi, sh = best_share
        res["best_share"] = {"q": q, "tau_D": tau, "band_s": [round(lo, 3), round(hi, 3)], "share_within": round(sh, 4),
                             "behaviour": behaviour(q, tau, points)}
    res["nominal"]["behaviour"] = behaviour(q_nom, TAU_D_MANUAL, points)
    res["nominal"]["share_within"] = round(share_in(d, *bn), 4) if bn else 0.0
    if best is None:
        res["status"] = "STOP"
        res["reason"] = ("нет пары (q, τ_D) на сетке, чья полоса «4-й сбой / 5-й успех» покрывает [q10, q90] темпа внутри "
                         f"сессий: нужно отношение концов {res['need_ratio_q90_q10']}, максимум модели "
                         f"{res['u_band']['max_ratio_hi_lo']}")
        return res
    _, q, tau, lo, hi = best
    res["status"] = "PAIR_CHOSEN"
    res["chosen"] = {"q": q, "tau_D": tau, "band_s": [round(lo, 6), round(hi, 6)], "lower_open": True,
                     "margin_log": round(best[0][0], 6), "share_within": round(share_in(d, lo, hi), 4),
                     "behaviour": behaviour(q, tau, points)}
    return res


# ---------------------------------------------------------------- файл параметров
def read_theta(params: Path) -> "tuple[float, float, float, dict]":
    try:
        doc = json.loads(Path(params).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise InputError(f"params: {type(exc).__name__}") from None
    p = doc.get("params") if isinstance(doc, dict) else None
    try:
        tc, tr, td = (float(p[k]["value"]) for k in ("Theta_crit", "Theta_reset", "tau_D"))
    except (TypeError, KeyError, ValueError):
        raise InputError("params: нет Theta_crit / Theta_reset / tau_D") from None
    if (tc, tr, td) != (THETA_CRIT_MANUAL, THETA_RESET_MANUAL, TAU_D_MANUAL):
        raise InputError("params: Theta_crit / Theta_reset / tau_D не равны ручным 12:04 (0,50 / 0,20 / 60 с)")
    return tc, tr, td, doc


def params_section(res: dict) -> dict:
    c = res["chosen"]
    t = res["tempo_within"]
    return {"status": "CALIBRATED_3A", "rule": "impulse", "formula": res["model"], "q": c["q"], "tau_D": c["tau_D"],
            "Theta_crit": res["theta"]["crit"], "Theta_reset": res["theta"]["reset"], "targets": res["targets"],
            "band_s": c["band_s"], "lower_open": True, "share_within": c["share_within"],
            "tempo_within": {k: t[k] for k in ("n", "q10", "median", "q90")}, "session_gap_s": res.get("session_gap_s"),
            "note": "params.tau_D (60 с, MANUAL 12:04) -- интегратор фазы 2; ядро 3b берёт q, τ_D отсюда; "
                    "calibrate_sigma.py этот раздел не пишет (перезапуск calibrate_sigma его снимет)",
            "source": REF}


def write_params(path: Path, doc: dict, section: dict) -> bytes:
    doc = dict(doc)
    doc["integrator_3a"] = section
    data = (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
    tmp = Path(path).with_name(Path(path).name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data


def render(res: dict) -> str:
    t = res["tempo_within"]
    L = ["# calibrate_integrator (aggregates only; tempo quantiles, model parameters)",
         f"model: {res['model']}; Theta_crit={res['theta']['crit']} Theta_reset={res['theta']['reset']} (MANUAL, unchanged); "
         f"targets: alarm on {N_ALARM}th fail, reset on {N_RESET}th success",
         f"session gap: > {res.get('session_gap_s')} s; tempo within sessions: n={t['n']} q10={t['q10']} "
         f"median={t['median']} q90={t['q90']}",
         f"grid: q {res['grid']['q'][0]}..{res['grid']['q'][1]} step {res['grid']['q'][2]}; tau_D "
         f"{res['grid']['tau_D_s'][0]}..{res['grid']['tau_D_s'][1]} s step {res['grid']['tau_D_s'][2]}; pairs={res['grid']['pairs']}"]
    u = res["u_band"]
    L.append(f"band model: q with band={u['q_with_band']} range={u['q_range']}; max ratio dt_hi/dt_lo={u['max_ratio_hi_lo']} "
             f"at q={u['q_at_max_ratio']} (u band {u['u_at_max_ratio']})")
    if "need_ratio_q90_q10" in res:
        L.append(f"need ratio q90/q10={res['need_ratio_q90_q10']}; covering pairs={res.get('covering_pairs')}")
    for k, v in (res.get("per_point_feasible") or {}).items():
        L.append(f"  dt at {k}: pairs with 4/5={v['pairs']} tau_D range={v['tau_D']} q range={v['q']}")
    n = res["nominal"]
    L.append(f"nominal (q=1-e^(-13/60)={n['q']}, tau_D=60): band_s={n['band_s']} share_within={n.get('share_within')} "
             + " ".join(f"{k}:alarm={b['alarm_at']},reset={b['reset_after']}" for k, b in (n.get("behaviour") or {}).items()))
    for k, m in res["mind_check_tau60"].items():
        L.append(f"mind check tau_D=60 dt={m['dt']}: q for alarm on 4th in [{m['q_alarm4'][0]}, {m['q_alarm4'][1]}) -> "
                 f"resets after {m['resets_at_q_ends'][0]}..{m['resets_at_q_ends'][1]} successes")
    for k, m in res["mind_check_feasible"].items():
        L.append(f"mind check dt={m['dt']}: 4/5 feasible for tau_D in [{m['tau_D_min']}, {m['tau_D_sup']}) with q in {m['q_range']}")
    lv = res["lambda0_variant"]
    L.append(f"lambda_0 variant (alt): u0 range={lv['u0_range']} -> dt0 at tau_D 60: {lv['dt0_range_at_tau60_s']} s; "
             f"nominal 13 s inside={lv['nominal_13s_inside']}")
    if res.get("best_share"):
        bs = res["best_share"]
        L.append(f"best share pair: q={bs['q']} tau_D={bs['tau_D']} band_s={bs['band_s']} share_within={bs['share_within']} "
                 + " ".join(f"{k}:alarm={b['alarm_at']},reset={b['reset_after']}" for k, b in bs["behaviour"].items()))
    if res["chosen"]:
        c = res["chosen"]
        L.append(f"CHOSEN: q={c['q']} tau_D={c['tau_D']} band_s=({c['band_s'][0]}, {c['band_s'][1]}] margin_log={c['margin_log']} "
                 f"share_within={c['share_within']} "
                 + " ".join(f"{k}:alarm={b['alarm_at']},reset={b['reset_after']}" for k, b in c["behaviour"].items()))
    L.append(f"status: {res['status']}" + (f" -- {res['reason']}" if res["reason"] else ""))
    return "\n".join(L)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Joint (q, tau_D) calibration of the impulse dissonance integrator (3a).")
    ap.add_argument("--path", default=str(DEFAULT_PATH))
    ap.add_argument("--params", default=str(DEFAULT_PARAMS))
    ap.add_argument("--out", default=None)
    ap.add_argument("--write-params", default=None, help="write integrator_3a section here ONLY if a pair is chosen")
    a = ap.parse_args(argv)
    try:
        tc, tr, td, doc = read_theta(Path(a.params))
    except InputError as exc:
        sp._out(f"status: INPUT_ERROR ({exc})")
        return EXIT_INPUT
    dts = sp.tempo_values(Path(a.path), td)
    res = calibrate(dts, tc, tr)
    res["session_gap_s"] = td
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_bytes(sp.dumps(res))
    txt = render(res)
    if res["status"] == "PAIR_CHOSEN":
        if a.write_params:
            write_params(Path(a.write_params), doc, params_section(res))
            txt += "\nparams: integrator_3a written"
        sp._out(txt)
        return EXIT_OK
    txt += "\nparams: NOT touched (STOP)"
    sp._out(txt)
    return EXIT_STOP


if __name__ == "__main__":
    raise SystemExit(main())
