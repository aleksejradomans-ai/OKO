"""tools/sigma_shadow_stats.py -- счётчики теневого журнала Σ (фаза 2, этап 2c). Только stdlib.

Читает logs/sigma_shadow.jsonl (+ ротированный logs/sigma_shadow.jsonl.1, он старше) и выводит ТОЛЬКО счётчики:
строк; команд (seq); по step; по action x outcome; по feature -- число пар (s_exp, s_obs); по err (FAIL без err =
other); переходов меню -- число рёбер и число различных; период (первая / последняя метка); пороги «достаточно»
этапа 2c (утв. Оператором 15.11 21:23) -- выполнен / нет.

НЕ выводит: значения s_exp / s_obs, отдельные ts, коды узлов меню (node_from / node_to), любой текст строки.
Имена action / feature печатаются, только если это код [A-Za-z0-9_]{1,48} (как в схеме журнала), иначе -- «?».
Битая строка (не JSON, не объект, без step/ts, поле вне белого списка, текст вместо кода) пропускается и считается в lines_bad.
Пустой или отсутствующий файл -> нули, код возврата 0.

(16.11, 2c-fix): признак `volume` ИЗОЛИРОВАН (решение 16.11 01:00, правка (б)): звук ТВ идёт на внешний аудиовыход
(Bluetooth / колонки), громкость ТВ читается как 0 -> каждая volume_* = FAIL verify_fail. Команды изолированного признака
не входят в error_commands / err, пары eps_state, T_theta(API), tau_b, pi_0 и пороги; выводятся одной строкой
«volume: изолирован (внешний аудиовыход) ...» (только счётчики). Поля журнала не меняются. CLI изолирует по умолчанию
(--no-isolate -- сырой счёт); compute(path) без аргумента isolate -- прежний сырой счёт (совместимость ).

(16.11, фаза 3, этап 3c; решение Оператора 15:36 п.3): белый список дополнен полями писателя d_cmd (Δ_cmd --
величина шага команды STEP, целое со знаком) и s_raw (сырое значение признака до приведения к числу/шкале). Старые
строки без этих полей -- прежние строки (поля = NO_DATA), разбор и все прежние счётчики не меняются; новый счётчик
fields_3c -- только число строк outcome с d_cmd / s_raw (значения не печатаются).

(16.11, фаза 3, этап 3d): белый список дополнен полями вердикта Σ (коды и флаги): sigma, sigma_p (EXECUTE |
OBSERVE | CLARIFY | HALT), sigma_why (код), a_t, sigma_live (0/1); шаг "sigma" -- строка боевого режима без исполнения
(в счётчик step не входит, прежние счётчики не меняются). Новый счётчик fields_3d: строки outcome с вердиктом, по
вердикту, a_t = 1, sigma_live = 1 и метрика gate A фазы 3 -- доля VERIFIED после SENT (команды verified с записью в ТВ,
исход PASS с чтением; изолированный признак не входит). Значения не печатаются.

Запуск (интерпретатор службы):  <python> tools\\sigma_shadow_stats.py [--path P] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "logs" / "sigma_shadow.jsonl"
FIELDS = frozenset({"ts", "step", "cmd_kind", "action", "feature", "s_exp", "s_obs",
                    "node_from", "node_to", "outcome", "err", "latency_ms", "seq",
                    "d_cmd", "s_raw",                        # (этап 3c): Δ_cmd и сырое значение, NO_DATA в старых
                    "sigma", "sigma_p", "sigma_why", "a_t", "sigma_live"})   # (этап 3d): вердикт Σ
STEPS = ("send", "ack", "readback", "outcome", "menu_step", "error")
STEPS_3D = STEPS + ("sigma",)          # допустимы в строке; в счётчик step не входят
SIGMAS = ("EXECUTE", "OBSERVE", "CLARIFY", "HALT")
OUTCOMES = ("PASS", "WARN", "FAIL")
ERRS = ("net", "parse_fail", "cancel", "verify_fail", "warn")
_CODE_RE = re.compile(r"^[A-Za-z0-9_]{1,48}$")

# Пороги 2c («Порог достаточно», утв. 15.11 21:23).
TH_PAIRS = 30          # ε_state, κ_j, α_j: пар (s_exp, s_obs) по признаку, из них >= 1 не-PASS
TH_NONPASS = 1
TH_FEAT_ACTION = 10    # T_θ (API): команд по паре (признак, действие)
TH_MENU_STEPS = 50     # T_θ (меню): шагов menu_step
TH_EDGE_MIN = 3        # ребро, встреченное < 3 раз, -- NO_DATA
TH_TAU_B = 20          # τ_b: пар соседних команд по одному признаку
TH_READBACK = 30       # π_0: считываний
TH_ERRORS = 10         # τ_D, Θ_crit, Θ_reset: ошибок любого типа (включая other)

# признаки вне калибровки -> причина (печатается). Изоляция по имени признака: ТВ не отдаёт признак внешнего
# аудиовыхода в журнал (белый список полей прежний).
ISOLATE_DEFAULT: dict = {"volume": "внешний аудиовыход"}


def _code(v: Any) -> str:
    return v if isinstance(v, str) and _CODE_RE.match(v) else "?"


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _int_ts(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _valid(row: Any) -> bool:
    """Схема строки как у писателя (tv_bot_handler.shadow_validate), без cmd_kind: он не выводится."""
    if not isinstance(row, dict) or not set(row) <= FIELDS:
        return False
    if row.get("step") == "sigma":       # строка вердикта боевого режима -- та же схема, шаг из STEPS_3D
        row = dict(row, step="outcome")
    if row.get("step") not in STEPS or not _int_ts(row.get("ts")):
        return False
    for k in ("sigma", "sigma_p"):
        if k in row and row[k] not in SIGMAS:
            return False
    if "sigma_why" in row and not (isinstance(row["sigma_why"], str) and _CODE_RE.match(row["sigma_why"])):
        return False
    for k in ("a_t", "sigma_live"):
        if k in row and (isinstance(row[k], bool) or row[k] not in (0, 1)):
            return False
    for k in ("seq", "latency_ms"):
        if k in row and not _int_ts(row[k]):
            return False
    for k in ("s_exp", "s_obs", "s_raw"):
        if k in row and not _is_num(row[k]):
            return False
    if "d_cmd" in row and not (isinstance(row["d_cmd"], int) and not isinstance(row["d_cmd"], bool)):
        return False
    for k in ("action", "feature"):
        if k in row and not (isinstance(row[k], str) and _CODE_RE.match(row[k])):
            return False
    for k in ("node_from", "node_to"):
        if k in row and not (isinstance(row[k], str) and 0 < len(row[k]) <= 96):
            return False
    if "outcome" in row and row["outcome"] not in OUTCOMES:
        return False
    if "err" in row and row["err"] not in ERRS:
        return False
    return True


def journal_files(path: Path) -> list[Path]:
    """Старший (.1) первым, затем текущий."""
    return [path.with_name(path.name + ".1"), path]


def _iter_lines(files: Iterable[Path], st: dict) -> Iterable[dict]:
    for f in files:
        key = "main" if not f.name.endswith(".1") else "rotated"
        try:
            raw = f.read_bytes()
        except FileNotFoundError:
            st["files"][key] = "absent"
            continue
        except OSError as exc:
            st["files"][key] = f"unreadable ({type(exc).__name__})"
            continue
        n = 0
        for b in raw.splitlines():
            if not b.strip():
                continue
            n += 1
            st["lines_total"] += 1
            try:
                row = json.loads(b.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                st["lines_bad"] += 1
                continue
            if not _valid(row):
                st["lines_bad"] += 1
                continue
            yield row
        st["files"][key] = f"lines={n}"


def _isolated_seqs(rows: list, iso: "set[str]") -> set:
    """seq команд изолированного признака: по feature строки outcome; команда без outcome -- по любой своей строке."""
    if not iso:
        return set()
    out_feat: dict = {}
    any_feat: dict = defaultdict(set)
    for r in rows:
        if not _int_ts(r.get("seq")):
            continue
        if r["step"] == "outcome":
            out_feat[r["seq"]] = r.get("feature")
        if r.get("feature") is not None:
            any_feat[r["seq"]].add(r["feature"])
    seqs = set(out_feat) | set(any_feat)
    return {s for s in seqs if (out_feat[s] in iso if s in out_feat else bool(any_feat[s] & iso))}


def compute(path: Path = DEFAULT_PATH, isolate: "Iterable[str] | dict" = ()) -> dict:
    st: dict[str, Any] = {"files": {}, "lines_total": 0, "lines_bad": 0}
    rows_all = list(_iter_lines(journal_files(Path(path)), st))
    st["lines_ok"] = len(rows_all)
    iso = {f for f in isolate if isinstance(f, str) and _CODE_RE.match(f)}
    reasons = isolate if isinstance(isolate, dict) else {}
    iso_seqs = _isolated_seqs(rows_all, iso)
    # Строки изолированных команд (и строки без seq с изолированным признаком) не идут в счётчики Σ ниже.
    def _iso_row(r: dict) -> bool:
        return (r["seq"] in iso_seqs) if _int_ts(r.get("seq")) else (r.get("feature") in iso)
    rows = [r for r in rows_all if not _iso_row(r)]
    out_rows = [r for r in rows_all if _iso_row(r)]
    isolated: dict = {}
    for f in sorted(iso):
        mine = {r["seq"] for r in out_rows if _int_ts(r.get("seq")) and r.get("feature") == f}
        fo = [r for r in out_rows if r["step"] == "outcome" and r.get("seq") in mine]
        isolated[f] = {"reason": reasons.get(f, "isolated"), "commands": len(mine),
                       "outcome": {o: sum(1 for r in fo if r.get("outcome") == o) for o in OUTCOMES},
                       "readback": sum(1 for r in out_rows if r["step"] == "readback" and r.get("seq") in mine)}

    # Описательные счётчики (строки, команды, step, action x outcome, период) -- по ВСЕМ строкам, как в «счётчики журнала Σ».
    steps = Counter(r["step"] for r in rows_all)
    seqs = {r["seq"] for r in rows_all if _int_ts(r.get("seq"))}
    outcomes_all = [r for r in rows_all if r["step"] == "outcome"]
    outcomes = [r for r in rows if r["step"] == "outcome"]

    action_outcome: dict[str, Counter] = defaultdict(Counter)
    for r in outcomes_all:
        oc = r.get("outcome") if r.get("outcome") in OUTCOMES else "?"
        action_outcome[_code(r.get("action"))][oc] += 1
    # Счётчики для Σ (пары, T_theta, tau_b, ошибки, pi_0) -- без изолированных признаков.
    feat_pairs: dict[str, Counter] = defaultdict(Counter)
    feat_action: Counter = Counter()
    feat_ts: dict[str, list[int]] = defaultdict(list)
    for r in outcomes:
        oc = r.get("outcome") if r.get("outcome") in OUTCOMES else "?"
        feat = r.get("feature")
        if feat is not None:
            f = _code(feat)
            feat_action[(f, _code(r.get("action")))] += 1
            feat_ts[f].append(r["ts"])
            if _is_num(r.get("s_exp")) and _is_num(r.get("s_obs")):
                feat_pairs[f]["pairs"] += 1
                if oc != "PASS":
                    feat_pairs[f]["non_pass"] += 1

    # Ошибки -- по командам (seq): один код один раз на команду; FAIL без err ни в одной строке команды -> other.
    by_seq: dict[Any, dict] = defaultdict(lambda: {"errs": set(), "fail": False})
    for i, r in enumerate(rows):
        k = r["seq"] if _int_ts(r.get("seq")) else ("noseq", i)
        e = r.get("err")
        if e is not None:
            by_seq[k]["errs"].add(e if e in ERRS else "?")
        if r["step"] == "outcome" and r.get("outcome") == "FAIL":
            by_seq[k]["fail"] = True
    err_counts: Counter = Counter()
    err_cmds = 0
    for v in by_seq.values():
        if v["errs"] or v["fail"]:
            err_cmds += 1
        for e in v["errs"]:
            err_counts[e] += 1
        if v["fail"] and not v["errs"]:
            err_counts["other"] += 1

    edges: Counter = Counter()
    for r in rows:
        if r["step"] == "menu_step" and isinstance(r.get("node_from"), str) and isinstance(r.get("node_to"), str):
            edges[(r["node_from"], r["node_to"])] += 1
    menu = {"steps": steps.get("menu_step", 0), "edges": sum(edges.values()), "edges_distinct": len(edges),
            "edges_distinct_ge3": sum(1 for n in edges.values() if n >= TH_EDGE_MIN),
            "edges_distinct_lt3": sum(1 for n in edges.values() if n < TH_EDGE_MIN)}

    tau_b = {f: max(0, len(ts) - 1) for f, ts in feat_ts.items()}
    readbacks = [r for r in rows if r["step"] == "readback"]
    rb = {"rows": len(readbacks), "with_s_obs": sum(1 for r in readbacks if _is_num(r.get("s_obs")))}
    outs3 = [r for r in rows_all if r["step"] == "outcome"]
    fields_3c = {"outcome_rows": len(outs3), "with_d_cmd": sum(1 for r in outs3 if "d_cmd" in r),
                 "with_s_raw": sum(1 for r in outs3 if "s_raw" in r)}

    # вердикт Σ и метрика gate A (доля VERIFIED после SENT) -- по командам с вердиктом, без изолированных
    with_v = [r for r in outs3 if "sigma" in r]
    sent_seq = {r["seq"] for r in rows if r["step"] == "send" and _int_ts(r.get("seq"))}
    ga = [r for r in outcomes if "sigma" in r and r.get("cmd_kind") == "verified" and r.get("seq") in sent_seq]
    ga_ok = sum(1 for r in ga if r.get("outcome") == "PASS" and _is_num(r.get("s_obs")))
    fields_3d = {"outcome_with_sigma": len(with_v),
                 "sigma": {v: sum(1 for r in with_v if r.get("sigma") == v) for v in SIGMAS},
                 "sigma_p": {v: sum(1 for r in with_v if r.get("sigma_p") == v) for v in SIGMAS},
                 "a_t1": sum(1 for r in with_v if r.get("a_t") == 1),
                 "live1": sum(1 for r in rows_all if r.get("sigma_live") == 1),
                 "sigma_rows": sum(1 for r in rows_all if r["step"] == "sigma"),
                 "gateA_sent": len(ga), "gateA_verified": ga_ok,
                 "gateA_ratio": round(ga_ok / len(ga), 4) if ga else None}

    ts_all = [r["ts"] for r in rows_all]
    period = {"first": None, "last": None, "hours": 0.0}
    if ts_all:
        a, b = min(ts_all), max(ts_all)
        period = {"first": _fmt(a), "last": _fmt(b), "hours": round((b - a) / 3_600_000, 2)}

    st.update({
        "commands_seq": len(seqs),
        "commands_with_outcome": len({r["seq"] for r in outcomes_all if _int_ts(r.get("seq"))}),
        "step": {s: steps.get(s, 0) for s in STEPS},
        "action_outcome": {a: {o: c.get(o, 0) for o in (*OUTCOMES, "?") if o in OUTCOMES or c.get(o)}
                           for a, c in sorted(action_outcome.items())},
        "feature_pairs": {f: {"pairs": c["pairs"], "non_pass": c["non_pass"]} for f, c in sorted(feat_pairs.items())},
        "feature_action_cmds": {f"{f}|{a}": n for (f, a), n in sorted(feat_action.items())},
        "err": {e: err_counts.get(e, 0) for e in (*ERRS, "other")} | ({"?": err_counts["?"]} if err_counts.get("?") else {}),
        "error_commands": err_cmds,
        "menu": menu,
        "tau_b_pairs": dict(sorted(tau_b.items())),
        "readback": rb,
        "period": period,
        "isolated": isolated,
        "fields_3c": fields_3c,
        "fields_3d": fields_3d,
    })
    st["thresholds"] = thresholds(st)
    return st


def _fmt(ms: int) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def _yn(ok: bool) -> str:
    return "MET" if ok else "NOT_MET"


def thresholds(st: dict) -> list[dict]:
    """Таблица порогов 2c только из счётчиков. Признаки -- все, что встретились в журнале."""
    out: list[dict] = []
    fp = st["feature_pairs"]
    for f, c in fp.items():
        out.append({"param": "eps_state/kappa_j/alpha_j", "key": f, "have": f"pairs={c['pairs']} non_pass={c['non_pass']}",
                    "need": f"pairs>={TH_PAIRS} non_pass>={TH_NONPASS}",
                    "status": _yn(c["pairs"] >= TH_PAIRS and c["non_pass"] >= TH_NONPASS)})
    if not fp:
        out.append({"param": "eps_state/kappa_j/alpha_j", "key": "-", "have": "pairs=0", "need": f"pairs>={TH_PAIRS}",
                    "status": "NOT_MET"})
    fa = st["feature_action_cmds"]
    for k, n in fa.items():
        out.append({"param": "T_theta(API)", "key": k, "have": f"commands={n}", "need": f">={TH_FEAT_ACTION}",
                    "status": _yn(n >= TH_FEAT_ACTION)})
    if not fa:
        out.append({"param": "T_theta(API)", "key": "-", "have": "commands=0", "need": f">={TH_FEAT_ACTION}",
                    "status": "NOT_MET"})
    m = st["menu"]
    out.append({"param": "T_theta(menu)", "key": "-",
                "have": f"menu_step={m['steps']} edges_distinct={m['edges_distinct']} lt3(NO_DATA)={m['edges_distinct_lt3']}",
                "need": f"menu_step>={TH_MENU_STEPS}", "status": _yn(m["steps"] >= TH_MENU_STEPS)})
    tb = st["tau_b_pairs"]
    for f, n in tb.items():
        out.append({"param": "tau_b", "key": f, "have": f"pairs={n}", "need": f">={TH_TAU_B}", "status": _yn(n >= TH_TAU_B)})
    if not tb:
        out.append({"param": "tau_b", "key": "-", "have": "pairs=0", "need": f">={TH_TAU_B}", "status": "NOT_MET"})
    rb = st["readback"]["rows"]
    out.append({"param": "pi_0", "key": "-", "have": f"readback={rb}", "need": f">={TH_READBACK} (else MANUAL uniform)",
                "status": _yn(rb >= TH_READBACK)})
    ec = st["error_commands"]
    out.append({"param": "tau_D/Theta_crit/Theta_reset", "key": "-", "have": f"error_commands={ec}",
                "need": f">={TH_ERRORS} (else MANUAL by Operator)", "status": _yn(ec >= TH_ERRORS)})
    return out


def render(st: dict) -> str:
    L: list[str] = ["# sigma_shadow_stats (counters only; no s_exp/s_obs values, no ts, no node codes)"]
    L.append("files: " + ", ".join(f"{k}={v}" for k, v in st["files"].items()))
    L.append(f"lines: total={st['lines_total']} ok={st['lines_ok']} bad={st['lines_bad']}")
    L.append(f"commands: seq={st['commands_seq']} with_outcome={st['commands_with_outcome']}")
    p = st["period"]
    L.append(f"period: first={p['first'] or '-'} last={p['last'] or '-'} hours={p['hours']}")
    L.append("step: " + " ".join(f"{k}={v}" for k, v in st["step"].items()))
    L.append("action x outcome:")
    for a, c in st["action_outcome"].items() or [("-", {})]:
        L.append(f"  {a}: " + (" ".join(f"{o}={n}" for o, n in c.items()) or "0"))
    L.append("feature (s_exp,s_obs) pairs:")
    for f, c in st["feature_pairs"].items() or [("-", {"pairs": 0, "non_pass": 0})]:
        L.append(f"  {f}: pairs={c['pairs']} non_pass={c['non_pass']}")
    L.append("feature|action commands:")
    for k, n in st["feature_action_cmds"].items() or [("-", 0)]:
        L.append(f"  {k}: {n}")
    L.append("err (per command): " + " ".join(f"{k}={v}" for k, v in st["err"].items())
             + f" | error_commands={st['error_commands']}")
    m = st["menu"]
    L.append(f"menu: menu_step={m['steps']} edges={m['edges']} edges_distinct={m['edges_distinct']} "
             f"distinct_ge3={m['edges_distinct_ge3']} distinct_lt3={m['edges_distinct_lt3']}")
    L.append("tau_b pairs (consecutive commands per feature): "
             + (" ".join(f"{f}={n}" for f, n in st["tau_b_pairs"].items()) or "-"))
    L.append(f"readback: rows={st['readback']['rows']} with_s_obs={st['readback']['with_s_obs']}")
    f3 = st["fields_3c"]
    L.append(f"fields 3c (outcome rows): total={f3['outcome_rows']} with_d_cmd={f3['with_d_cmd']} "
             f"with_s_raw={f3['with_s_raw']} (older rows: NO_DATA)")
    f4 = st.get("fields_3d") or {}
    if f4:
        L.append(f"fields 3d (outcome rows with sigma verdict): total={f4['outcome_with_sigma']} "
                 + "sigma[" + " ".join(f"{k}={v}" for k, v in f4["sigma"].items()) + "] "
                 + "sigma_p[" + " ".join(f"{k}={v}" for k, v in f4["sigma_p"].items()) + "] "
                 + f"a_t1={f4['a_t1']} live1={f4['live1']} sigma_rows={f4['sigma_rows']}")
        L.append(f"gate A (VERIFIED after SENT, verified commands with sigma): sent={f4['gateA_sent']} "
                 f"verified={f4['gateA_verified']} ratio={'NO_DATA' if f4['gateA_ratio'] is None else f4['gateA_ratio']}")
    for f, c in (st.get("isolated") or {}).items():
        oc = c["outcome"]
        L.append(f"{_code(f)}: изолирован ({c['reason']}) commands={c['commands']} PASS={oc['PASS']} WARN={oc['WARN']} "
                 f"FAIL={oc['FAIL']} readback={c['readback']} -- not in error_commands/err, eps_state, T_theta, tau_b, "
                 f"pi_0, thresholds")
    L.append("thresholds 2c (15.11 21:23):")
    for t in st["thresholds"]:
        L.append(f"  [{t['status']}] {t['param']} {t['key']}: {t['have']} (need {t['need']})")
    met = sum(1 for t in st["thresholds"] if t["status"] == "MET")
    L.append(f"thresholds: MET={met} NOT_MET={len(st['thresholds']) - met}")
    return "\n".join(L)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Counters of logs/sigma_shadow.jsonl (no values).")
    ap.add_argument("--path", default=str(DEFAULT_PATH))
    ap.add_argument("--json", action="store_true", help="machine-readable counters")
    ap.add_argument("--no-isolate", action="store_true", help="raw counts: do not isolate features (B07d)")
    a = ap.parse_args(argv)
    st = compute(Path(a.path), {} if a.no_isolate else ISOLATE_DEFAULT)
    out = json.dumps(st, ensure_ascii=True, sort_keys=True) if a.json else render(st)
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "").replace("_", "")
    if enc != "utf8" and hasattr(sys.stdout, "reconfigure"):
        try:   # строка изоляции -- кириллица; вывод в канал под Windows (cp125x) не должен падать
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
