"""block_finish.py -- единая точка закрытия блока: строка run_metrics записана и прочитана назад -> только тогда GREEN.

Вход -- машинные строки блока вида `METRIC <ключ>=<значение>` (прочие строки не читаются, текст отчёта не
разбирается). Ключи (все обязательны, каждый ровно один раз):
  task, block, started_at, ended_at      -- коды и время ISO 8601 (как в run_metrics);
  steps, rollbacks, uac, hitl,           -- счётчики, которые блок ведёт сам (целые >= 0): шаги, откаты, факт
  parse_errors, net_errors, flaky           запуска с правами администратора, ожидания ответа человека, ошибки
                                             сухого разбора, сетевые ошибки, повторы упавших тестов;
  signal_wanted                          -- итог, который блок просит: 1 GREEN, 2 RED, 3 ASK, 5 MANUAL;
  report_sha16                           -- sha256[:16] текста отчёта без итоговой строки SIGNAL;
  run_nonce                              -- соль запуска (32 hex, новая на каждый запуск): своя строка на попытку;
  outcome                                -- GREEN / RED / ROLLBACK / STOP (signal 2) / ASK (3) / MANUAL (5);
  fail_stage, fail_code                  -- шаг остановки (N/M или init) и машинный код причины; пусто у GREEN;
  launch_ts                              -- время маячка запуска (первая строка скрипта блока);
  launch_delay_s, uac_wait_s, hitl_wait_s -- секунды: задержка от запроса до маячка, ожидание UAC, ожидание
                                             человека; пусто = не измерялось (NULL), 0 = ожидания не было.
  NOT_STARTED (сигнал 2): обёртка запустилась, UAC истёк / отменён.
Пустое значение (`METRIC hitl=`) -- NULL: допустимо для счётчиков, fail_stage, fail_code и только при исходе не GREEN
(блок упал до сбора счётчиков -- пишется то, что известно). Каждый запуск с любым исходом пишет свою строку;
attempt (номер попытки задача+блок за сутки) считает quarantine_state.record_run чтением таблицы.
Правило (finish): итоговую строку `SIGNAL: 1` печатает только эта функция и только если строка run_metrics
(source = block) записана И прочитана назад новым соединением с теми же полями. Иначе при signal_wanted = 1 ->
`SIGNAL: 3 - ASK; metrics_not_recorded (<причина>)`. RED / ASK / MANUAL не меняются (запись для них тоже
обязательна, её сбой отмечается в причине, но зелёным итог не становится). Неразборчивый signal_wanted -> RED.
Повтор с теми же строками -- та же строка run_metrics (run_id детерминирован), итог тот же; та же запись с другими
полями -> расхождение -> не GREEN. Только стандартная библиотека и quarantine_state."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Iterable, Optional

import quarantine_state as qs

KEYS = ("task", "block", "started_at", "ended_at") + qs.RUN_COUNTS + ("signal_wanted", "report_sha16", "run_nonce",
                                                                       "outcome", "fail_stage", "fail_code",
                                                                       "launch_ts") + qs.WAITS
NULLABLE = qs.RUN_COUNTS + ("fail_stage", "fail_code", "launch_ts") + qs.WAITS
_SEC_RE = re.compile(r"^(0|[1-9][0-9]{0,8})(\.[0-9]{1,3})?$")
NAMES = {1: "GREEN", 2: "RED", 3: "ASK", 5: "MANUAL"}
_LINE_RE = re.compile(r"^METRIC ([a-z][a-z0-9_]*)=(\S*)$")
_INT_RE = re.compile(r"^(0|[1-9][0-9]{0,8})$")


class MetricError(ValueError):
    pass


def parse_metric_lines(lines: Iterable[str]) -> dict:
    """Строки `METRIC k=v` -> словарь. Неизвестный ключ, повтор ключа, пропуск ключа, нецелый счётчик -> MetricError."""
    got: dict = {}
    for raw in lines:
        ln = str(raw).rstrip("\r\n")
        if not ln.startswith("METRIC "):
            continue
        m = _LINE_RE.match(ln)
        if not m:
            raise MetricError("строка METRIC не вида 'METRIC ключ=значение'")
        k, v = m.group(1), m.group(2)
        if k not in KEYS:
            raise MetricError(f"неизвестный ключ METRIC: {k}")
        if k in got:
            raise MetricError(f"ключ METRIC повторён: {k}")
        got[k] = v
    miss = [k for k in KEYS if k not in got]
    if miss:
        raise MetricError("нет ключей METRIC: " + ",".join(miss))
    for k in NULLABLE:
        if got[k] == "":
            got[k] = None
    for k in qs.RUN_COUNTS + ("signal_wanted",):
        if got[k] is None:
            continue
        if not _INT_RE.match(got[k]):
            raise MetricError(f"{k}: целое >= 0")
        got[k] = int(got[k])
    for k in qs.WAITS:
        if got[k] is None:
            continue
        if not _SEC_RE.match(got[k]):
            raise MetricError(f"{k}: секунды >= 0 (до 3 знаков после точки) или пусто")
        got[k] = float(got[k])
    if not re.match(r"^[0-9a-f]{32}$", got["run_nonce"]):
        raise MetricError("run_nonce: 32 строчных шестнадцатеричных знака")
    return got


def wanted_signal(lines: Iterable[str]) -> Optional[int]:
    """signal_wanted из строк, даже если остальные строки неверны (для итога при ошибке входа); None -- нет/неверен."""
    vals = []
    for raw in lines:
        m = _LINE_RE.match(str(raw).rstrip("\r\n"))
        if m and m.group(1) == "signal_wanted":
            vals.append(m.group(2))
    if len(vals) != 1 or not _INT_RE.match(vals[0]) or int(vals[0]) not in NAMES:
        return None
    return int(vals[0])


def _expected_row(m: dict) -> dict:
    rec = {k: m[k] for k in ("task", "block", "started_at", "ended_at") + qs.RUN_COUNTS}
    rec["signal"] = m["signal_wanted"]
    task, block, st, en, dur, vals, sig = qs.validate_run(rec, allow_null=True)
    outcome, fs, fc = qs.validate_outcome(m["outcome"], sig, m["fail_stage"], m["fail_code"])
    if outcome == "GREEN" and any(vals[k] is None for k in qs.RUN_COUNTS):
        raise MetricError("outcome GREEN: все счётчики -- целые")
    return {"run_id": qs.run_id(task, block, st, m["run_nonce"]), "task": task, "block": block, "started_at": st,
            "ended_at": en, "duration_s": dur, **vals, "signal": sig, "source": "block",
            "report_sha16": m["report_sha16"], "outcome": outcome, "fail_stage": fs, "fail_code": fc,
            "launch_ts": None if m["launch_ts"] is None else qs._norm_time(m["launch_ts"], "launch_ts"),
            **{k: None if m[k] is None else round(m[k], 3) for k in qs.WAITS}}


def finish(lines: Iterable[str], db: Optional[Path] = None, now: Optional[dt.datetime] = None) -> dict:
    """-> {signal, status, recorded, reason, run_id, new, line}. Никогда не бросает: любой сбой -> не GREEN."""
    lines = list(lines)
    want = wanted_signal(lines)
    recorded, reason, rid, new, exp = False, "", None, None, {}
    try:
        m = parse_metric_lines(lines)
        if m["signal_wanted"] not in NAMES:
            raise MetricError("signal_wanted: одно из 1, 2, 3, 5")
        exp = _expected_row(m)
        rid = exp["run_id"]
        res = qs.record_run({**{k: exp[k] for k in ("task", "block", "started_at", "ended_at") + qs.RUN_COUNTS},
                             "signal": exp["signal"]}, db, now=now, source="block", report_sha16=m["report_sha16"],
                            outcome=exp["outcome"], fail_stage=exp["fail_stage"], fail_code=exp["fail_code"],
                            salt=m["run_nonce"], launch_ts=m["launch_ts"], waits={k: m[k] for k in qs.WAITS})
        new = res["new"]
        exp["attempt"] = res["attempt"]
        back = qs.read_run(rid, db)
        if back is None:
            reason = "row_not_read_back"
        else:
            diff = sorted(k for k, v in exp.items() if back.get(k) != v)
            if diff:
                reason = "fields_differ:" + ",".join(diff)
            else:
                recorded = True
    except MetricError as e:
        reason = f"bad_metric_lines: {e}"
    except qs.SchemaError:
        reason = "db_schema_v1_not_migrated"
    except ValueError as e:
        reason = f"bad_values: {e}"
    except Exception as e:                       # база недоступна / заблокирована / диск -- не GREEN
        reason = f"db_error: {type(e).__name__}"
    if want is None:
        sig = 2
        why = "signal_wanted missing or invalid"
    elif want == 1 and not recorded:
        sig = 3
        why = "metrics_not_recorded"
    else:
        sig = want
        why = "metrics_recorded" if recorded else "metrics_not_recorded"
    tail = f"run_id={rid} attempt={exp.get('attempt')}" if recorded else f"({reason})"
    line = f"SIGNAL: {sig} - {NAMES[sig]}; {why} {tail}".rstrip()
    return {"signal": sig, "status": NAMES[sig], "recorded": recorded, "reason": reason, "run_id": rid, "new": new,
            "attempt": exp.get("attempt") if recorded else None, "outcome": exp.get("outcome"), "line": line}
