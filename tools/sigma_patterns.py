"""tools/sigma_patterns.py -- фоновый сбор паттернов теневого журнала Σ (фаза 3, этап 3a). Только stdlib.

Этап 3a п.1 (УТВЕРЖДЁН Оператором 16.11 15:36); правила математического аудита §4.
Читает logs/sigma_shadow.jsonl (+ ротированный .1) тем же разбором строк, что sigma_shadow_stats.py (белый список полей,
битые строки -> lines_bad; признак `volume` изолирован, как в ). Выводит ТОЛЬКО агрегаты:
  * сессии: разрыв -- пауза между соседними командами > gap_s (по умолчанию = τ_D из config/sigma_params.json, 60 с;
    обоснование: после паузы > τ_D остаток диссонанса e^(−Δt/τ_D)·D < 0,37·D -- серия сбоев прежней сессии почти
    забыта, следующая команда начинает новую серию); число, одиночные, длины (команд), длительности (с);
  * темп внутри сессий: Δt между соседними командами одной сессии -- n, min/q10/q25/медиана/q75/q90/max, гистограмма;
    чувствительность к порогу разрыва (30 / 60 / 120 с); разбивка по границе 2e (seq <= / > holdout_2e.last_seq файла
    параметров; само значение last_seq не выводится);
  * доли признаков и действий (команды, исходы), шаги признаков (цель − состояние до команды, гистограмма),
    значения вне шкалы 0..κ_j (счётчик, наблюдение вне шкалы), ошибки по типам (по командам; FAIL без err = other), серии сбоев
    подряд внутри сессий (правило δ any_fail_1: сбой = outcome FAIL или err), час суток (локальное время ПК, без дат).
Квантили -- линейная интерполяция между порядковыми статистиками (Hyndman–Fan тип 7), округление до 0,001 с.
НЕ выводит: значения признаков из строк, отдельные ts / seq, коды узлов меню, текст строк. Имена action / feature --
только если это код [A-Za-z0-9_]{1,48}, иначе «?». Пустой или отсутствующий журнал -> нули, код возврата 0.

Запуск (интерпретатор службы):
  <python> tools\\sigma_patterns.py [--path P] [--params F] [--gap S] [--out J] [--json]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("sigma_shadow_stats_for_patterns", _HERE / "sigma_shadow_stats.py")
sss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sss)

ROOT = _HERE.parent
DEFAULT_PATH = ROOT / "logs" / "sigma_shadow.jsonl"
DEFAULT_PARAMS = ROOT / "config" / "sigma_params.json"
SCHEMA = "sigma_patterns/3a-v1"
GAP_FALLBACK_S = 60.0                  # τ_D MANUAL (решение 16.11 12:04 п.1), если файла параметров нет
SENS_GAPS_S = (30.0, 60.0, 120.0)
TEMPO_BINS_S = (2.0, 5.0, 10.0, 20.0, 40.0, 60.0, 120.0)   # верхние границы корзин гистограммы Δt
ISOLATE_DEFAULT = sss.ISOLATE_DEFAULT
ERR_TYPES = tuple(sss.ERRS) + ("other",)


# ---------------------------------------------------------------- статистика
def quantile(sorted_vals: list, p: float) -> "float | None":
    """Квантиль типа 7 (линейная интерполяция). Пустой список -> None."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    h = (n - 1) * p
    lo = math.floor(h)
    hi = min(lo + 1, n - 1)
    return float(sorted_vals[lo]) + (h - lo) * (float(sorted_vals[hi]) - float(sorted_vals[lo]))


def _r(x: "float | None") -> "float | None":
    return None if x is None else round(float(x), 3)


def summary(vals: list) -> dict:
    v = sorted(float(x) for x in vals)
    out: dict = {"n": len(v)}
    for k, p in (("min", 0.0), ("q10", 0.10), ("q25", 0.25), ("median", 0.50), ("q75", 0.75), ("q90", 0.90),
                 ("max", 1.0)):
        out[k] = _r(quantile(v, p))
    return out


