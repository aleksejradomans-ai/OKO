"""tools/calibrate_sigma.py -- офлайн-калибровка параметров Σ («калибровка 2d» + «калибровка 2d» + «правки после 2e» + «закрытие 2e», фаза 2, этапы 2d/2e; «этап 3c», фаза 3, этап 3c: dt_0). Только stdlib, вне src/.

Вход: logs/sigma_shadow.jsonl (+ .1) тем же разбором строк, что sigma_shadow_stats.py (признак `volume` изолирован: в
ошибки, ε_state, T_θ, Z_θ, π_0, τ_b не входит). Ядро src/abstraction_layer.py -- только импорт (predict_belief /
update_belief / state_distance / state_admissibility для Θ_state; ядро не меняется). Ручные значения (MANUAL) -- только
из файла --manual (JSON), чисел скрипт не придумывает:
    {"ref": "<ссылка на решение>", "tau_D": <с, > 0>, "Theta_crit": <>= 0>, "Theta_reset": <>= 0, < Theta_crit>,
     "R_t": {"N_obs": <int>, "N_route": <int>, "N_reflect": <int>}, "kappa": {"<feature>": <> 0>, ...}  (по желанию),
     "eps_state": {"<feature>": <>= 0, в делениях шкалы признака>, ...}  (по желанию), "tau_b": <с, > 0>  (по желанию),
     "delta_rule": "any_fail_1"  (по желанию; решение 16.11 12:59 п.1),
     "Theta_state": <[0, 1]>  (по желанию; 13:43 п.1 -- MANUAL, оценка по журналу -> journal_note),
     "eps_norm": {"<feature>": <[0, 1], доля шкалы>, ...}  (по желанию; 13:43 п.2 -- запись eps_norm для фазы 3),
     "dt_0": <с, > 0>  (по желанию; 16:16 + 18:21 п.3 -- номинальный шаг Δt_0 интегратора λ_0 = exp(−Δt_0/τ_D))}
Есть dt_0 -> раздел `dt_0` файла параметров (status MANUAL, ссылка ref; ядро фазы 3 читает его params_from_doc,
«этап 3c» этапа 3c); 20 параметров раскладки и прочие разделы от dt_0 не зависят. Нет dt_0 -> раздела нет (как до 3c).
Θ_state всегда несёт source.loo_note -- LOO-запись (n, min, q05, q10, медиана, max, разброс, защита от вырождения).
Есть eps_state / tau_b -> статус MANUAL со ссылкой ref; оценка по журналу -- в source.journal_note (для сравнения).
Есть kappa у признака -> признак входит в Σ даже при одном различном значении в журнале (решение 16.11 12:59 п.5);
без kappa такой признак -- NO_DATA, не 0. delta_rule -> запись `delta_rule` в файле параметров (правило δ_t для
вызывающей стороны; β_k остаются записью). Прочие поля JSON (например "holdout" строкой) игнорируются без ошибки.
Нет файла или нет любого из tau_D / Theta_crit / Theta_reset / R_t -> статус MANUAL_PENDING, файл параметров НЕ пишется,
код возврата 3. Неверные ручные значения (Theta_reset >= Theta_crit, не число, неизвестное delta_rule, ...) -> код 2.
Всё на месте -> config/sigma_params.json (или --out), код 0. --dry-run -- посчитать и не писать.

Выборка (решение 16.11 12:04 п.3): 2d калибруется на всех командах журнала (volume изолирован); отложенной части нет.
2e -- новые команды с seq > last_seq. Пересчёт 2d после 2e (решение 16.11 12:59 п.3): --last-seq-from <файл
параметров> берёт holdout_2e.last_seq из прежнего файла и считает ТОЛЬКО строки с seq <= last_seq; last_seq не меняется.

Модель состояния (решение 16.11 12:59 п.3):
  * пространство состояний признака S_f -- целые от min(0, наблюдённые) до max(κ_f, наблюдённые);
  * сглаживание -- априор Дирихле с массой 1 псевдонаблюдение на строку (Perks): T_θ и π_0 -- аддитивное
    (Лидстон) с α = 1/|S_f| (база равномерная); Z_θ -- та же масса β = 1, база -- общая для всех состояний модель
    ошибки считывания по смещению (s_obs − s_exp), сама сглажена Лидстоном с α = 1/|смещений|. Новые значения и
    переходы получают малый ненулевой вес; при наличии данных доминируют частоты;
  * b⁻ = ρ_t·T_θ(·|до) + (1 − ρ_t)·π_0, ρ_t = exp(−Δt/τ_b), Δt -- реальный интервал от считывания «до» до отправки
    (или от outcome прошлой команды того же признака); нет «до» -> b⁻ = π_0;
  * Θ_state -- min C_state на PASS 2d, C_state каждой команды -- вне выборки (leave-one-out: частоты без самой
    команды). Оценка на той же выборке (in-sample) -- в source.in_sample_note.
Значения журнала (s_exp / s_obs / ts / seq) в stdout не выводятся: только счётчики и статусы. Файл параметров
детерминирован (без времени запуска): два прогона -> байт-в-байт.

Раскладка 20 параметров: решение 15.11 11:39 (phase2b_layout) + 16.11 01:41 (close_2c) + 16.11 12:04 (manual_2d)
+ 16.11 12:59 (2e_fix).
Запуск (интерпретатор службы):
  <python> tools\\calibrate_sigma.py --manual M.json [--last-seq-from config\\sigma_params.json]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("sigma_shadow_stats_for_calibrate", _HERE / "sigma_shadow_stats.py")
sss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sss)

ROOT = _HERE.parent
_KERNEL = "abstraction_layer_for_calibrate"
_kspec = importlib.util.spec_from_file_location(_KERNEL, ROOT / "src" / "abstraction_layer.py")
al = importlib.util.module_from_spec(_kspec)
sys.modules[_KERNEL] = al            # dataclasses ядра ищут свой модуль в sys.modules
_kspec.loader.exec_module(al)        # ядро: только импорт

DEFAULT_JOURNAL = ROOT / "logs" / "sigma_shadow.jsonl"
DEFAULT_OUT = ROOT / "config" / "sigma_params.json"
ISOLATE_DEFAULT = sss.ISOLATE_DEFAULT
SCHEMA = "sigma_params/2d-v3"
JOURNAL_NAME = "logs/sigma_shadow.jsonl"
EXIT_OK, EXIT_INVALID, EXIT_PENDING = 0, 2, 3
ERR_TYPES = tuple(sss.ERRS) + ("other",)  # FAIL без err = other (решение 15.11 11:39, «Следствия» п.3)
TH_READBACK = sss.TH_READBACK            # π_0: меньше -- запасной MANUAL (равномерный)

REF_LAYOUT = "решение 2026-11-15 11:39 (раскладка 20 параметров Σ)"
REF_CLOSE2C = "решение 2026-11-16 01:41 (закрытие 2c, правила 2d)"
REF_SAMPLE = "решение 2026-11-16 12:04 п.3 (2d на всём журнале; 2e -- новые команды)"
REF_BETA = "решение 2026-11-15 10:50 (δ_t/β_k: Σβ_k = 1) + 11:39 п.16 (равные веса) + «Следствия» п.3 (other)"
REF_ALPHA = "решение 2026-11-15 11:39 п.14 (равные веса по числовым признакам API ТВ, сумма 1)"
REF_FIX = "решение 2026-11-16 12:59 (правки после 2e, B07h)"
REF_CLOSE2E = "решение 2026-11-16 13:43 (закрытие 2e, B07i)"
LOO_MIN_N = 10                          # LOO-запись Θ_state: меньше -- квантиль не робастен, нижняя граница = min
LOO_Q = (0.05, 0.10)                    # нижние квантили LOO-оценок (ближайший ранг снизу)
# κ_j = max − min признака из API ТВ (раскладка п.15). setting_710 -- шкала 0–10 (close_2c п.2). Остальное -- из --manual.
KAPPA_KNOWN: dict = {"setting_710": (10, REF_CLOSE2C + ": яркость подсветки, шкала 0–10 (API ТВ)")}
# Правила δ_t (решение 16.11 12:59 п.1). Без delta_rule в --manual -- прежняя запись β_k (δ_t = Σ β_k e_k).
DELTA_RULES = {"any_fail_1": "δ_t = 1 на любом сбое команды (outcome FAIL или err), 0 на успехе; β_k -- запись, в δ не участвуют"}
DELTA_RULE_LEGACY = "beta_k"
MAX_STATES = 1024                       # S_f шире -- только шкала 0..κ и наблюдённые значения (защита от выброса)
SMOOTH_MASS = Fraction(1)               # масса априора Дирихле на строку: 1 псевдонаблюдение (Perks)
SMOOTH_METHOD = ("априор Дирихле, масса 1 псевдонаблюдение на строку (Perks): T_θ и π_0 -- Лидстон α = 1/|S_f| "
                 "(база равномерная); Z_θ -- база = общая модель ошибки считывания по смещению s_obs − s_exp "
                 "(Лидстон α = 1/|смещений|), β = 1 (" + REF_FIX + " п.3)")

PARAMS = ("tau_b", "tau_D", "tau_h", "tau_i", "kappa_space", "eps_cov", "eps_state", "Theta_state", "Theta_intent",
          "Theta_margin", "Theta_goal", "Theta_crit", "Theta_reset", "alpha_j", "kappa_j", "beta_k", "T_theta",
          "Z_theta", "pi_0", "R_t")
MANUAL_REQUIRED = ("tau_D", "Theta_crit", "Theta_reset", "R_t")
MANUAL_OPTIONAL = ("eps_state", "tau_b", "delta_rule", "Theta_state", "eps_norm",   # 12:04 п.4, 12:59 п.1, 13:43 п.1–2
                   "dt_0")                                                           # 16:16 + 18:21 п.3 (этап 3c)
REF_DT0 = "решение 2026-11-16 16:16 (интегратор λ_0 = exp(−Δt_0/τ_D)) + 18:21 п.3 (Δt_0 в файл параметров, этап 3c)"
NO_DATA_FIXED = {
    "tau_h": "камера: фаза 4 (vision_parser.py) -- " + REF_LAYOUT + " п.3",
    "tau_i": "камера: фаза 4 -- " + REF_LAYOUT + " п.4",
    "kappa_space": "камера: фаза 4 -- " + REF_LAYOUT + " п.5",
    "eps_cov": "камера: фаза 4 -- " + REF_LAYOUT + " п.6",
    "Theta_intent": "оценки намерения q нет (правка логики в фазе 2 запрещена) -- " + REF_LAYOUT + " п.9",
    "Theta_margin": "нет второй оценки (top-2) -- " + REF_LAYOUT + " п.10",
    "Theta_goal": "C_goal нигде не считается -- " + REF_LAYOUT + " п.11",
}


_DRAFT_RX = re.compile(r"\s*\(DECISION_draft_[^()]*\.md\)")
_DEC_RX = re.compile(r"DECISIONS(?![A-Za-z0-9_])")


def public_ref(s: str) -> str:
    """текст ссылки ref для публичного файла параметров -- без имён внутренних файлов: скобка с именем
    черновика решения (DECISION_draft_*.md) убирается, слово DECISIONS -> «решение». Числа и даты не трогаются;
    без этих меток -- строка без изменений."""
    return _DEC_RX.sub("решение", _DRAFT_RX.sub("", s))


class ManualError(ValueError):
    """Неверные ручные значения: код 2, файл не пишется."""


# ---------------------------------------------------------------- разбор журнала
def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _key(v: Any) -> str:
    """Канонический ключ состояния (детерминированный)."""
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def _f(x: Fraction) -> float:
    return float(x)


def load_rows(path: Path, isolate: dict, last_seq: "int | None" = None) -> "tuple[list, dict, list]":
    """Строки журнала тем же разбором, что sigma_shadow_stats; изолированные команды отброшены.
    В st["last_seq"] -- последний seq журнала по всем строкам (вместе с изолированными) или None.
    last_seq задан -> только строки с целым seq <= last_seq (2d после закрытия); st["rows_2d"] -- их число."""
    st: dict = {"files": {}, "lines_total": 0, "lines_bad": 0}
    files = sss.journal_files(Path(path))
    rows = list(sss._iter_lines(files, st))
    st["last_seq"] = max((r["seq"] for r in rows if sss._int_ts(r.get("seq"))), default=None)
    st["lines_ok"] = len(rows)
    if last_seq is not None:
        rows = [r for r in rows if sss._int_ts(r.get("seq")) and r["seq"] <= last_seq]
    st["rows_2d"] = len(rows)
    st["rows_all"] = rows
    iso = {f for f in isolate if isinstance(f, str)}
    iso_seqs = sss._isolated_seqs(rows, iso)

    def _iso(r: dict) -> bool:
        return (r["seq"] in iso_seqs) if sss._int_ts(r.get("seq")) else (r.get("feature") in iso)
    st["isolated"] = {f: len({r["seq"] for r in rows if _iso(r) and sss._int_ts(r.get("seq")) and r.get("feature") == f})
                      for f in sorted(iso)}
    return [r for r in rows if not _iso(r)], st, files


def journal_digest(files: list) -> str:
    h = hashlib.sha256()
    for f in files:
        try:
            h.update(Path(f).read_bytes())
        except OSError:
            h.update(b"<absent>")
        h.update(b"\x00")
    return h.hexdigest()[:16]


def rows_digest(rows: list) -> str:
    """Отпечаток набора строк (канонический JSON): не меняется, когда журнал дописывается после last_seq."""
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(r, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def build_commands(rows: list) -> list:
    """Команды с outcome, по seq. pre -- считанное ДО первой отправки (тот же признак), after -- считанное в outcome.
    send_ts -- первая отправка, pre_ts -- момент считывания pre (для Δt в ρ_t). before / dt_s -- link_commands()."""
    by: dict = defaultdict(list)
    for i, r in enumerate(rows):
        if sss._int_ts(r.get("seq")):
            by[r["seq"]].append((i, r))
    cmds = []
    for seq in sorted(by):
        items = by[seq]
        outs = [r for _, r in items if r["step"] == "outcome"]
        if not outs:
            continue
        o = outs[-1]
        feat = o.get("feature")
        first_send = min((i for i, r in items if r["step"] == "send"), default=None)
        pre = pre_ts = None
        if first_send is not None:
            for i, r in items:
                if i < first_send and r["step"] == "readback" and r.get("feature") == feat and _num(r.get("s_obs")):
                    pre, pre_ts = r["s_obs"], r["ts"]
        send_ts = next((r["ts"] for i, r in items if i == first_send), None)
        errs = {r["err"] for _, r in items if r.get("err") is not None}
        fail = o.get("outcome") == "FAIL"
        cmds.append({
            "seq": seq, "feature": feat, "action": o.get("action"), "outcome": o.get("outcome"),
            "s_exp": o.get("s_exp") if _num(o.get("s_exp")) else None,
            "s_obs": o.get("s_obs") if _num(o.get("s_obs")) else None,
            "pre": pre, "pre_ts": pre_ts, "send_ts": send_ts,
            "first_ts": min(r["ts"] for _, r in items), "out_ts": o["ts"],
            "readbacks": [(r.get("feature"), r["s_obs"]) for _, r in items
                          if r["step"] == "readback" and _num(r.get("s_obs")) and r.get("feature") is not None],
            "err_types": sorted(errs) if errs else (["other"] if fail else []),
        })
    return link_commands(cmds)


def link_commands(cmds: list) -> list:
    """before -- состояние до команды: pre, иначе считанное в outcome прошлой команды того же признака (поток по seq).
    dt_s -- реальный интервал (с) от момента «до» до отправки (для ρ_t = exp(−Δt/τ_b)); None -- нет «до»."""
    last: dict = {}
    for c in cmds:
        f = c["feature"]
        t_send = c["send_ts"] if c.get("send_ts") is not None else c["first_ts"]
        if c["pre"] is not None:
            c["before"] = c["pre"]
            c["dt_s"] = max(0.0, (t_send - c["pre_ts"]) / 1000.0) if c.get("pre_ts") is not None else 0.0
        elif f in last:
            c["before"], prev_out = last[f]
            c["dt_s"] = max(0.0, (t_send - prev_out) / 1000.0)
        else:
            c["before"], c["dt_s"] = None, None
        if f is not None and c["s_obs"] is not None:
            last[f] = (c["s_obs"], c["out_ts"])
    return cmds


def holdout_2e(last_seq: "int | None") -> dict:
    """Граница выборки 2e (решение 16.11 12:04 п.3): в 2d -- весь журнал, в 2e -- команды с seq > last_seq."""
    return {"rule": "новые команды, seq > last_seq", "last_seq": last_seq}


def last_seq_from(path: Path) -> int:
    """holdout_2e.last_seq из прежнего файла параметров (last_seq не меняется). ManualError -- нет / неверный."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ManualError(f"--last-seq-from: {type(exc).__name__}") from None
    h = doc.get("holdout_2e") if isinstance(doc, dict) else None
    v = h.get("last_seq") if isinstance(h, dict) else None
    if not sss._int_ts(v):
        raise ManualError("--last-seq-from: нет holdout_2e.last_seq")
    return v


