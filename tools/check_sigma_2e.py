"""tools/check_sigma_2e.py -- проверка 2e (фаза 2, этап 2e): офлайн decide ядра Σ на отложенной выборке.

Только stdlib, вне src/. Ядро `src/abstraction_layer.py` -- только импорт (не меняется, к боту не подключается).
Параметры -- `config/sigma_params.json` (94d8aa2). Журнал -- `logs/sigma_shadow.jsonl` (+ .1), только чтение,
тем же разбором, что calibrate_sigma.py. Выборка 2e -- команды с seq > holdout_2e.last_seq (решение 16.11 12:04 п.3,
16.11 12:28 п.1). Gate «2e» -- решение 15.11 11:39 «Следствия» п.1.

Что проверяется:
  1. Выборка: команд в расчёте (volume отдельно), по признакам; порог А (>= 30 / >= 10 setting_710 / >= 1 lounge_brightness).
  2. Срез volume (изолирован, в Σ не входит): полный цикл send/ack/readback/outcome, только абсолютные команды
     (volume_set), потолок s_exp <= 15 у всех строк команды.
  3. Сквозной decide с параметрами файла: Θ_intent / Θ_margin / Θ_goal = NO_DATA -> ядро даёт HALT на любом входе.
     Такие отказы -- отдельной строкой («HALT только из-за NO_DATA намерения»). Поэтому ветки проверяются по частям
     (решение 15.11 11:39): ПРОБА = те же параметры, но ветка намерения нейтрализована (Θ_intent = Θ_margin = Θ_goal = 0,
     q_best = 1, q_second = 0, C_goal = 1, Authorization = SafetyGate = 1, бюджет = R_t). Исход ПРОБЫ зависит только от
     веток состояния (C_state против Θ_state) и диссонанса (D_t против Θ_crit, защёлка Θ_reset). Проба -- не калибровка.
  4. Ветка состояния на живых командах: b⁻ = T_θ(·|до) (predict_belief, Δt = 0, как при выводе Θ_state в 2d) или π_0;
     b = update_belief(Z_θ(s_obs|·)); C_state = state_admissibility(A_ε по ε_state, d_Σ = state_distance(α_j, κ_j)).
     Признак вне Σ (нет κ_j / α_j, например lounge_brightness) -> ветка состояния NO_DATA (отдельной строкой).
     Чувствительность: то же с Δt = реальный интервал и τ_b (смесь с π_0) -- сколько команд падает ниже Θ_state.
  5. Ветка диссонанса на живых командах: поток по seq, e_k -- типы ошибок команды (FAIL без err = other), δ_t = Σ β_k e_k,
     Δt -- от outcome прошлой команды, dissonance_step + DissonanceMonitor. Плюс модель «δ = 1 на сбое» (расчёт 12:04).
  6. Синтетика гистерезиса (δ = 1 на сбое, шаг 13 с): 4 сбоя подряд -> OBSERVE; 5 успехов -> D_t < Θ_reset (сброс);
     после тревоги Δt = 1 ч -> затухание и сброс; одиночный сбой -> без тревоги. Отдельно -- β-модель файла (сбой одного
     типа: δ = β_k): достижима ли тревога вообще.
  7. Кейсы полигона (fuzz reports): (а) must_not -- все строки последнего полигона (по умолчанию batch99/100/101);
     (б) violated -- все строки всех батчей с непустым violated; (в) F01-27 до 1c -- строки id F01-27 в батчах с номером < 80
     (batch80 -- первый полигон после «сторож самоидентификации», отчёт 1c). У кейса полигона нет наблюдения ТВ (полигон не пишет журнал Σ) ->
     C_state = 0 (наблюдения нет); сбой кейса (verdict FAIL или violated) -> e_other = 1; D_t -- из D = 0 за шаг 13 с.
     Gate: 0 EXECUTE в ПРОБЕ на (а), (б), (в). Контрфактика (не gate): то же при C_state = 1 -- что держит ветка диссонанса одна.
  8. Файл параметров: у всех 20 параметров есть source (gate фазы 2 по 01 §5: «все пороги с источником»).

Вывод -- ТОЛЬКО счётчики, статусы и доли; значений журнала (s_exp / s_obs / ts / seq), last_seq и текстов полигона
(input / answer / must_not) нет. Синтетические D_t печатаются (это не данные журнала). Детерминирован.
Код возврата: 0 -- gate GREEN; 1 -- gate RED; 2 -- нет / неверный файл параметров или ядра.
Запуск (интерпретатор службы):  <python> tools\\check_sigma_2e.py
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclasses ядра ищут свой модуль в sys.modules
    spec.loader.exec_module(mod)
    return mod


cal = _load("calibrate_sigma_for_check_2e", _HERE / "calibrate_sigma.py")              # тот же разбор журнала и модель
al = cal.al                                                                             # ядро: только импорт (одна копия)
sss = cal.sss

DEFAULT_JOURNAL = ROOT / "logs" / "sigma_shadow.jsonl"
DEFAULT_PARAMS = ROOT / "config" / "sigma_params.json"
DEFAULT_POLY = ROOT / "exchange" / "claude" / "fuzz" / "reports"
DEFAULT_POLY_LATEST = ("batch99_b01_g", "batch100_b02_g", "batch101_b30_g")
EXIT_GREEN, EXIT_RED, EXIT_INPUT = 0, 1, 2

# Порог А (решение 16.11 12:28 п.1)
TH_SAMPLE, TH_SETTING_710, TH_LOUNGE = 30, 10, 1
# Срез volume (решение 16.11 12:28 п.2)
VOLUME_FEATURE = "volume"
VOLUME_CEILING = 15
VOLUME_ABSOLUTE = frozenset({"volume_set"})
FULL_CYCLE = ("send", "ack", "readback", "outcome")
# Синтетика (решение 16.11 12:04 п.1: шаг 13 с, δ = 1 на сбое)
SYN_STEP_S = 13.0
SYN_LONG_GAP_S = 3600.0
SYN_FAILS, SYN_SUCCESSES = 4, 5
INTENT_NO_DATA = frozenset({"NO_DATA:theta_intent", "NO_DATA:theta_margin", "NO_DATA:theta_goal"})
F01_27 = "F01-27"
FIRST_BATCH_AFTER_1C = 80          # batch80_b01_c -- первый полигон на a283f3b (REPORT_2026-11-15_phase1c_oko.md)
_BATCH_RE = re.compile(r"^batch(\d+)_.+_fuzz_report\.jsonl$")
OUTCOMES = ("EXECUTE", "OBSERVE", "CLARIFY", "HALT")


class InputError(ValueError):
    """Нет / неверный файл параметров: код 2."""


# ---------------------------------------------------------------- параметры
def _val(doc: dict, k: str) -> Any:
    p = doc["params"].get(k)
    if not isinstance(p, dict) or p.get("status") in (None, "NO_DATA", "MANUAL_PENDING"):
        return None
    return p.get("value")


def load_params(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise InputError(f"params: {type(exc).__name__}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("params"), dict):
        raise InputError("params: нет объекта params")
    h = doc.get("holdout_2e")
    if not isinstance(h, dict) or not sss._int_ts(h.get("last_seq")):
        raise InputError("params: нет holdout_2e.last_seq")
    for k in ("T_theta", "Z_theta"):
        v = doc["params"].get(k, {}).get("value") if isinstance(doc["params"].get(k), dict) else None
        if isinstance(v, dict) and "states" not in v:
            raise InputError(f"params: {k} без states (нужна схема {cal.SCHEMA}, пересчёт B07h)")
    return doc


def delta_rule(doc: dict) -> str:
    """Правило δ_t файла параметров (решение 16.11 12:59 п.1); нет записи -- прежняя β-модель."""
    d = doc.get("delta_rule")
    r = d.get("rule") if isinstance(d, dict) else None
    return r if r in cal.DELTA_RULES else cal.DELTA_RULE_LEGACY


def kernel_params(doc: dict) -> "al.AbstractionParams":
    """AbstractionParams из файла: числа -- как в файле; NO_DATA -> None (ядро даст HALT)."""
    def num(k):
        v = _val(doc, k)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return al.AbstractionParams(tau_b=num("tau_b"), tau_D=num("tau_D"), tau_h=num("tau_h"), tau_i=num("tau_i"),
                                kappa_space=num("kappa_space"), eps_cov=num("eps_cov"), eps_state=num("eps_state"),
                                theta_state=num("Theta_state"), theta_intent=num("Theta_intent"),
                                theta_margin=num("Theta_margin"), theta_goal=num("Theta_goal"),
                                theta_crit=num("Theta_crit"), theta_reset=num("Theta_reset"))


def probe_params(p: "al.AbstractionParams") -> "al.AbstractionParams":
    """ПРОБА: ветка намерения нейтрализована (0), остальные параметры -- как в файле."""
    return dataclasses.replace(p, theta_intent=0.0, theta_margin=0.0, theta_goal=0.0)


def budget(doc: dict) -> int:
    r = _val(doc, "R_t")
    if not isinstance(r, dict):
        return 0
    return sum(v for v in r.values() if isinstance(v, int) and not isinstance(v, bool))


def betas(doc: dict) -> "tuple[tuple, tuple]":
    b = _val(doc, "beta_k")
    if not isinstance(b, dict) or not b:
        return (), ()
    keys = tuple(sorted(b))
    return keys, tuple(float(b[k]) for k in keys)


def sourced(doc: dict) -> "tuple[int, int, list]":
    names = list(cal.PARAMS)
    bad = [k for k in names if not isinstance(doc["params"].get(k), dict)
           or not doc["params"][k].get("source") or doc["params"][k].get("status") == "MANUAL_PENDING"]
    return len(names), len(names) - len(bad), bad


# ---------------------------------------------------------------- выборка 2e
def sample(journal: Path, last_seq: int) -> dict:
    """Команды 2e (seq > last_seq): Σ-команды (без изолированных) и срез volume -- по строкам журнала."""
    rows, st, _ = cal.load_rows(Path(journal), {})                 # все строки, без изоляции
    new_rows = [r for r in rows if sss._int_ts(r.get("seq")) and r["seq"] > last_seq]
    cmds = [c for c in cal.build_commands(new_rows)]
    iso = set(sss.ISOLATE_DEFAULT)
    iso_seqs = sss._isolated_seqs(new_rows, iso)
    sig = [c for c in cmds if c["seq"] not in iso_seqs]
    vol_seqs = sorted({r["seq"] for r in new_rows if r["seq"] in iso_seqs and r.get("feature") == VOLUME_FEATURE}
                      | {c["seq"] for c in cmds if c["feature"] == VOLUME_FEATURE})
    by_seq: dict = {}
    for r in new_rows:
        by_seq.setdefault(r["seq"], []).append(r)
    cal.link_commands(sig)      # «до» и реальный Δt (ρ_t) -- в потоке Σ-команд 2e, тем же правилом, что в калибровке
    return {"lines_total": st["lines_total"], "lines_bad": st["lines_bad"], "rows_new": len(new_rows),
            "commands_all": len({r["seq"] for r in new_rows}), "sigma": sig,
            "volume": [(s, by_seq.get(s, [])) for s in vol_seqs],
            "no_outcome": len({r["seq"] for r in new_rows} - {c["seq"] for c in cmds})}


def volume_check(vol: list) -> dict:
    out = {"commands": len(vol), "full_cycle": 0, "absolute": 0, "relative": 0, "ceiling_ok": 0, "no_target": 0,
           "outcome": Counter()}
    for _, rs in vol:
        steps = {r["step"] for r in rs}
        if all(s in steps for s in FULL_CYCLE):
            out["full_cycle"] += 1
        acts = {r.get("action") for r in rs if r.get("action") is not None}
        if acts and acts <= VOLUME_ABSOLUTE:
            out["absolute"] += 1
        else:
            out["relative"] += 1
        tg = [r["s_exp"] for r in rs if sss._is_num(r.get("s_exp"))]
        if not tg:
            out["no_target"] += 1
        elif all(0 <= v <= VOLUME_CEILING for v in tg):
            out["ceiling_ok"] += 1
        oc = [r.get("outcome") for r in rs if r["step"] == "outcome"]
        out["outcome"][oc[-1] if oc else "none"] += 1
    n = out["commands"]
    out["ok"] = n > 0 and out["full_cycle"] == n and out["absolute"] == n and out["ceiling_ok"] == n
    return out


# ---------------------------------------------------------------- ветка состояния
def model_view(doc: dict) -> dict:
    return cal.view_from_values(_val(doc, "T_theta"), _val(doc, "Z_theta"), _val(doc, "pi_0"))


def c_state(doc: dict, p: "al.AbstractionParams", c: dict, dt_s: "float | None" = None,
            view: "dict | None" = None) -> "tuple[float | None, str]":
    """C_state команды функциями ядра той же моделью, что Θ_state в калибровке (calibrate_sigma.c_state):
    b⁻ = ρ_t·T_θ(·|до) + (1 − ρ_t)·π_0 с реальным Δt (dt_s -- подмена Δt для чувствительности), Z_θ со сглаживанием.
    -> (C_state, статус): OK / OBS_UNSEEN / PRIOR_ZERO / NO_DATA_FEATURE / NO_PAIR / NO_PRIOR."""
    alpha = _val(doc, "alpha_j") if isinstance(_val(doc, "alpha_j"), dict) else None
    kappa = _val(doc, "kappa_j") if isinstance(_val(doc, "kappa_j"), dict) else None
    return cal.c_state(view or model_view(doc), p.eps_state, alpha, kappa, c, p.tau_b, dt_override=dt_s)


def tolerance_divisions(doc: dict, p: "al.AbstractionParams", f: str) -> "float | None":
    """Допуск A_ε признака в делениях шкалы: d_Σ = α·min(1, d/κ) <= ε -> d <= ε·κ/α (если ε/α < 1)."""
    a = (_val(doc, "alpha_j") or {}).get(f) if isinstance(_val(doc, "alpha_j"), dict) else None
    k = (_val(doc, "kappa_j") or {}).get(f) if isinstance(_val(doc, "kappa_j"), dict) else None
    if not a or not k or p.eps_state is None or p.eps_state / a >= 1:
        return None
    return p.eps_state / a * k


# ---------------------------------------------------------------- ветка диссонанса
def err_vector(keys: tuple, err_types: "list | set") -> tuple:
    return tuple(1.0 if k in err_types else 0.0 for k in keys)


def dissonance_stream(p, keys: tuple, bvals: tuple, events: list, delta_one: bool = False,
                      rule: "str | None" = None) -> dict:
    """events: [(dt_s, failed: bool, err_types)] -> D_t по шагам ядром. delta_one: δ = 1 на сбое (модель 12:04);
    rule: правило δ_t файла параметров (calibrate_sigma.delta_vector; any_fail_1 = то же, что delta_one)."""
    rule = rule or ("any_fail_1" if delta_one else cal.DELTA_RULE_LEGACY)
    mon = al.DissonanceMonitor(p)
    d, ds, alarms, kmax, decay_gaps, first_alarm = 0.0, [], 0, 0, 0, None
    for i, (dt, failed, errs) in enumerate(events):
        e, b = cal.delta_vector(rule, failed, errs, keys, bvals)
        if dt > 10 * p.tau_D:
            decay_gaps += 1
        d = al.dissonance_step(d, e, b, dt, p.tau_D)
        was = mon.alarm
        a = mon.update(d)
        if a and not was:
            alarms += 1
            if first_alarm is None:
                first_alarm = i + 1
        kmax = max(kmax, mon.k_high)
        ds.append((d, a))
    return {"steps": len(events), "alarms": alarms, "first_alarm": first_alarm, "k_high_max": kmax,
            "nonzero": sum(1 for x, _ in ds if x > 0), "gaps_gt_10tau": decay_gaps, "trace": ds,
            "alarm_end": mon.alarm}


def synthetic(p, keys: tuple, bvals: tuple, rule: "str | None" = None) -> dict:
    """Гистерезис на синтетике функциями ядра (δ = 1 на сбое, шаг 13 с), + β-модель файла,
    + правило δ_t файла (rule): достижима ли тревога (4 сбоя -> тревога, 5 успехов -> сброс)."""
    out: dict = {}
    seq = [(SYN_STEP_S, True, ["other"])] * SYN_FAILS + [(SYN_STEP_S, False, [])] * SYN_SUCCESSES
    s1 = dissonance_stream(p, keys, bvals, seq, delta_one=True)
    tr = s1["trace"]
    reset_at = next((i - SYN_FAILS + 1 for i in range(SYN_FAILS, len(tr)) if not tr[i][1]), None)
    out["fail4"] = {"alarm_at_fail": s1["first_alarm"], "D_at_4": tr[SYN_FAILS - 1][0],
                    "alarm_before_4": any(a for _, a in tr[:SYN_FAILS - 1]), "reset_at_success": reset_at,
                    "D_at_reset": tr[SYN_FAILS + reset_at - 1][0] if reset_at else None,
                    "D_success_4": tr[SYN_FAILS + 3][0]}
    # decide на синтетике (ПРОБА, C_state = 1): тревога -> OBSERVE (DISSONANCE); после сброса -> EXECUTE
    pp = probe_params(p)
    dec = [_decide(pp, 1.0, d, a, 8).outcome.value for d, a in tr]
    out["fail4"]["decide_at_4"] = dec[SYN_FAILS - 1]
    out["fail4"]["decide_before_4"] = sorted(set(dec[:SYN_FAILS - 1]))
    out["fail4"]["decide_after_reset"] = dec[SYN_FAILS + reset_at - 1] if reset_at else None
    s2 = dissonance_stream(p, keys, bvals, [(SYN_STEP_S, True, ["other"])], delta_one=True)
    out["single"] = {"D": s2["trace"][0][0], "alarm": s2["alarms"] > 0}
    s3 = dissonance_stream(p, keys, bvals, seq[:SYN_FAILS] + [(SYN_LONG_GAP_S, False, [])], delta_one=True)
    s3b = dissonance_stream(p, keys, bvals, seq[:SYN_FAILS] + [(SYN_LONG_GAP_S, True, ["other"])], delta_one=True)
    out["long_gap"] = {"D_after_gap": s3["trace"][-1][0], "reset": not s3["alarm_end"],
                       "D_fail_after_gap": s3b["trace"][-1][0]}
    # β-модель файла: сбой одного типа -> δ = β_k; предел D_t при бесконечной серии = max β_k
    bmax = max(bvals) if bvals else 0.0
    s4 = dissonance_stream(p, keys, bvals, [(SYN_STEP_S, True, ["other"])] * 50)
    need = None
    if bvals:
        acc, srt = 0.0, sorted(bvals, reverse=True)
        for i, b in enumerate(srt):
            acc += b
            if acc >= p.theta_crit:
                need = i + 1
                break
    out["beta"] = {"delta_one_type": bmax, "alarms_50_fails": s4["alarms"], "D_after_50": s4["trace"][-1][0],
                   "reachable_one_type": bmax >= p.theta_crit, "types_needed": need, "types_total": len(bvals)}
    f4 = out["fail4"]
    out["ok"] = (f4["alarm_at_fail"] == SYN_FAILS and not f4["alarm_before_4"] and f4["reset_at_success"] == SYN_FAILS + 1
                 and f4["decide_at_4"] == "OBSERVE" and f4["decide_after_reset"] == "EXECUTE"
                 and not out["single"]["alarm"] and out["long_gap"]["reset"])
    out["long_gap"]["alarm_fail_after_gap"] = bool(s3b["alarm_end"])
    r = rule or cal.DELTA_RULE_LEGACY
    sr = dissonance_stream(p, keys, bvals, seq, rule=r)
    trr = sr["trace"]
    rr = next((i - SYN_FAILS + 1 for i in range(SYN_FAILS, len(trr)) if not trr[i][1]), None) if sr["alarms"] else None
    out["rule"] = {"rule": r, "alarm_at_fail": sr["first_alarm"], "D_at_4": trr[SYN_FAILS - 1][0], "reset_at_success": rr,
                   "ok": sr["first_alarm"] == SYN_FAILS and rr == SYN_FAILS + 1}
    return out


# ---------------------------------------------------------------- решение
def _decide(p, cs: float, d_t: float, alarm: bool, r: int, intent_open: bool = True) -> "al.Decision":
    if intent_open:
        x = al.DecisionInputs(c_state=cs, q_best=1.0, q_second=0.0, c_goal=1.0, d_t=d_t, authorization=1,
                              safety_gate=1, budget_remaining=r, dissonance_alarm=alarm)
    else:  # сквозной: оценок намерения нет -> нули (ядро всё равно даст HALT по NO_DATA порога)
        x = al.DecisionInputs(c_state=cs, q_best=0.0, q_second=0.0, c_goal=0.0, d_t=d_t, authorization=1,
                              safety_gate=1, budget_remaining=r, dissonance_alarm=alarm)
    return al.decide(p, x)


def _tally(decs: list) -> dict:
    c = Counter(d.outcome.value for d in decs)
    intent_only = sum(1 for d in decs if d.outcome is al.Outcome.HALT and d.reasons and set(d.reasons) <= INTENT_NO_DATA)
    reasons = Counter(r for d in decs for r in d.reasons)
    return {"n": len(decs), "by": {o: c.get(o, 0) for o in OUTCOMES}, "halt_intent_only": intent_only,
            "reasons": dict(sorted(reasons.items()))}


CS_BUCKETS = ((0.0, 0.5), (0.5, 0.9), (0.9, 1.0))     # корзины C_state ложных OBSERVE (только счёт)


def _bucket(v: float) -> str:
    for lo, hi in CS_BUCKETS:
        if lo <= v < hi:
            return f"[{lo:.1f},{hi:.1f})"
    return "[1.0]"


def live(doc: dict, p, smp: dict) -> dict:
    keys, bvals = betas(doc)
    rule = delta_rule(doc)
    pp = probe_params(p)
    r = budget(doc)
    view = model_view(doc)
    cmds = sorted(smp["sigma"], key=lambda c: c["seq"])
    events, prev_out = [], None
    for c in cmds:
        dt = math.inf if prev_out is None else max(0.0, (c["first_ts"] - prev_out) / 1000.0)
        failed = c["outcome"] == "FAIL" or bool(c["err_types"])
        events.append((dt, failed, c["err_types"]))
        prev_out = c["out_ts"]
    dis = dissonance_stream(p, keys, bvals, events) if keys else None          # β-модель (для сравнения)
    dis1 = dissonance_stream(p, keys, bvals, events, delta_one=True)           # δ = 1 на сбое
    dis_rule = dissonance_stream(p, keys, bvals, events, rule=rule) if (keys or rule != cal.DELTA_RULE_LEGACY) else None
    st_status, st_ok, sens_drop, sens_rise, kernel_err = Counter(), 0, 0, 0, 0
    unseen: set = set()          # различные считанные значения вне S_f / с Z = 0 (только число -- в вывод)
    full, probe, probe_by_feat = [], [], {}
    false_obs = {"n_good": 0, "observe": 0, "by_feature": Counter(), "reasons": Counter(), "buckets": Counter()}
    dev = {"n": 0, "detected": 0, "by_feature": Counter(), "detected_by_feature": Counter(), "no_tolerance": 0}
    for i, c in enumerate(cmds):
        try:
            cs, status = c_state(doc, p, c, view=view)
        except Exception as exc:  # noqa: BLE001 -- ошибка ядра -> HALT (01 §8.2), считается
            cs, status = None, "KERNEL_ERROR:" + type(exc).__name__
            kernel_err += 1
        st_status[status] += 1
        if status in ("OBS_UNSEEN", "PRIOR_ZERO"):
            unseen.add((c["feature"], cal._key(c["s_obs"])))
        d_t, alarm = dis_rule["trace"][i] if dis_rule else (0.0, False)
        if cs is not None and p.theta_state is not None and cs >= p.theta_state:
            st_ok += 1
        if cs is not None and status == "OK" and c.get("before") is not None:
            try:   # чувствительность: тот же расчёт при Δt = 0 (как Θ_state в 2d-v2)
                cs0, _ = c_state(doc, p, c, dt_s=0.0, view=view)
                if cs0 is not None and p.theta_state is not None and cs < p.theta_state <= cs0:
                    sens_drop += 1     # реальный Δt опустил ниже порога
                if cs0 is not None and p.theta_state is not None and cs0 < p.theta_state <= cs:
                    sens_rise += 1     # реальный Δt поднял до порога
            except Exception:  # noqa: BLE001
                kernel_err += 1
        full.append(_decide(p, cs if cs is not None else 0.0, d_t, alarm, r, intent_open=False))
        if cs is None:
            continue   # ветка состояния NO_DATA -- ПРОБА не запускается (отдельной строкой)
        dpr = _decide(pp, cs, d_t, alarm, r)
        probe.append(dpr)
        probe_by_feat.setdefault(c["feature"], []).append(dpr)
        if c["outcome"] == "PASS" and not c["err_types"]:          # хорошая команда: OBSERVE = ложный
            false_obs["n_good"] += 1
            if dpr.outcome is al.Outcome.OBSERVE:
                false_obs["observe"] += 1
                false_obs["by_feature"][sss._code(c["feature"])] += 1
                for rs in dpr.reasons:
                    false_obs["reasons"][rs] += 1
                if "STATE" in dpr.reasons:
                    false_obs["buckets"][_bucket(cs)] += 1
            # синтетическое отклонение: s_obs = s_exp ± (допуск A_ε + 1 деление) -- ветка состояния должна дать OBSERVE
            tol = tolerance_divisions(doc, p, c["feature"])
            if tol is None:
                dev["no_tolerance"] += 1
                continue
            states = cal.states_of(view["spaces"][c["feature"]]) if c["feature"] in view["spaces"] else []
            k = math.floor(tol) + 1
            for sgn in (1, -1):
                o = c["s_exp"] + sgn * k
                if cal._key(o) not in states:
                    continue
                try:
                    csd, _ = c_state(doc, p, dict(c, s_obs=o), view=view)
                except Exception:  # noqa: BLE001
                    kernel_err += 1
                    continue
                dev["n"] += 1
                dev["by_feature"][sss._code(c["feature"])] += 1
                if _decide(pp, csd, 0.0, False, r).outcome is al.Outcome.OBSERVE:
                    dev["detected"] += 1
                    dev["detected_by_feature"][sss._code(c["feature"])] += 1
    feats = Counter(c["feature"] for c in cmds)
    return {"n": len(cmds), "features": dict(sorted((sss._code(k), v) for k, v in feats.items())),
            "outcomes": dict(Counter(c["outcome"] for c in cmds)),
            "errors": sum(1 for e in events if e[1]), "state_status": dict(sorted(st_status.items())),
            "state_ok": st_ok, "sens_drop": sens_drop, "sens_rise": sens_rise, "unseen_values": len(unseen), "kernel_err": kernel_err,
            "dis": dis, "dis1": dis1, "dis_rule": dis_rule, "rule": rule, "full": _tally(full), "probe": _tally(probe),
            "probe_by_feat": {sss._code(f): _tally(v) for f, v in sorted(probe_by_feat.items())},
            "false_obs": false_obs, "deviation": dev}


# ---------------------------------------------------------------- полигон
def poly_cases(poly_dir: Path, latest: tuple) -> "tuple[dict, dict]":
    """-> (наборы кейсов, сводка файлов). Кейс = (failed: bool,). Тексты полигона не сохраняются."""
    sets = {"must_not": [], "violated": [], "f01_27_pre1c": []}
    info = {"files": 0, "bad_lines": 0, "latest_found": [], "latest_missing": [], "pre1c_batches": 0}
    if not Path(poly_dir).is_dir():
        info["absent"] = True
        return sets, info
    pre1c = set()
    for fp in sorted(Path(poly_dir).iterdir(), key=lambda x: x.name):
        m = _BATCH_RE.match(fp.name)
        if not m:
            continue
        info["files"] += 1
        num = int(m.group(1))
        stem = fp.name[: -len("_fuzz_report.jsonl")]
        try:
            lines = fp.read_bytes().splitlines()
        except OSError:
            info["bad_lines"] += 1
            continue
        for b in lines:
            if not b.strip():
                continue
            try:
                row = json.loads(b.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                info["bad_lines"] += 1
                continue
            if not isinstance(row, dict):
                info["bad_lines"] += 1
                continue
            viol = row.get("violated")
            failed = row.get("verdict") == "FAIL" or (isinstance(viol, list) and len(viol) > 0)
            case = {"failed": failed}
            if stem in latest and row.get("must_not"):
                sets["must_not"].append(case)
            if isinstance(viol, list) and viol:
                sets["violated"].append(case)
            if row.get("id") == F01_27 and num < FIRST_BATCH_AFTER_1C:
                sets["f01_27_pre1c"].append(case)
                pre1c.add(num)
        if stem in latest:
            info["latest_found"].append(stem)
    info["latest_missing"] = [b for b in latest if b not in info["latest_found"]]
    info["pre1c_batches"] = len(pre1c)
    return sets, info


def poly_check(doc: dict, p, sets: dict) -> dict:
    keys, bvals = betas(doc)
    rule = delta_rule(doc)
    pp = probe_params(p)
    r = budget(doc)
    out = {}
    for name, cases in sets.items():
        full, probe, cf, cf1, alarms = [], [], [], [], 0
        for c in cases:
            errs = ["other"] if c["failed"] else []
            d = al.dissonance_step(0.0, err_vector(keys, errs), bvals, SYN_STEP_S, p.tau_D) if keys else 0.0
            d1 = al.dissonance_step(0.0, (1.0 if c["failed"] else 0.0,), (1.0,), SYN_STEP_S, p.tau_D)
            er, br = cal.delta_vector(rule, c["failed"], errs, keys, bvals)
            dr = al.dissonance_step(0.0, er, br, SYN_STEP_S, p.tau_D) if br else 0.0
            mon = al.DissonanceMonitor(p)
            a = mon.update(d)
            mon1 = al.DissonanceMonitor(p)
            a1 = mon1.update(d1)
            ar = al.DissonanceMonitor(p).update(dr)
            alarms += int(a) + int(a1) + int(ar)
            full.append(_decide(p, 0.0, dr, ar, r, intent_open=False))
            probe.append(_decide(pp, 0.0, dr, ar, r))           # нет наблюдения ТВ -> C_state = 0; δ по правилу файла
            cf.append(_decide(pp, 1.0, d, a, r))                # контрфактика: состояние «совпало»
            cf1.append(_decide(pp, 1.0, d1, a1, r))
        out[name] = {"n": len(cases), "failed": sum(1 for c in cases if c["failed"]), "full": _tally(full),
                     "probe": _tally(probe), "cf_beta": _tally(cf), "cf_delta1": _tally(cf1), "alarms": alarms}
    return out


# ---------------------------------------------------------------- отчёт
def _pct(k: int, n: int) -> str:
    return f"{k}/{n}" + (f" ({100.0 * k / n:.1f}%)" if n else "")


def _shares(t: dict) -> str:
    n = t["n"]
    return " ".join(f"{o}={_pct(t['by'][o], n)}" for o in OUTCOMES)


def run(journal: Path, params_path: Path, poly_dir: Path, latest: tuple) -> "tuple[int, list]":
    L: list = ["# check_sigma_2e (counters only; no s_exp/s_obs/ts/seq values, no last_seq, no polygon texts)"]
    try:
        doc = load_params(params_path)
        p = kernel_params(doc)
    except (InputError, ValueError) as exc:
        L.append(f"status: INPUT_ERROR ({exc})")
        return EXIT_INPUT, L
    if p.tau_D is None or p.theta_crit is None or p.theta_reset is None:
        L.append("status: INPUT_ERROR (tau_D / Theta_crit / Theta_reset NO_DATA in params)")
        return EXIT_INPUT, L
    last_seq = doc["holdout_2e"]["last_seq"]
    n_par, n_src, no_src = sourced(doc)
    L.append(f"params: schema={doc.get('schema')} params={n_par} with source={n_src}"
             + (f" missing={','.join(no_src)}" if no_src else "")
             + " | kernel NO_DATA: " + ",".join(p.missing()))
    used = ("tau_b", "tau_D", "eps_state", "Theta_state", "Theta_crit", "Theta_reset", "alpha_j", "kappa_j", "beta_k",
            "T_theta", "Z_theta", "pi_0", "R_t")
    L.append("params used: " + " ".join(f"{k}={doc['params'].get(k, {}).get('status')}" for k in used)
             + f"; R_t={budget(doc)}; beta_k types={len(betas(doc)[0])}")

    smp = sample(journal, last_seq)
    lv = live(doc, p, smp)
    f = lv["features"]
    n710, nlb = f.get("setting_710", 0), f.get("lounge_brightness", 0)
    th_ok = lv["n"] >= TH_SAMPLE and n710 >= TH_SETTING_710 and nlb >= TH_LOUNGE
    L.append(f"journal: lines total={smp['lines_total']} bad={smp['lines_bad']}; 2e rows={smp['rows_new']} "
             f"commands={smp['commands_all']} (without outcome={smp['no_outcome']})")
    L.append(f"sample 2e (seq > last_seq): in Sigma={lv['n']} by feature: "
             + " ".join(f"{k}={v}" for k, v in f.items()) + "; outcomes: "
             + " ".join(f"{k}={v}" for k, v in sorted(lv["outcomes"].items())) + f"; error commands={lv['errors']}")
    L.append(f"threshold A (>= {TH_SAMPLE} / setting_710 >= {TH_SETTING_710} / lounge_brightness >= {TH_LOUNGE}): {th_ok}")
    v = volume_check(smp["volume"])
    L.append(f"volume (isolated, not in Sigma): commands={v['commands']} full_cycle={v['full_cycle']} absolute={v['absolute']} "
             f"relative={v['relative']} ceiling<={VOLUME_CEILING} ok={v['ceiling_ok']} no_target={v['no_target']} outcome: "
             + " ".join(f"{k}={n}" for k, n in sorted(v["outcome"].items())) + f" -> {('OK' if v['ok'] else 'FAIL')}")

    L.append("--- end-to-end decide (params file as is; intent NO_DATA)")
    t = lv["full"]
    L.append(f"live: {_shares(t)}; HALT only due to NO_DATA intent (Theta_intent/margin/goal) = {_pct(t['halt_intent_only'], t['n'])}")
    e2e_always = t["n"] > 0 and t["halt_intent_only"] == t["n"]
    L.append("  => end-to-end decide is ALWAYS non-EXECUTE (HALT NO_DATA intent): " + str(e2e_always)
             + "; branches are checked by parts (decision 15.11 11:39) -- PROBE below")

    L.append("--- state branch (C_state vs Theta_state), live")
    ts_p = doc["params"].get("Theta_state", {})
    loo = (ts_p.get("source") or {}).get("loo_note") or {}
    L.append(f"  model: T_theta/Z_theta/pi_0 smoothed (Dirichlet mass 1/row; Z offset back-off); b- with real Δt (rho_t, tau_b);"
             f" Theta_state={ts_p.get('status')}"
             + (" (Operator 16.11 13:43)" if ts_p.get("status") == "MANUAL" else " (min C_state on 2d PASS, leave-one-out)")
             + f"; delta_rule={lv['rule']}")
    if loo:
        L.append(f"  LOO record of Theta_state (2d, not the threshold): n={loo.get('n')} excluded={loo.get('excluded')} "
                 + " ".join(f"{k}={loo[k]:.4f}" for k in ("min", "q05", "q10", "median", "max") if isinstance(loo.get(k), (int, float)))
                 + f" guard={'ok' if loo.get('guard') == 'ok' else 'see loo_note'}")
    L.append("  status: " + " ".join(f"{k}={n}" for k, n in lv["state_status"].items())
             + f"; C_state >= Theta_state: {_pct(lv['state_ok'], lv['n'])}; kernel errors={lv['kernel_err']}")
    L.append("  OBS_UNSEEN = s_obs outside the state space / Z=0; PRIOR_ZERO = sum Z*b- = 0 (with smoothing: not expected);"
             f" distinct (feature, s_obs) among them={lv['unseen_values']}")
    L.append(f"  sensitivity vs Δt=0 (2d-v2 way): real Δt dropped below Theta_state={lv['sens_drop']}, "
             f"raised to >= Theta_state={lv['sens_rise']}")
    L.append("--- dissonance branch (D_t vs Theta_crit/Theta_reset, tau_D), live")
    for nm, d in ((f"file rule ({lv['rule']})", lv["dis_rule"]), ("beta_k model", lv["dis"]), ("delta=1 model", lv["dis1"])):
        if d is None:
            L.append(f"  {nm}: beta_k NO_DATA")
            continue
        L.append(f"  {nm}: steps={d['steps']} D_t>0 steps={d['nonzero']} alarms={d['alarms']} K_high max={d['k_high_max']} "
                 f"gaps > 10*tau_D={d['gaps_gt_10tau']}")
    L.append("--- PROBE (intent branch neutralized; outcome = state + dissonance (file rule) branches only), live")
    t = lv["probe"]
    L.append(f"  live evaluated={t['n']} (state NO_DATA not evaluated: {lv['n'] - t['n']}): {_shares(t)}")
    for ft, tt in lv["probe_by_feat"].items():
        L.append(f"    {ft}: {_shares(tt)} reasons=" + (",".join(f"{k}:{n}" for k, n in tt["reasons"].items()) or "-"))
    fo = lv["false_obs"]
    L.append(f"  false OBSERVE on good commands (PASS, no err): {_pct(fo['observe'], fo['n_good'])}; by feature: "
             + (" ".join(f"{k}={n}" for k, n in sorted(fo["by_feature"].items())) or "-")
             + "; reasons: " + (",".join(f"{k}:{n}" for k, n in sorted(fo["reasons"].items())) or "-")
             + "; C_state of STATE ones: " + (" ".join(f"{k}={n}" for k, n in sorted(fo["buckets"].items())) or "-")
             + " (B07g: 12/26 setting_710)")
    dv = lv["deviation"]
    L.append(f"  synthetic deviation on good commands (observed = expected +/- (A_eps tolerance + 1 division)): state branch "
             f"OBSERVE {_pct(dv['detected'], dv['n'])}; by feature: "
             + (" ".join(f"{k}={dv['detected_by_feature'][k]}/{n}" for k, n in sorted(dv["by_feature"].items())) or "-")
             + (f"; no tolerance (eps/alpha >= 1)={dv['no_tolerance']}" if dv["no_tolerance"] else ""))

    L.append("--- synthetic hysteresis (kernel dissonance_step + DissonanceMonitor; delta=1 per failure, step 13 s)")
    keys, bvals = betas(doc)
    sy = synthetic(p, keys, bvals, lv["rule"])
    f4 = sy["fail4"]
    L.append(f"  4 failures: alarm at failure #{f4['alarm_at_fail']} (D={f4['D_at_4']:.3f}; before #4: {f4['alarm_before_4']}); "
             f"decide PROBE at #4={f4['decide_at_4']} (before: {','.join(f4['decide_before_4'])})")
    L.append(f"  then successes: D<Theta_reset at success #{f4['reset_at_success']} (D={f4['D_at_reset']:.3f}; "
             f"at #4 D={f4['D_success_4']:.3f}); decide PROBE after reset={f4['decide_after_reset']}")
    L.append(f"  single failure: D={sy['single']['D']:.3f} alarm={sy['single']['alarm']}")
    lg = sy["long_gap"]
    L.append(f"  after alarm, gap {int(SYN_LONG_GAP_S)} s: D={lg['D_after_gap']:.3g} reset={lg['reset']} (decay)")
    L.append(f"  KNOWN DEFECT (phase 3; not gate): one failure after gap {int(SYN_LONG_GAP_S)} s: "
             f"D={lg['D_fail_after_gap']:.3f} alarm={lg['alarm_fail_after_gap']} (lambda->0 => D=delta)")
    b = sy["beta"]
    L.append(f"  beta_k model (record only): one-type failure delta={b['delta_one_type']:.3f}; 50 failures in a row -> "
             f"alarms={b['alarms_50_fails']} D={b['D_after_50']:.3f}; alarm reachable with one type={b['reachable_one_type']}; "
             f"types needed in one step={b['types_needed']} of {b['types_total']}")
    sr = sy["rule"]
    L.append(f"  file delta_rule={sr['rule']}: 4 failures -> alarm at failure #{sr['alarm_at_fail']} (D={sr['D_at_4']:.3f}); "
             f"reset at success #{sr['reset_at_success']}; reachable: {sr['ok']}")
    L.append(f"  synthetic expectations (4 -> OBSERVE, 5 -> reset, gap -> decay, single -> no alarm): {sy['ok']}")

    L.append("--- polygon cases (no TV observation -> C_state=0; failed case -> delta by file rule, D from 0 in 13 s)")
    sets, info = poly_cases(poly_dir, latest)
    if info.get("absent"):
        L.append("  polygon reports dir: absent")
    L.append(f"  reports: files={info['files']} bad lines={info['bad_lines']} latest={','.join(info['latest_found']) or '-'}"
             + (f" MISSING={','.join(info['latest_missing'])}" if info["latest_missing"] else "")
             + f"; F01-27 pre-1c batches={info['pre1c_batches']}")
    pc = poly_check(doc, p, sets)
    probe_exec = 0
    for name, d in pc.items():
        probe_exec += d["probe"]["by"]["EXECUTE"]
        L.append(f"  [{name}] cases={d['n']} failed={d['failed']} alarms={d['alarms']}")
        L.append(f"    end-to-end: {_shares(d['full'])}; HALT only NO_DATA intent={_pct(d['full']['halt_intent_only'], d['n'])}")
        L.append(f"    PROBE:      {_shares(d['probe'])} reasons=" + (",".join(f"{k}:{n}" for k, n in d['probe']['reasons'].items()) or "-"))
        L.append(f"    counterfactual C_state=1 (not gate): beta_k {_shares(d['cf_beta'])} | delta=1 {_shares(d['cf_delta1'])}")
    sets_ok = all(pc[k]["n"] > 0 for k in pc) and not info["latest_missing"]

    gate = {"params with source": n_src == n_par, "sample threshold A": th_ok, "volume slice": v["ok"],
            "kernel errors 0": lv["kernel_err"] == 0, "synthetic hysteresis": sy["ok"],
            "alarm reachable under file delta_rule": sr["ok"],
            "polygon sets found": sets_ok, "0 EXECUTE on must_not/violated/F01-27 (PROBE)": probe_exec == 0}
    L.append("--- findings (not gate; for review)")
    L.append(f"  F1 delta_rule={lv['rule']}: alarm reachable={sr['ok']} (beta_k record: one-type delta={b['delta_one_type']:.3f})")
    L.append(f"  F2 long gap (known defect, phase 3): first failure after gap > 10*tau_D gives D=delta "
             f"(alarm={lg['alarm_fail_after_gap']})")
    L.append(f"  F3 false OBSERVE on good commands: {_pct(fo['observe'], fo['n_good'])} (B07g: 12/26); reasons="
             + (",".join(f"{k}:{n}" for k, n in sorted(fo["reasons"].items())) or "-")
             + f"; deviation detection {_pct(dv['detected'], dv['n'])}")
    L.append("  F4 polygon: counterfactual C_state=1 (delta=1) -> EXECUTE: "
             + " ".join(f"{k}={pc[k]['cf_delta1']['by']['EXECUTE']}/{pc[k]['n']} (failed {pc[k]['failed']})" for k in pc)
             + " -- a single bad case is stopped by the state branch only (gate is formal: decision 16.11 12:59 p.4)")
    L.append("--- gate 2e")
    for k, ok in gate.items():
        L.append(f"  {k}: {ok}")
    L.append("  note: e2e HALT is by NO_DATA intent (phase 3); polygon 0 EXECUTE is held by the state branch (no TV observation)")
    green = all(gate.values())
    L.append(f"status: {'GREEN' if green else 'RED'}")
    return (EXIT_GREEN if green else EXIT_RED), L


def _out(s: str) -> None:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "").replace("_", "")
    if enc != "utf8" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.write(s + "\n")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 2e check: offline decide on new journal commands. Counters-only stdout.")
    ap.add_argument("--path", default=str(DEFAULT_JOURNAL))
    ap.add_argument("--params", default=str(DEFAULT_PARAMS))
    ap.add_argument("--poly-dir", default=str(DEFAULT_POLY))
    ap.add_argument("--poly-latest", default=",".join(DEFAULT_POLY_LATEST), help="comma-separated batch stems (must_not set)")
    a = ap.parse_args(argv)
    latest = tuple(x.strip() for x in a.poly_latest.split(",") if x.strip())
    rc, lines = run(Path(a.path), Path(a.params), Path(a.poly_dir), latest)
    _out("\n".join(lines))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