def tempo_hist(vals: list) -> dict:
    h: dict = {}
    prev = 0.0
    for b in TEMPO_BINS_S:
        h[f"{prev:g}-{b:g}"] = sum(1 for x in vals if prev < x <= b) + (sum(1 for x in vals if x == 0) if prev == 0 else 0)
        prev = b
    h[f">{prev:g}"] = sum(1 for x in vals if x > prev)
    return h


def _len_bucket(n: int) -> str:
    return "1" if n == 1 else "2-3" if n <= 3 else "4-9" if n <= 9 else "10+"


# ---------------------------------------------------------------- журнал -> команды
def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _key(v: Any) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(round(f, 6))


def load(path: Path, isolate: dict = ISOLATE_DEFAULT) -> "tuple[list, list, dict]":
    """(строки Σ без изолированных, все строки, счётчики разбора)."""
    st: dict = {"files": {}, "lines_total": 0, "lines_bad": 0}
    rows_all = list(sss._iter_lines(sss.journal_files(Path(path)), st))
    st["lines_ok"] = len(rows_all)
    iso = {f for f in isolate if isinstance(f, str)}
    iso_seqs = sss._isolated_seqs(rows_all, iso)

    def _iso(r: dict) -> bool:
        return (r["seq"] in iso_seqs) if sss._int_ts(r.get("seq")) else (r.get("feature") in iso)
    st["isolated"] = {sss._code(f): len({r["seq"] for r in rows_all if _iso(r) and sss._int_ts(r.get("seq"))
                                          and r.get("feature") == f}) for f in sorted(iso)}
    st["all_seq"] = len({r["seq"] for r in rows_all if sss._int_ts(r.get("seq"))})
    return [r for r in rows_all if not _iso(r)], rows_all, st


def build_commands(rows: list) -> list:
    """Команды с outcome (по seq), упорядочены по моменту команды t_ms (первая отправка, иначе первая строка).
    failed -- правило any_fail_1 (outcome FAIL или err в любой строке команды); before -- считанное до первой отправки
    (тот же признак), иначе считанное в outcome прошлой команды того же признака."""
    by: dict = defaultdict(list)
    for i, r in enumerate(rows):
        if sss._int_ts(r.get("seq")):
            by[r["seq"]].append((i, r))
    cmds = []
    for seq, items in by.items():
        outs = [r for _, r in items if r["step"] == "outcome"]
        if not outs:
            continue
        o = outs[-1]
        feat = o.get("feature")
        sends = [i for i, r in items if r["step"] == "send"]
        first_send = min(sends) if sends else None
        t_ms = next((r["ts"] for i, r in items if i == first_send), None) if first_send is not None else None
        if t_ms is None:
            t_ms = min(r["ts"] for _, r in items)
        pre = None
        if first_send is not None:
            for i, r in items:
                if i < first_send and r["step"] == "readback" and r.get("feature") == feat and _num(r.get("s_obs")):
                    pre = r["s_obs"]
        errs = {r["err"] for _, r in items if r.get("err") is not None}
        fail = o.get("outcome") == "FAIL"
        vals = [r[k] for _, r in items for k in ("s_exp", "s_obs") if _num(r.get(k)) and r.get("feature") == feat]
        cmds.append({"seq": seq, "t_ms": t_ms, "feature": feat, "action": o.get("action"),
                     "outcome": o.get("outcome") if o.get("outcome") in sss.OUTCOMES else "?",
                     "target": o.get("s_exp") if _num(o.get("s_exp")) else None,
                     "after": o.get("s_obs") if _num(o.get("s_obs")) else None, "pre": pre,
                     "failed": bool(fail or errs),
                     "err_types": sorted(e if e in sss.ERRS else "?" for e in errs) if errs else (["other"] if fail else []),
                     "values": vals})
    cmds.sort(key=lambda c: (c["t_ms"], c["seq"]))
    last: dict = {}
    for c in cmds:                      # состояние «до»: pre, иначе после прошлой команды признака (по времени)
        f = c["feature"]
        c["before"] = c["pre"] if c["pre"] is not None else last.get(f)
        if f is not None and c["after"] is not None:
            last[f] = c["after"]
    return cmds