# ---------------------------------------------------------------- ручные значения
def _pos_num(v: Any, name: str, strict: bool) -> float:
    if not _num(v):
        raise ManualError(f"{name}: не конечное число")
    if v < 0 or (strict and v == 0):
        raise ManualError(f"{name}: должно быть {'> 0' if strict else '>= 0'}")
    return float(v)


def validate_manual(m: "dict | None") -> "tuple[dict, list]":
    """-> (проверенные значения, список недостающих). ManualError -- неверные значения."""
    if m is None:
        return {}, list(MANUAL_REQUIRED)
    if not isinstance(m, dict):
        raise ManualError("manual: ожидается объект JSON")
    out: dict = {}
    missing = [k for k in MANUAL_REQUIRED if k not in m or m[k] is None]
    present = ([k for k in MANUAL_REQUIRED if k not in missing] + (["kappa"] if m.get("kappa") else [])
               + [k for k in MANUAL_OPTIONAL if m.get(k) is not None])
    ref = m.get("ref")
    if present and not (isinstance(ref, str) and ref.strip()):
        raise ManualError("ref: нужна ссылка на решение (число без источника не пишется)")
    out["ref"] = public_ref(ref.strip()) if isinstance(ref, str) else None
    if "tau_D" not in missing:
        out["tau_D"] = _pos_num(m["tau_D"], "tau_D", strict=True)
    for k in ("Theta_crit", "Theta_reset"):
        if k not in missing:
            out[k] = _pos_num(m[k], k, strict=False)
    if "Theta_crit" in out and "Theta_reset" in out and not out["Theta_reset"] < out["Theta_crit"]:
        raise ManualError("Theta_reset должно быть < Theta_crit")
    if "R_t" not in missing:
        r = m["R_t"]
        if not isinstance(r, dict) or set(r) != {"N_obs", "N_route", "N_reflect"}:
            raise ManualError("R_t: нужны ровно N_obs, N_route, N_reflect")
        for k, v in r.items():
            if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
                raise ManualError(f"R_t.{k}: целое >= 0")
        out["R_t"] = {k: r[k] for k in sorted(r)}
    kap = m.get("kappa") or {}
    if not isinstance(kap, dict):
        raise ManualError("kappa: ожидается объект {признак: число}")
    out["kappa"] = {}
    for f in sorted(kap, key=str):
        if not isinstance(f, str) or not f:
            raise ManualError("kappa: имя признака -- строка")
        out["kappa"][f] = _pos_num(kap[f], f"kappa.{f}", strict=True)
    if m.get("tau_b") is not None:
        out["tau_b"] = _pos_num(m["tau_b"], "tau_b", strict=True)
    if m.get("eps_state") is not None:
        e = m["eps_state"]
        if not isinstance(e, dict) or not e:
            raise ManualError("eps_state: ожидается непустой объект {признак: число}")
        out["eps_state"] = {}
        for f in sorted(e, key=str):
            if not isinstance(f, str) or not f:
                raise ManualError("eps_state: имя признака -- строка")
            out["eps_state"][f] = _pos_num(e[f], f"eps_state.{f}", strict=False)
    if m.get("delta_rule") is not None:
        d = m["delta_rule"]
        if not isinstance(d, str) or d not in DELTA_RULES:
            raise ManualError("delta_rule: допустимо " + ", ".join(sorted(DELTA_RULES)))
        out["delta_rule"] = d
    if m.get("Theta_state") is not None:
        v = _pos_num(m["Theta_state"], "Theta_state", strict=False)
        if v > 1:
            raise ManualError("Theta_state: должно быть в [0, 1]")
        out["Theta_state"] = v
    if m.get("eps_norm") is not None:
        e = m["eps_norm"]
        if not isinstance(e, dict) or not e:
            raise ManualError("eps_norm: ожидается непустой объект {признак: доля шкалы}")
        out["eps_norm"] = {}
        for f in sorted(e, key=str):
            if not isinstance(f, str) or not f:
                raise ManualError("eps_norm: имя признака -- строка")
            v = _pos_num(e[f], f"eps_norm.{f}", strict=False)
            if v > 1:
                raise ManualError(f"eps_norm.{f}: доля шкалы в [0, 1]")
            out["eps_norm"][f] = v
    if m.get("dt_0") is not None:
        out["dt_0"] = _pos_num(m["dt_0"], "dt_0", strict=True)
    return out, missing