def split_sessions(cmds: list, gap_s: float) -> list:
    """Сессии: новая начинается, когда пауза до предыдущей команды > gap_s."""
    out: list = []
    prev = None
    for c in cmds:
        if prev is None or (c["t_ms"] - prev["t_ms"]) / 1000.0 > gap_s:
            out.append([])
        out[-1].append(c)
        prev = c
    return out


def within_intervals(sessions: list) -> list:
    """[(Δt с, команда-после)] между соседними командами одной сессии."""
    return [((s[i]["t_ms"] - s[i - 1]["t_ms"]) / 1000.0, s[i]) for s in sessions for i in range(1, len(s))]


def tempo_values(path: Path, gap_s: float, isolate: dict = ISOLATE_DEFAULT) -> list:
    """Δt внутри сессий (с), отсортировано. Для калибровки интегратора; наружу -- только агрегаты."""
    rows, _, _ = load(path, isolate)
    return sorted(dt for dt, _ in within_intervals(split_sessions(build_commands(rows), gap_s)))


# ---------------------------------------------------------------- файл параметров
def read_params(path: "Path | None") -> dict:
    """gap (τ_D), κ_j, last_seq из config/sigma_params.json; нет файла / поля -> None (без ошибки)."""
    out: dict = {"tau_D": None, "kappa": {}, "last_seq": None, "file": "absent"}
    if path is None:
        return out
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as exc:
        out["file"] = f"unreadable ({type(exc).__name__})"
        return out
    out["file"] = "ok"
    p = doc.get("params") if isinstance(doc, dict) else None
    p = p if isinstance(p, dict) else {}
    td = (p.get("tau_D") or {}).get("value") if isinstance(p.get("tau_D"), dict) else None
    out["tau_D"] = float(td) if _num(td) and td > 0 else None
    kj = (p.get("kappa_j") or {}).get("value") if isinstance(p.get("kappa_j"), dict) else None
    if isinstance(kj, dict):
        out["kappa"] = {k: float(v) for k, v in kj.items() if isinstance(k, str) and _num(v) and v > 0}
    h = doc.get("holdout_2e") if isinstance(doc, dict) else None
    ls = h.get("last_seq") if isinstance(h, dict) else None
    out["last_seq"] = ls if sss._int_ts(ls) else None
    return out


# ---------------------------------------------------------------- агрегаты
def _tempo_block(sessions: list) -> dict:
    dts = [dt for dt, _ in within_intervals(sessions)]
    return summary(dts) | {"hist_s": tempo_hist(dts)}


def compute(path: Path = DEFAULT_PATH, params: "Path | None" = DEFAULT_PARAMS, gap_s: "float | None" = None,
            isolate: dict = ISOLATE_DEFAULT) -> dict:
    pr = read_params(params)
    gap_src = "--gap" if gap_s is not None else ("tau_D из файла параметров" if pr["tau_D"] else "запасное 60 с")
    gap = float(gap_s if gap_s is not None else (pr["tau_D"] or GAP_FALLBACK_S))
    rows, rows_all, st = load(path, isolate)
    cmds = build_commands(rows)
    sess = split_sessions(cmds, gap)
    lens = [len(s) for s in sess]
    durs = [(s[-1]["t_ms"] - s[0]["t_ms"]) / 1000.0 for s in sess]
    between = [(sess[i][0]["t_ms"] - sess[i - 1][-1]["t_ms"]) / 1000.0 for i in range(1, len(sess))]
    n = len(cmds)

    feats = Counter(sss._code(c["feature"]) if c["feature"] is not None else "-" for c in cmds)
    acts: dict = defaultdict(Counter)
    for c in cmds:
        acts[sss._code(c["action"])][c["outcome"]] += 1

    steps: dict = defaultdict(Counter)
    for c in cmds:
        if c["feature"] is not None and c["target"] is not None and c["before"] is not None:
            steps[sss._code(c["feature"])][_key(c["target"] - c["before"])] += 1
    scale: dict = {}
    for f, k in sorted(pr["kappa"].items()):
        mine = [c for c in cmds if c["feature"] == f]
        bad = sum(1 for c in mine for v in c["values"] if v < 0 or v > k)
        scale[sss._code(f)] = {"kappa": k, "commands": len(mine), "values_out_of_scale": bad}

    err = Counter()
    for c in cmds:
        for e in c["err_types"]:
            err[e] += 1
    runs: list = []
    for s in sess:                       # серии сбоев подряд внутри сессии
        k = 0
        for c in s + [None]:
            if c is not None and c["failed"]:
                k += 1
            elif k:
                runs.append(k)
                k = 0
    hours = Counter()
    for c in cmds:
        try:
            hours[datetime.fromtimestamp(c["t_ms"] / 1000).strftime("%H")] += 1
        except (OverflowError, OSError, ValueError):
            hours["?"] += 1

    sens = {}
    for g in SENS_GAPS_S:
        ss = split_sessions(cmds, g)
        d = sorted(dt for dt, _ in within_intervals(ss))
        sens[f"{g:g}"] = {"sessions": len(ss), "n": len(d), "q10": _r(quantile(d, .1)), "median": _r(quantile(d, .5)),
                          "q90": _r(quantile(d, .9))}
    seg = None
    if pr["last_seq"] is not None:
        ls = pr["last_seq"]
        wi = within_intervals(sess)
        seg = {"rule": "по команде-после интервала: seq <= / > holdout_2e.last_seq (значение не выводится)",
               "upto_last_seq": summary([dt for dt, c in wi if c["seq"] <= ls]),
               "after_last_seq": summary([dt for dt, c in wi if c["seq"] > ls]),
               "commands_upto": sum(1 for c in cmds if c["seq"] <= ls),
               "commands_after": sum(1 for c in cmds if c["seq"] > ls)}

    return {
        "schema": SCHEMA,
        "journal": {"files": st["files"], "lines_total": st["lines_total"], "lines_ok": st["lines_ok"],
                    "lines_bad": st["lines_bad"]},
        "commands": {"all_seq": st["all_seq"], "sigma_with_outcome": n, "isolated": st["isolated"]},
        "session_rule": {"gap_s": gap, "gap_source": gap_src, "params_file": pr["file"],
                         "basis": "пауза > τ_D: остаток e^(−Δt/τ_D)·D < 0,37·D -- прежняя серия сбоев почти забыта "
                                  "(решение 14:51 п.2: межсессионные паузы отсекаются)"},
        "sessions": {"count": len(sess), "singletons": sum(1 for x in lens if x == 1),
                     "length_cmds": summary(lens), "length_hist": dict(sorted(Counter(_len_bucket(x) for x in lens).items())),
                     "duration_s": summary(durs), "between_sessions_s": summary(between)},
        "tempo_within": _tempo_block(sess),
        "tempo_sensitivity": sens,
        "tempo_segments_2e": seg,
        "features": {f: {"commands": c, "share": _r(c / n)} for f, c in sorted(feats.items())},
        "actions": {a: {"commands": sum(c.values()), "share": _r(sum(c.values()) / n),
                        "outcome": {o: c.get(o, 0) for o in (*sss.OUTCOMES, "?") if o in sss.OUTCOMES or c.get(o)}}
                    for a, c in sorted(acts.items())},
        "steps": {f: {"n": sum(c.values()), "hist": dict(sorted(c.items(), key=lambda kv: float(kv[0])))}
                  for f, c in sorted(steps.items())},
        "scale": scale,
        "errors": {"fail_commands_any_fail_1": sum(1 for c in cmds if c["failed"]),
                   "by_type": {e: err.get(e, 0) for e in ERR_TYPES} | ({"?": err["?"]} if err.get("?") else {}),
                   "fail_runs_within_sessions": {"count": len(runs), "max_len": max(runs, default=0),
                                                 "hist": {str(k): v for k, v in sorted(Counter(runs).items())}}},
        "hour_of_day_local": dict(sorted(hours.items())),
    }