def loo_record(vals: list, n_excluded: int, in_sample_min: "float | None") -> dict:
    """LOO-запись Θ_state (решение 16.11 13:43 п.1): робастная сводка оценок C_state вне выборки с защитой от
    вырождения (мало n -> квантиль не берётся; все равны -> вырождено; None/ошибки -- исключены и посчитаны)."""
    v = sorted(vals)
    n = len(v)
    rec: dict = {"n": n, "excluded": n_excluded, "method": "C_state каждой PASS-команды 2d по модели без неё "
                 "(leave-one-out); нижний квантиль -- ближайший ранг снизу", "min_n_for_quantile": LOO_MIN_N}
    if n == 0:
        return rec | {"guard": "вырождено: нет ни одной LOO-оценки", "robust_lower": None}
    q = {f"q{int(round(p * 100)):02d}": v[int(math.floor(p * (n - 1)))] for p in LOO_Q}
    rec |= {"min": v[0], "median": v[(n - 1) // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2, "max": v[-1],
            "spread": v[-1] - v[0]} | q
    if in_sample_min is not None:
        rec["in_sample_min"] = in_sample_min
    if v[-1] - v[0] == 0:
        rec["guard"] = "вырождено: все LOO-оценки равны (разброс 0)"
        rec["robust_lower"] = v[0]
    elif n < LOO_MIN_N:
        rec["guard"] = f"n={n} < {LOO_MIN_N}: квантиль не робастен, нижняя граница = min"
        rec["robust_lower"] = v[0]
    else:
        rec["guard"] = "ok"
        rec["robust_lower"] = q["q05"]
    return rec


# ---------------------------------------------------------------- модель состояния (сглаживание)
def state_space(values: "set[str]", kappa: "float | None") -> "dict | None":
    """S_f: целые от min(0, наблюдённые) до max(κ_f, наблюдённые). Нецелые значения -- отсортированный список."""
    if not values and kappa is None:
        return None
    nums = [float(v) for v in values]
    if all(x.is_integer() for x in nums) and (kappa is None or float(kappa).is_integer()):
        lo = int(min([0.0] + nums)) if kappa is not None else int(min(nums))
        hi = int(max(([float(kappa)] if kappa is not None else []) + nums))
        if hi - lo + 1 <= MAX_STATES:
            return {"min": lo, "max": hi}
    keys = set(values)                     # нецелые или слишком широкий разброс: шкала 0..κ + наблюдённые значения
    if kappa is not None and kappa + 1 <= MAX_STATES:
        keys |= {_key(x) for x in range(0, int(math.floor(kappa)) + 1)}
    return {"list": sorted(keys, key=float)}


def states_of(space: dict) -> list:
    if "list" in space:
        return list(space["list"])
    return [str(x) for x in range(int(space["min"]), int(space["max"]) + 1)]


def _lidstone(cnt: Counter, size: int) -> dict:
    """Строка Лидстона с α = масса/size: {"n", "seen": {k: p}, "unseen": p0}; Σ = 1 по построению (точная арифметика)."""
    n = sum(cnt.values())
    a = SMOOTH_MASS / size
    den = n + SMOOTH_MASS
    return {"n": n, "seen": {k: _f((c + a) / den) for k, c in sorted(cnt.items(), key=lambda kv: float(kv[0]))},
            "unseen": _f(a / den)}


def _lid_vec(row: "dict | None", states: list) -> tuple:
    if row is None:
        return tuple(1.0 / len(states) for _ in states)
    return tuple(row["seen"].get(s, row["unseen"]) for s in states)


def model_counts(cmds: list, feats: "list | set") -> dict:
    """Частоты по командам (только признаки feats): T (признак|действие, до -> после), Z (s_exp -> s_obs),
    смещения считывания (s_obs − s_exp), π_0 (считанные). Ключи -- _key."""
    T: dict = defaultdict(lambda: defaultdict(Counter))
    Z: dict = defaultdict(lambda: defaultdict(Counter))
    OFF: dict = defaultdict(Counter)
    PI: dict = defaultdict(Counter)
    vals: dict = defaultdict(set)
    for c in cmds:
        f = c["feature"]
        if f not in feats:
            continue
        b, a, e = c.get("before"), c["s_obs"], c["s_exp"]
        if b is not None and a is not None:
            T[f"{f}|{c['action']}"][_key(b)][_key(a)] += 1
        if e is not None and a is not None:
            Z[f][_key(e)][_key(a)] += 1
            OFF[f][_key(Fraction(a) - Fraction(e))] += 1
        for v in (b, a, e):
            if v is not None:
                vals[f].add(_key(v))
    for c in cmds:                                  # π_0: все считывания по своему признаку (как в 2d-v2)
        for ff, v in c["readbacks"]:
            if ff in feats:
                PI[ff][_key(v)] += 1
                vals[ff].add(_key(v))
    return {"T": T, "Z": Z, "OFF": OFF, "PI": PI, "vals": vals}


def smooth_model(cnt: dict, spaces: dict) -> dict:
    """Сглаженные T_θ / Z_θ / π_0 (масса априора 1 на строку). spaces: {f: state_space}."""
    t_api: dict = {}
    for k in sorted(cnt["T"]):
        f = k.split("|", 1)[0]
        size = len(states_of(spaces[f]))
        t_api[k] = {b: _lidstone(row, size) for b, row in sorted(cnt["T"][k].items(), key=lambda kv: float(kv[0]))}
    z_api, z_base = {}, {}
    for f in sorted(spaces):
        st = states_of(spaces[f])
        lo, hi = float(st[0]), float(st[-1])
        offs = [_key(Fraction(x) - Fraction(st[0])) for x in st] + [_key(Fraction(st[0]) - Fraction(x)) for x in st]
        offs = sorted(set(offs), key=float)
        z_base[f] = _lidstone(cnt["OFF"].get(f, Counter()), len(offs)) | {"range": [_key(lo - hi), _key(hi - lo)]}
        z_api[f] = {x: {"n": sum(r.values()), "counts": {o: n for o, n in sorted(r.items(), key=lambda kv: float(kv[0]))}}
                    for x, r in sorted(cnt["Z"].get(f, {}).items(), key=lambda kv: float(kv[0]))}
    pi = {f: _lidstone(cnt["PI"].get(f, Counter()), len(states_of(spaces[f]))) for f in sorted(spaces)}
    return {"T": t_api, "Z": z_api, "Z_base": z_base, "pi": pi}


def view_from_values(t_val: "dict | None", z_val: "dict | None", pi_val: "dict | None") -> dict:
    """Вид модели для c_state() из значений файла параметров (schema 2d-v3)."""
    t_val, z_val = t_val or {}, z_val or {}
    return {"T": t_val.get("api") or {}, "Z": z_val.get("api") or {}, "Z_base": z_val.get("base") or {},
            "beta": z_val.get("beta", 1.0), "pi": pi_val or {},
            "spaces": (z_val.get("states") or t_val.get("states") or {})}


def z_likelihood(view: dict, f: str, obs: str, states: list) -> tuple:
    """Z_θ(obs | x) по всем x ∈ S_f: (n_x(obs) + β·q_x(obs)) / (n_x + β), q_x -- база по смещению, нормирована по S_f."""
    base = view["Z_base"].get(f)
    rows = view["Z"].get(f, {})
    beta = float(view.get("beta", 1.0))
    out = []
    sset = set(states)
    seen = [(Fraction(k), p) for k, p in base["seen"].items()] if base is not None else []
    for x in states:
        if base is not None:
            fx = Fraction(x)
            inside = [p for k, p in seen if _key(fx + k) in sset]     # смещения базы, попадающие в S_f от x
            norm = math.fsum(inside) + base["unseen"] * (len(states) - len(inside))
            qx = base["seen"].get(_key(Fraction(obs) - fx), base["unseen"]) / norm
        else:
            qx = 0.0
        r = rows.get(x)
        if r is None:
            out.append(qx if base is not None else 0.0)
        else:
            out.append((r["counts"].get(obs, 0) + beta * qx) / (r["n"] + beta))
    return tuple(out)


def c_state(view: dict, eps: "float | None", alpha: "dict | None", kappa: "dict | None", c: dict,
            tau_b: "float | None", dt_override: "float | None" = None) -> "tuple[float | None, str]":
    """C_state команды функциями ядра. -> (C_state, статус):
    OK / NO_DATA_FEATURE / NO_PAIR / NO_PRIOR / OBS_UNSEEN (s_obs вне S_f или Z = 0) / PRIOR_ZERO (Σ Z·b⁻ = 0)."""
    f = c["feature"]
    a = (alpha or {}).get(f)
    k = (kappa or {}).get(f)
    if a is None or k is None or eps is None or f not in view["spaces"]:
        return None, "NO_DATA_FEATURE"
    if c["s_exp"] is None or c["s_obs"] is None:
        return None, "NO_PAIR"
    states = states_of(view["spaces"][f])
    obs = _key(c["s_obs"])
    if obs not in states:
        return 0.0, "OBS_UNSEEN"
    pi_row = view["pi"].get(f)
    pi_vec = _lid_vec(pi_row, states) if pi_row is not None else None
    before = c.get("before")
    if before is not None and _key(before) in states and pi_vec is not None:
        b0 = _key(before)
        t_row = view["T"].get(f"{f}|{c['action']}", {}).get(b0)
        row = _lid_vec(t_row, states)
        onehot = tuple(1.0 if s == b0 else 0.0 for s in states)
        uni = _lid_vec(None, states)
        trans = tuple(row if s == b0 else uni for s in states)
        dt = c.get("dt_s") if dt_override is None else dt_override
        b_minus = al.predict_belief(onehot, trans, pi_vec, math.inf if dt is None else float(dt), tau_b)
    elif pi_vec is not None:
        b_minus = pi_vec
    else:
        return None, "NO_PRIOR"
    lik = z_likelihood(view, f, obs, states)
    if not any(lik):
        return 0.0, "OBS_UNSEEN"
    upd = al.update_belief(b_minus, lik)
    if upd.outcome is al.Outcome.OBSERVE:   # Σ Z·b⁻ = 0: ядро -> OBSERVE; C_state = 0
        return 0.0, "PRIOR_ZERO"
    dist = tuple(al.state_distance((float(s),), (float(c["s_exp"]),), (float(a),), (float(k),)) for s in states)
    return al.state_admissibility(upd.belief, dist, eps, ()), "OK"


def delta_vector(rule: str, failed: bool, err_types: "list | set", keys: tuple, bvals: tuple) -> "tuple[tuple, tuple]":
    """(e_k, β_k) для dissonance_step ядра по правилу δ_t файла параметров (решение 16.11 12:59 п.1)."""
    if rule == "any_fail_1":
        return ((1.0,) if failed else (0.0,)), (1.0,)
    return tuple(1.0 if k in err_types else 0.0 for k in keys), bvals


# ---------------------------------------------------------------- расчёт
def _src(n: int, method: str) -> dict:
    return {"file": JOURNAL_NAME, "n": n, "method": method}


def _nodata(reason: str) -> dict:
    return {"status": "NO_DATA", "value": None, "source": {"no_data": reason}}


def _manual(value: Any, ref: str, note: str = "") -> dict:
    s = {"manual": ref}
    if note:
        s["note"] = note
    return {"status": "MANUAL", "value": value, "source": s}


def _dist(counter: Counter) -> dict:
    tot = sum(counter.values())
    return {k: _f(Fraction(c, tot)) for k, c in sorted(counter.items())}


def calibrate(path: Path = DEFAULT_JOURNAL, manual: "dict | None" = None, isolate: dict = ISOLATE_DEFAULT,
              last_seq: "int | None" = None) -> dict:
    """-> {'doc': документ параметров, 'counts': счётчики для stdout, 'missing': [...]}. ManualError -- неверный вход.
    last_seq задан -> расчёт только по строкам seq <= last_seq, holdout_2e.last_seq = last_seq."""
    man, missing = validate_manual(manual)
    rows, st, files = load_rows(Path(path), isolate, last_seq)
    cmds = build_commands(rows)
    train = cmds                                         # весь журнал 2d (решение 16.11 12:04 п.3)
    frozen = last_seq is not None
    hinfo = holdout_2e(last_seq if frozen else st["last_seq"])
    menu_steps = sum(1 for r in rows if r["step"] == "menu_step")
    P: dict = {}

    # --- значения признаков (все команды): пары outcome, различные значения
    pairs: dict = defaultdict(list)                      # f -> [(s_exp, s_obs, outcome)]
    for c in train:
        if c["feature"] is not None and c["s_exp"] is not None and c["s_obs"] is not None:
            pairs[c["feature"]].append((c["s_exp"], c["s_obs"], c["outcome"]))
    distinct = {f: len({_key(a) for a, _, _ in p} | {_key(b) for _, b, _ in p}) for f, p in pairs.items()}

    # κ_j (п.15): известная шкала API ТВ или --manual. Признак с < 2 различных значений -> NO_DATA, кроме κ из --manual
    # (решение 16.11 12:59 п.5: κ lounge_brightness = 255 -> ветка состояния для цвета считается).
    kappa_val: dict = {}
    kappa_src: dict = {}
    for f in sorted(pairs):
        if f in man.get("kappa", {}):
            kappa_val[f] = man["kappa"][f]
            kappa_src[f] = {"manual": man["ref"]} | ({"note": f"различных значений {distinct[f]} < 2; κ задан вручную "
                                                              f"({REF_FIX} п.5)"} if distinct[f] < 2 else {})
        elif distinct[f] < 2:
            kappa_src[f] = {"no_data": f"различных значений {distinct[f]} < 2 ({REF_CLOSE2C})"}
        elif f in KAPPA_KNOWN:
            kappa_val[f], kappa_src[f] = float(KAPPA_KNOWN[f][0]), {"manual": KAPPA_KNOWN[f][1]}
        else:
            kappa_src[f] = {"no_data": "диапазон API ТВ не задан (нужен kappa в --manual)"}
    calib = sorted(kappa_val)                            # признаки Σ: известный κ_j (журнал или --manual)
    if calib:
        P["kappa_j"] = {"status": "MANUAL", "value": {f: kappa_val[f] for f in calib},
                        "source": {"manual": REF_LAYOUT + " п.15 (κ_j = max − min, API ТВ)", "per_feature": kappa_src}}
    else:
        P["kappa_j"] = {"status": "NO_DATA", "value": None, "source": {"no_data": "нет признака с κ_j",
                                                                        "per_feature": kappa_src}}
    # α_j (п.14): равные веса по признакам Σ
    if calib:
        P["alpha_j"] = _manual({f: _f(Fraction(1, len(calib))) for f in calib}, REF_ALPHA)
    else:
        P["alpha_j"] = _nodata("нет признака Σ (κ_j / различные значения)")
    alpha_fr = {f: Fraction(1, len(calib)) for f in calib}

    def d_sigma(f: str, s: float, s_exp: float) -> Fraction:
        """d_Σ = α_j·min(1, |s − s_exp|/κ_j) (один признак на команду; насыщение -- решение M05)."""
        return alpha_fr[f] * min(Fraction(1), abs(Fraction(s) - Fraction(s_exp)) / Fraction(kappa_val[f]))

    # ε_state (п.7): max d_Σ(s_obs, s_exp) на PASS, только признаки Σ (оценка по журналу)
    eps_cmds = [(f, a, b, o) for f in calib for a, b, o in pairs[f]]
    pass_d = [d_sigma(f, b, a) for f, a, b, o in eps_cmds if o == "PASS"]
    if pass_d:
        eps = max(pass_d)
        P["eps_state"] = {"status": "JOURNAL", "value": _f(eps),
                          "source": _src(len(eps_cmds), "max d_Σ(s_obs, s_exp) на PASS; d_Σ = Σ α_j·min(1, |d_j|/κ_j); "
                                         f"признаки {','.join(calib)}; не-PASS пар "
                                         f"{sum(1 for x in eps_cmds if x[3] != 'PASS')} ({REF_CLOSE2C} п.2)")}
        P["eps_state"]["source"]["per_feature_no_data"] = {f: kappa_src[f] for f in sorted(pairs) if f not in calib}
    else:
        eps = None
        P["eps_state"] = _nodata("нет пар PASS у признаков Σ (различных значений < 2 -> NO_DATA, не 0)")
    # ε_state из --manual (решение 16.11 12:04 п.4): ε_j в делениях шкалы -> ε = Σ α_j·min(1, ε_j/κ_j) по признакам Σ
    if "eps_state" in man:
        journal_note = P["eps_state"]                    # оценка по журналу -- для сравнения
        me = man["eps_state"]
        used = [f for f in sorted(me) if f in calib]
        nd = {f: kappa_src.get(f, {"no_data": "признака нет в парах журнала"}) for f in sorted(me) if f not in calib}
        nd |= {f: kappa_src[f] for f in sorted(pairs) if f not in calib and f not in nd}
        if used:
            eps = sum((alpha_fr[f] * min(Fraction(1), Fraction(me[f]) / Fraction(kappa_val[f])) for f in used), Fraction(0))
            P["eps_state"] = {"status": "MANUAL", "value": _f(eps),
                              "source": {"manual": man["ref"], "per_feature": {f: me[f] for f in used},
                                         "units": "деления шкалы признака",
                                         "method": "ε_state = Σ α_j·min(1, ε_j/κ_j) по признакам Σ",
                                         "per_feature_no_data": nd, "journal_note": journal_note}}
            no_eps = [f for f in calib if f not in me]
            if no_eps:   # признак Σ без ε_j: допуск -- общий ε_state (скаляр ядра), в его делениях -- ε·κ_j/α_j
                P["eps_state"]["source"]["implicit_per_feature"] = {
                    f: {"divisions": _f(min(Fraction(1), eps / alpha_fr[f]) * Fraction(kappa_val[f])),
                        "note": "ε_j не задан; общий ε_state ядра"} for f in no_eps}
        else:
            eps = None
            P["eps_state"] = {"status": "NO_DATA", "value": None,
                              "source": {"no_data": "ε_state из --manual: нет признака Σ (κ_j / различные значения)",
                                         "manual": man["ref"], "per_feature_no_data": nd, "journal_note": journal_note}}

    # --- модель состояния: пространства S_f, частоты, сглаживание
    cnt = model_counts(train, set(pairs) | {f for c in train for f, _ in c["readbacks"]})
    spaces = {}
    for f in sorted(cnt["vals"]):
        sp = state_space(cnt["vals"][f], kappa_val.get(f, KAPPA_KNOWN.get(f, (None,))[0]))
        if sp is not None:
            spaces[f] = sp
    sm = smooth_model(cnt, spaces)
    smoothing = {"method": SMOOTH_METHOD, "mass_per_row": _f(SMOOTH_MASS),
                 "alpha": {f: _f(SMOOTH_MASS / len(states_of(spaces[f]))) for f in sorted(spaces)}}

    # T_θ (API, п.17): transition[x'][x] по (признак|действие); до = считанное до отправки, иначе after прошлой команды
    n_tr = sum(sum(r.values()) for rows_ in cnt["T"].values() for r in rows_.values())
    t_menu = {"status": "NO_DATA", "source": {"no_data": f"menu_step={menu_steps}; {REF_CLOSE2C} п.2"}}
    if sm["T"]:
        P["T_theta"] = {"status": "JOURNAL",
                        "value": {"api": sm["T"], "menu": None, "states": spaces, "smoothing": smoothing,
                                  "row_format": "строка до -> {n, seen: {после: p}, unseen: p каждого не встреченного}; "
                                                "строки «до», не встреченной в журнале, нет -> равномерная по S_f"},
                        "source": _src(n_tr, "частоты (до, после) по признак|действие + сглаживание (Лидстон α = 1/|S_f|); "
                                       "строки -- распределения по S_f (сумма 1)"),
                        "menu": t_menu}
    else:
        P["T_theta"] = _nodata("нет переходов (до, после) в журнале") | {"menu": t_menu}

    # Z_θ (API, п.18): Z[s_exp][s_obs] по парам outcome + база по смещению считывания
    n_z = sum(len(p) for p in pairs.values())
    z_menu = {"status": "NO_DATA", "source": {"no_data": "узел меню по кадру -- без детектора, фаза 4 (" + REF_LAYOUT + " п.18)"}}
    if sm["Z"] and any(sm["Z"].values()):
        P["Z_theta"] = {"status": "JOURNAL",
                        "value": {"api": {f: r for f, r in sm["Z"].items() if r}, "menu": None, "states": spaces,
                                  "base": {f: b for f, b in sm["Z_base"].items() if f in sm["Z"] and sm["Z"][f]},
                                  "beta": _f(SMOOTH_MASS), "smoothing": smoothing,
                                  "row_format": "Z(o|x) = (counts_x[o] + β·q(o − x)/Σ_o' q(o' − x)) / (n_x + β); "
                                                "x не встречен -> q(o − x)/Σ q; q -- base (Лидстон по смещению)"},
                        "source": _src(n_z, "частоты s_obs при s_exp по парам outcome + база по смещению s_obs − s_exp; "
                                       "строки сумма 1 по построению"), "menu": z_menu}
    else:
        P["Z_theta"] = _nodata("нет пар (s_exp, s_obs) в журнале") | {"menu": z_menu}

    # π_0 (п.19): частоты считанных состояний по признаку (сглажено)
    n_rb = sum(sum(c.values()) for c in cnt["PI"].values())
    if n_rb >= TH_READBACK:
        P["pi_0"] = {"status": "JOURNAL", "value": {f: sm["pi"][f] for f in sorted(cnt["PI"]) if f in sm["pi"]},
                     "source": _src(n_rb, "частоты считанных состояний (readback) по признаку + Лидстон α = 1/|S_f|")}
    elif n_rb > 0:
        P["pi_0"] = _manual({f: {"n": 0, "seen": {}, "unseen": _f(Fraction(1, len(states_of(spaces[f]))))}
                             for f in sorted(cnt["PI"]) if f in spaces},
                            REF_LAYOUT + " п.19 (запасной MANUAL: равномерный по S_f)", f"readback={n_rb} < {TH_READBACK}")
    else:
        P["pi_0"] = _nodata("считываний нет")

    # τ_b (п.1): соседние команды по признаку; расхождение = считанное до команды != считанному после прошлой
    gaps: list = []
    n_pairs = n_mis = 0
    prev: dict = {}
    for c in train:
        f = c["feature"]
        if f is None:
            continue
        p = prev.get(f)
        if p is not None:
            n_pairs += 1
            dt = c["first_ts"] - p["out_ts"]
            after = p["s_obs"] if p["s_obs"] is not None else (p["s_exp"] if p["outcome"] == "PASS" else None)
            if dt >= 0 and c["pre"] is not None and after is not None:
                gaps.append(dt)
                if _key(c["pre"]) != _key(after):
                    n_mis += 1
        prev[f] = c
    if gaps:
        expo = Fraction(sum(gaps), 1000)
        val = expo / max(n_mis, 1)
        gs = sorted(gaps)
        P["tau_b"] = {"status": "JOURNAL", "value": _f(val),
                      "source": _src(len(gaps), "экспоненциальная MLE: τ_b = Σ интервалов (с) / число расхождений; "
                                     f"расхождений {n_mis}" + (" -> 0: делитель 1 (одно условное событие, оценка "
                                                                "не идентифицирована)" if n_mis == 0 else "")
                                     + f"; соседних пар всего {n_pairs}; {REF_CLOSE2C}: набрано за ~10 мин")
                      | {"spread_s": {"min": gs[0] / 1000, "median": _f(Fraction(gs[(len(gs) - 1) // 2] + gs[len(gs) // 2], 2000)),
                                      "max": gs[-1] / 1000}}}
    else:
        P["tau_b"] = _nodata("нет соседних пар с считанным состоянием до команды")
    if "tau_b" in man:                                   # решение 16.11 12:04 п.4: MANUAL вместо оценки по журналу
        P["tau_b"] = {"status": "MANUAL", "value": man["tau_b"],
                      "source": {"manual": man["ref"], "journal_note": P["tau_b"]}}
    tau_b = P["tau_b"]["value"]

    # Θ_state (п.8, производная): C_state = Σ_{s ∈ A_ε} P(s | s_obs) функциями ядра; b⁻ по §3 с реальным Δt;
    # порог = min C_state на PASS, C_state -- вне выборки (leave-one-out). In-sample -- в in_sample_note.
    alpha_f = {f: _f(v) for f, v in alpha_fr.items()}
    kappa_f = {f: float(v) for f, v in kappa_val.items()}
    eps_f = _f(eps) if eps is not None else None
    ok_model = bool(sm["Z"]) and P["pi_0"]["value"] is not None and eps is not None and tau_b is not None

    def _view(model: dict) -> dict:
        return {"T": model["T"], "Z": model["Z"], "Z_base": model["Z_base"], "beta": _f(SMOOTH_MASS),
                "pi": model["pi"], "spaces": spaces}

    full_view = _view(sm)
    cs_in: list = []
    cs_loo: list = []
    if ok_model:
        feats_all = set(cnt["vals"])
        for i, c in enumerate(train):
            if c["feature"] not in calib:
                continue
            v_in, st_in = c_state(full_view, eps_f, alpha_f, kappa_f, c, tau_b)
            if v_in is None:
                continue
            cs_in.append((v_in, c["outcome"]))
            loo = smooth_model(model_counts(train[:i] + train[i + 1:], feats_all), spaces)
            v_loo, _ = c_state(_view(loo), eps_f, alpha_f, kappa_f, c, tau_b)
            cs_loo.append((v_loo, c["outcome"]))
    pass_loo = [v for v, o in cs_loo if o == "PASS" and v is not None]
    pass_in = [v for v, o in cs_in if o == "PASS"]
    loo_note = loo_record(pass_loo, sum(1 for v, o in cs_loo if o == "PASS" and v is None),
                          min(pass_in) if pass_in else None)
    if pass_loo:
        P["Theta_state"] = {"status": "JOURNAL", "value": min(pass_loo),
                            "source": _src(len(cs_loo), f"производная: min C_state на PASS, C_state вне выборки (leave-one-out: "
                                           f"T_θ/Z_θ/π_0 без самой команды); A_ε по ε_state {P['eps_state']['status']}; "
                                           "b⁻ = ρ_t·T_θ(·|до) + (1 − ρ_t)·π_0, ρ_t = exp(−Δt/τ_b), Δt реальный; "
                                           "Z_θ со сглаживанием; ядро: predict_belief / update_belief / state_admissibility; "
                                           f"не-PASS {sum(1 for _, o in cs_loo if o != 'PASS')} ({REF_FIX} п.3)")
                            | {"in_sample_note": {"value": min(pass_in), "n": len(cs_in),
                                                  "method": "min C_state на PASS по той же выборке (для сравнения)"}}}
    else:
        P["Theta_state"] = _nodata("C_state не считается (нет ε_state / T_θ / Z_θ / π_0 / τ_b или PASS)")
    P["Theta_state"]["source"]["loo_note"] = loo_note
    if "Theta_state" in man:                             # решение 16.11 13:43 п.1: рабочий порог -- MANUAL
        P["Theta_state"] = {"status": "MANUAL", "value": man["Theta_state"],
                            "source": {"manual": man["ref"], "decision": REF_CLOSE2E + " п.1",
                                       "journal_note": P["Theta_state"], "loo_note": loo_note}}

    # β_k (п.16): равные веса по типам ошибок, Σ = 1; правило δ_t -- запись delta_rule
    errs_train = Counter(e for c in train for e in c["err_types"])
    rule = man.get("delta_rule", DELTA_RULE_LEGACY)
    P["beta_k"] = _manual({e: _f(Fraction(1, len(ERR_TYPES))) for e in ERR_TYPES}, REF_BETA,
                          f"error_commands={sum(1 for c in train if c['err_types'])}"
                          + (f"; в δ_t не участвуют: delta_rule={rule} ({REF_FIX} п.1)" if rule != DELTA_RULE_LEGACY else ""))
    if rule == DELTA_RULE_LEGACY:
        delta_doc = {"rule": DELTA_RULE_LEGACY, "text": "δ_t = Σ β_k e_k", "source": {"manual": REF_BETA}}
    else:
        delta_doc = {"rule": rule, "text": DELTA_RULES[rule], "delta_fail": 1, "delta_success": 0,
                     "beta_k_used": False, "source": {"manual": man["ref"], "decision": REF_FIX + " п.1"},
                     "caller": "δ_t передаёт вызывающая сторона (check_sigma_2e.py; router.py в фазе 3), ядро не меняется"}

    # MANUAL Оператора: τ_D, Θ_crit, Θ_reset (close_2c п.2), R_t (п.20)
    for k in MANUAL_REQUIRED:
        if k in man:
            P[k] = _manual(man[k], man["ref"])
        else:
            P[k] = {"status": "MANUAL_PENDING", "value": None,
                    "source": {"manual": None, "pending": f"значение Оператора не задано ({REF_CLOSE2C if k != 'R_t' else REF_LAYOUT + ' п.20'})"}}
    for k, why in NO_DATA_FIXED.items():
        P[k] = _nodata(why)

    assert set(P) == set(PARAMS), sorted(set(PARAMS) ^ set(P))
    rows_2d = st["rows_all"]
    journal = {"file": JOURNAL_NAME, "sha16": rows_digest(rows_2d) if frozen else journal_digest(files),
               "lines_ok": len(rows_2d),
               "lines_bad": st["lines_bad"], "isolated": dict(st["isolated"])}
    if frozen:
        journal["sha16_scope"] = "строки seq <= holdout_2e.last_seq (канонический JSON); журнал после last_seq не влияет"
    sample = {"rule": REF_SAMPLE, "commands": len(train)}
    if frozen:
        sample["upto"] = "seq <= holdout_2e.last_seq из прежнего файла параметров (" + REF_FIX + " п.3; last_seq не меняется)"
    doc = {"schema": SCHEMA, "journal": journal, "holdout_2e": hinfo, "sample": sample, "delta_rule": delta_doc,
           "params": {k: P[k] for k in PARAMS}}
    if "eps_norm" in man:                                # решение 16.11 13:43 п.2: запись для фазы 3
        kern = {f: (_f(min(Fraction(1), eps / alpha_fr[f])) if eps is not None and f in alpha_fr else None)
                for f in sorted(man["eps_norm"])}
        doc["eps_norm"] = {"per_feature": dict(man["eps_norm"]), "source": {"manual": man["ref"], "decision": REF_CLOSE2E + " п.2"},
                           "use": "фаза 3: d_j/κ_j сравнивается с ε_norm_j по признаку (допуск d_∞, ядро фазы 3); ядро фазы 2 берёт скаляр ε_state",
                           "kernel_scalar_equivalent": kern,
                           "consistent_with_eps_state": all(kern[f] is not None and abs(kern[f] - man["eps_norm"][f]) < 1e-12
                                                            for f in kern)}
    if "dt_0" in man:                                    # решение 16.11 16:16 + 18:21 п.3: Δt_0 -- MANUAL (этап 3c)
        doc["dt_0"] = {"status": "MANUAL", "value": man["dt_0"], "unit": "s",
                       "source": {"manual": man["ref"], "decision": REF_DT0},
                       "use": "ядро фазы 3: λ_0 = exp(−Δt_0/τ_D), τ_D -- params.tau_D (abstraction_layer.params_from_doc)"}

    def _jn(k: str) -> "int | None":
        j = P[k]["source"].get("journal_note") if isinstance(P[k]["source"], dict) else None
        return j["source"].get("n") if isinstance(j, dict) and isinstance(j.get("source"), dict) else None
    th = P["Theta_state"]
    counts = {"lines_total": st["lines_total"], "lines_ok": st["lines_ok"], "lines_bad": st["lines_bad"],
              "rows_2d": len(rows_2d), "frozen": frozen,
              "commands": len(cmds), "last_seq_set": hinfo["last_seq"] is not None,
              "isolated": dict(st["isolated"]),
              "error_types": {e: errs_train.get(e, 0) for e in ERR_TYPES},
              "n": {k: (P[k]["source"].get("n") if isinstance(P[k]["source"], dict) else None) for k in PARAMS},
              "journal_n": {k: _jn(k) for k in PARAMS}, "delta_rule": rule,
              "sigma_features": len(calib), "states": {sss._code(f): len(states_of(spaces[f])) for f in sorted(spaces)},
              "theta_state_status": th["status"], "loo_n": loo_note["n"], "loo_guard_ok": loo_note.get("guard") == "ok",
              "eps_norm": ("eps_norm" in doc, doc.get("eps_norm", {}).get("consistent_with_eps_state")),
              "dt_0": doc["dt_0"]["status"] if "dt_0" in doc else None,
              "theta_state_loo_below_in_sample": (th["status"] == "JOURNAL" and th["value"] < th["source"]["in_sample_note"]["value"])}
    return {"doc": doc, "counts": counts, "missing": missing}


def dumps(doc: dict) -> bytes:
    return (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def render(res: dict, status: str) -> str:
    c = res["counts"]
    L = ["# calibrate_sigma (counters only; no s_exp/s_obs/ts/seq values)",
         f"journal: lines total={c['lines_total']} ok={c['lines_ok']} bad={c['lines_bad']}"
         + (f"; frozen 2d rows (seq <= last_seq)={c['rows_2d']}" if c["frozen"] else ""),
         f"commands={c['commands']} (all in 2d, no holdout); holdout_2e: seq > last_seq, last_seq "
         + ("set (value in params file)" if c["last_seq_set"] else "none (empty journal)")
         + (", frozen from --last-seq-from (unchanged)" if c["frozen"] else ""),
         "isolated: " + (" ".join(f"{sss._code(f)}={n}" for f, n in c["isolated"].items()) or "-"),
         "error types: " + " ".join(f"{e}={n}" for e, n in c["error_types"].items()),
         f"delta_rule: {c['delta_rule']}; Sigma features={c['sigma_features']}; state space sizes: "
         + (" ".join(f"{f}={n}" for f, n in c["states"].items()) or "-")
         + "; smoothing: Dirichlet mass 1/row (T, pi_0 Lidstone; Z offset back-off); Theta_state "
         + ("MANUAL (LOO record in loo_note" if c["theta_state_status"] == "MANUAL" else "leave-one-out (record in loo_note")
         + f", n={c['loo_n']}, guard ok={c['loo_guard_ok']})"
         + (" (below in-sample)" if c["theta_state_loo_below_in_sample"] else "")
         + (f"; eps_norm record: consistent with eps_state={c['eps_norm'][1]}" if c["eps_norm"][0] else "")
         + (f"; dt_0 record: {c['dt_0']}" if c.get("dt_0") else "")]
    st = Counter()
    for k in PARAMS:
        p = res["doc"]["params"][k]
        st[p["status"]] += 1
        n, jn = c["n"][k], c["journal_n"][k]
        L.append(f"  [{p['status']}] {k}" + (f" n={n}" if n is not None else "")
                 + (f" journal_note_n={jn}" if jn is not None else ""))
    L.append("params: " + " ".join(f"{s}={st[s]}" for s in ("JOURNAL", "MANUAL", "MANUAL_PENDING", "NO_DATA")))
    L.append(f"status: {status}")
    return "\n".join(L)


def _out(s: str) -> None:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "").replace("_", "")
    if enc != "utf8" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.write(s + "\n")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Offline calibration of Σ params (phase 2d). Counters-only stdout.")
    ap.add_argument("--path", default=str(DEFAULT_JOURNAL))
    ap.add_argument("--manual", default=None, help="JSON with Operator values (tau_D, Theta_crit, Theta_reset, R_t, ref; "
                                                   "optional kappa, eps_state, tau_b, delta_rule, Theta_state, eps_norm, dt_0)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--last-seq-from", default=None, help="params file: keep its holdout_2e.last_seq, use rows seq <= last_seq")
    ap.add_argument("--dry-run", action="store_true", help="compute, do not write")
    a = ap.parse_args(argv)
    manual = None
    if a.manual:
        try:
            manual = json.loads(Path(a.manual).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            _out(f"status: INVALID_MANUAL ({type(exc).__name__}) -> no file written")
            return EXIT_INVALID
    try:
        ls = last_seq_from(Path(a.last_seq_from)) if a.last_seq_from else None
        res = calibrate(Path(a.path), manual, last_seq=ls)
    except ManualError as exc:
        _out(f"status: INVALID_MANUAL ({exc}) -> no file written")
        return EXIT_INVALID
    if res["missing"]:
        _out(render(res, "MANUAL_PENDING (missing: " + ", ".join(res["missing"]) + ") -> no file written"))
        return EXIT_PENDING
    if a.dry_run:
        _out(render(res, "OK (dry-run, no file written)"))
        return EXIT_OK
    write_atomic(Path(a.out), dumps(res["doc"]))
    _out(render(res, "OK -> written " + ("config/sigma_params.json" if Path(a.out) == DEFAULT_OUT else "--out")))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