def render(st: dict) -> str:
    def q(s: dict) -> str:
        return (f"n={s['n']} min={s['min']} q10={s['q10']} q25={s['q25']} median={s['median']} q75={s['q75']} "
                f"q90={s['q90']} max={s['max']}")
    j, c, sr, se = st["journal"], st["commands"], st["session_rule"], st["sessions"]
    L = ["# sigma_patterns (aggregates only; no feature values, no ts/seq, no node codes)",
         f"journal: lines total={j['lines_total']} ok={j['lines_ok']} bad={j['lines_bad']}; files: "
         + ", ".join(f"{k}={v}" for k, v in j["files"].items()),
         f"commands: all_seq={c['all_seq']} sigma_with_outcome={c['sigma_with_outcome']} isolated: "
         + (" ".join(f"{k}={v}" for k, v in c["isolated"].items()) or "-"),
         f"session gap: > {sr['gap_s']:g} s ({sr['gap_source']}; params file {sr['params_file']})",
         f"sessions: count={se['count']} singletons={se['singletons']} length_hist="
         + " ".join(f"{k}:{v}" for k, v in se["length_hist"].items()),
         "  length (commands): " + q(se["length_cmds"]),
         "  duration (s): " + q(se["duration_s"]),
         "  between sessions (s): " + q(se["between_sessions_s"]),
         "tempo within sessions dt (s): " + q(st["tempo_within"]),
         "  hist: " + " ".join(f"{k}:{v}" for k, v in st["tempo_within"]["hist_s"].items()),
         "  sensitivity gap: " + "; ".join(f"{g}s -> sessions={v['sessions']} n={v['n']} q10={v['q10']} "
                                            f"median={v['median']} q90={v['q90']}" for g, v in st["tempo_sensitivity"].items())]
    sg = st["tempo_segments_2e"]
    if sg:
        L.append(f"  by 2e boundary: upto commands={sg['commands_upto']} {q(sg['upto_last_seq'])}")
        L.append(f"                  after commands={sg['commands_after']} {q(sg['after_last_seq'])}")
    else:
        L.append("  by 2e boundary: NO_DATA (no holdout_2e.last_seq in params file)")
    L.append("features: " + " ".join(f"{f}={v['commands']}({v['share']})" for f, v in st["features"].items()))
    L.append("actions: " + " ".join(f"{a}={v['commands']}[" + ",".join(f"{o}:{n}" for o, n in v["outcome"].items()) + "]"
                                   for a, v in st["actions"].items()))
    for f, v in st["steps"].items():
        L.append(f"steps {f} (target - before): n={v['n']} " + " ".join(f"{k}:{n}" for k, n in v["hist"].items()))
    for f, v in st["scale"].items():
        L.append(f"scale {f}: kappa={v['kappa']:g} commands={v['commands']} values_out_of_scale={v['values_out_of_scale']}")
    e = st["errors"]
    L.append(f"errors: fail_commands(any_fail_1)={e['fail_commands_any_fail_1']} by_type: "
             + " ".join(f"{k}={v}" for k, v in e["by_type"].items()))
    fr = e["fail_runs_within_sessions"]
    L.append(f"fail runs within sessions: count={fr['count']} max_len={fr['max_len']} hist="
             + (" ".join(f"{k}:{v}" for k, v in fr["hist"].items()) or "-"))
    L.append("hour of day (local): " + (" ".join(f"{h}:{n}" for h, n in st["hour_of_day_local"].items()) or "-"))
    return "\n".join(L)


def dumps(doc: dict) -> bytes:
    return (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def _out(s: str) -> None:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "").replace("_", "")
    if enc != "utf8" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.write(s + "\n")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Aggregated patterns of logs/sigma_shadow.jsonl (no values).")
    ap.add_argument("--path", default=str(DEFAULT_PATH))
    ap.add_argument("--params", default=str(DEFAULT_PARAMS), help="config/sigma_params.json (tau_D, kappa_j, last_seq)")
    ap.add_argument("--gap", type=float, default=None, help="session gap, s (default: tau_D from params)")
    ap.add_argument("--out", default=None, help="write aggregates JSON here")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.gap is not None and not (math.isfinite(a.gap) and a.gap > 0):
        _out("status: INVALID_GAP")
        return 2
    st = compute(Path(a.path), Path(a.params) if a.params else None, a.gap)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_bytes(dumps(st))
    _out(json.dumps(st, ensure_ascii=True, sort_keys=True) if a.json else render(st))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
