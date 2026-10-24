"""sleep_digest.py — M7.1 сводка сна + M7.2 индекс дрейфа (только наблюдение).

Детерминированно, без LLM, без сети, без записи в данные знаний/памяти:
лог OkoBot (формат logger.LOG_FORMAT, файл = NSSM AppStderr) читается
инкрементально с курсора в data\\ops.db -> одна строка sleep_digest.

Приватность: в сводку не попадает текст сообщений. Хранятся только счётчики
уровней, служебные теги в НАЧАЛЕ сообщения ([TAG] ...), коды [ERR: X] и
числа из строк [METRICS] (M6).

Дрейф: drift_score = среднее |z| по ce, ad, rl, cjk, log(len+1) окна
(>= DRIFT_WINDOW_MIN ответов) против замороженной базы (первые
BASELINE_DAYS дней и >= BASELINE_MIN_SAMPLES ответов). Порогов-решений нет.

Ручной запуск (до подключения к JobQueue):
    python src\\sleep_digest.py --log <путь AppStderr> --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ops_db import Cursor, OpsDB

log = logging.getLogger(__name__)

HEAD_LEN = 256
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
TOP_TAGS = 50
TOP_ERR = 30

DRIFT_KEYS = ("ce", "ad", "rl", "cjk", "len_log")
STD_FLOOR = {"ce": 0.05, "ad": 0.05, "rl": 0.05, "cjk": 0.02, "len_log": 0.15}
Z_CAP = 10.0
DRIFT_WINDOW_MIN = 30
DRIFT_LOOKBACK_S = 7 * 86400
BASELINE_DAYS = 7
BASELINE_MIN_SAMPLES = 50
AD_LOW = 0.8

_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) \[([A-Z]+)\] ([^\s:]+): ?(.*)$")
_TAG_RE = re.compile(r"^\[([A-Z][A-Z0-9_]{1,39})\]")
_ERR_RE = re.compile(r"\[ERR:\s*([A-Z][A-Z0-9_]{1,39})\]")
_KV_RE = re.compile(r"([a-z_]+)=(\S+)")
_INTENT_RE = re.compile(r"^([a-z_]+)\(([-0-9.]+)\)$")
_METRIC_NUM = ("ce", "ad", "rl", "cyr", "lat", "cjk", "len")


# --- чтение лога ---------------------------------------------------------------
@dataclass(frozen=True)
class ReadResult:
    lines: List[str]
    cursor: Cursor
    rotated: bool
    decode_fallback: int
    truncated: bool  # упёрлись в max_bytes, хвост дочитается следующим прогоном


def _head_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _decode(raw: bytes) -> Tuple[str, bool]:
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace"), True


def read_new_lines(path: Path, cursor: Optional[Cursor],
                   max_bytes: int = DEFAULT_MAX_BYTES) -> ReadResult:
    """Читает только завершённые строки после курсора.

    Ротация/усечение: файл короче offset или начало файла не совпадает с
    запомненным -> чтение с 0, rotated=True (хвост старого файла не читается).
    Незавершённая последняя строка не потребляется.
    """
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(HEAD_LEN)
        f.seek(0, os.SEEK_END)
        size = f.tell()
        offset, rotated = 0, False
        if cursor is not None:
            same_head = (cursor.head_len <= len(head)
                         and _head_hash(head[:cursor.head_len]) == cursor.head_hash)
            if size >= cursor.offset and same_head:
                offset = cursor.offset
            else:
                rotated = True
        f.seek(offset)
        chunk = f.read(max_bytes)

    nl = chunk.rfind(b"\n")
    if nl >= 0:
        consumed = chunk[:nl + 1]
    elif len(chunk) >= max_bytes:
        consumed = chunk  # одна строка длиннее лимита: не застревать
    else:
        consumed = b""
    truncated = len(chunk) >= max_bytes and offset + len(chunk) < size

    lines, fallback = [], 0
    for raw in consumed.split(b"\n"):
        if not raw:
            continue
        text, fb = _decode(raw.rstrip(b"\r"))
        fallback += fb
        lines.append(text)
    new_cursor = Cursor(offset + len(consumed), _head_hash(head), len(head))
    return ReadResult(lines, new_cursor, rotated, fallback, truncated)


# --- разбор ------------------------------------------------------------------
def parse_line(line: str) -> Optional[Tuple[float, str, str, str]]:
    """-> (epoch, level, logger, message) или None (продолжение traceback и т.п.)."""
    m = _LINE_RE.match(line)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return time.mktime(dt.timetuple()) + int(m.group(2)) / 1000.0, m.group(3), m.group(4), m.group(5)


def parse_metrics(message: str) -> Optional[dict]:
    """Строка M6: '[METRICS] point=B ce=0.00 ad=1.00 rl=0.00 intent=inform(1.00) cyr=.. len=336'."""
    if not message.startswith("[METRICS]"):
        return None
    kv = dict(_KV_RE.findall(message))
    out: dict = {"point": kv.get("point", "?")}
    try:
        for k in _METRIC_NUM:
            out[k] = float(kv[k])
    except (KeyError, ValueError):
        return None
    im = _INTENT_RE.match(kv.get("intent", ""))
    out["intent"] = im.group(1) if im else "?"
    return out


def _stat_add(st: dict, x: float) -> None:
    st["n"] += 1
    st["sum"] += x
    st["sumsq"] += x * x
    st["min"] = x if st["min"] is None else min(st["min"], x)
    st["max"] = x if st["max"] is None else max(st["max"], x)


def _empty_stat() -> dict:
    return {"n": 0, "sum": 0.0, "sumsq": 0.0, "min": None, "max": None}


def build_digest(lines: Iterable[str]) -> dict:
    levels: Counter = Counter()
    tags: Counter = Counter()
    errs: Counter = Counter()
    points: Counter = Counter()
    intents: Counter = Counter()
    stats = {k: _empty_stat() for k in _METRIC_NUM + ("len_log",)}
    share = {"cjk_pos": 0, "ad_low": 0, "rl_pos": 0}
    total = unparsed = bad_metrics = 0
    t_first = t_last = None

    for line in lines:
        total += 1
        p = parse_line(line)
        if p is None:
            unparsed += 1
            continue
        ts, level, _name, msg = p
        t_first = ts if t_first is None else min(t_first, ts)
        t_last = ts if t_last is None else max(t_last, ts)
        levels[level] += 1
        tm = _TAG_RE.match(msg)
        if tm:
            tags[tm.group(1)] += 1
        for code in _ERR_RE.findall(msg):
            errs[code] += 1
        if tm and tm.group(1) == "METRICS":
            mt = parse_metrics(msg)
            if mt is None:
                bad_metrics += 1
                continue
            points[mt["point"]] += 1
            intents[mt["intent"]] += 1
            for k in _METRIC_NUM:
                _stat_add(stats[k], mt[k])
            _stat_add(stats["len_log"], math.log1p(max(mt["len"], 0.0)))
            share["cjk_pos"] += mt["cjk"] > 0
            share["ad_low"] += mt["ad"] < AD_LOW
            share["rl_pos"] += mt["rl"] > 0

    return {
        "v": 1,
        "window_start": t_first,
        "window_end": t_last,
        "lines_total": total,
        "lines_unparsed": unparsed,
        "levels": dict(levels),
        "tags": dict(tags.most_common(TOP_TAGS)),
        "err_codes": dict(errs.most_common(TOP_ERR)),
        "metrics": {"n": stats["ce"]["n"], "bad": bad_metrics, "points": dict(points),
                    "intent": dict(intents), "share": share, "stats": stats},
    }


# --- дрейф -------------------------------------------------------------------
def merge_stats(stat_list: Iterable[dict]) -> Dict[str, dict]:
    out = {k: _empty_stat() for k in DRIFT_KEYS}
    for stats in stat_list:
        for k in DRIFT_KEYS:
            s = stats.get(k)
            if not s or not s["n"]:
                continue
            o = out[k]
            o["n"] += s["n"]
            o["sum"] += s["sum"]
            o["sumsq"] += s["sumsq"]
            o["min"] = s["min"] if o["min"] is None else min(o["min"], s["min"])
            o["max"] = s["max"] if o["max"] is None else max(o["max"], s["max"])
    return out


def _mean_std(s: dict) -> Tuple[float, float]:
    n = s["n"]
    mean = s["sum"] / n
    var = max(s["sumsq"] / n - mean * mean, 0.0)
    return mean, math.sqrt(var)


def drift_score(window: Dict[str, dict], baseline: Dict[str, dict]) -> Tuple[float, Dict[str, float]]:
    comps: Dict[str, float] = {}
    for k in DRIFT_KEYS:
        if not window[k]["n"] or not baseline.get(k, {}).get("n"):
            continue
        wm, _ = _mean_std(window[k])
        bm, bs = _mean_std(baseline[k])
        comps[k] = round(min(abs(wm - bm) / max(bs, STD_FLOOR[k]), Z_CAP), 4)
    score = round(sum(comps.values()) / len(comps), 4) if comps else 0.0
    return score, comps


def evaluate_drift(ops: Optional[OpsDB], digest: dict, allow_freeze: bool) -> Tuple[Optional[float], str, dict]:
    """-> (score|None, status, детали). status: no_metrics | baseline_pending | insufficient | ok."""
    cur_stats = digest["metrics"]["stats"]
    n_cur = cur_stats["ce"]["n"]
    w_end = digest["window_end"]
    previous = ops.recent_digests(200) if ops else []
    baseline = ops.get_baseline() if ops else None

    if baseline is None:
        first = min([d.window_start for d in previous if d.window_start is not None]
                    + ([digest["window_start"]] if digest["window_start"] is not None else []),
                    default=None)
        all_stats = [d.payload["metrics"]["stats"] for d in previous] + [cur_stats]
        merged = merge_stats(all_stats)
        samples = merged["ce"]["n"]
        span_days = ((w_end - first) / 86400.0) if (first is not None and w_end is not None) else 0.0
        details = {"baseline_samples": samples, "baseline_span_days": round(span_days, 2)}
        if samples >= BASELINE_MIN_SAMPLES and span_days >= BASELINE_DAYS and allow_freeze and ops:
            if ops.freeze_baseline(first, w_end, samples, merged):
                log.info("[DRIFT_BASELINE] frozen samples=%d span_days=%.1f", samples, span_days)
            baseline = ops.get_baseline()
        else:
            return None, ("no_metrics" if n_cur == 0 else "baseline_pending"), details

    if n_cur == 0:
        return None, "no_metrics", {}
    window_parts, n, used = [cur_stats], n_cur, 1
    for d in previous:  # от новых к старым
        if n >= DRIFT_WINDOW_MIN:
            break
        if d.window_end is None or (w_end is not None and w_end - d.window_end > DRIFT_LOOKBACK_S):
            break
        window_parts.append(d.payload["metrics"]["stats"])
        n += d.payload["metrics"]["stats"]["ce"]["n"]
        used += 1
    details = {"window_samples": n, "window_digests": used}
    if n < DRIFT_WINDOW_MIN:
        return None, "insufficient", details
    score, comps = drift_score(merge_stats(window_parts), baseline["stats"])
    details["components"] = comps
    return score, "ok", details


# --- прогон -------------------------------------------------------------------
def run_once(log_path: Path, ops_path: Path, dry_run: bool = False,
             max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Один цикл сна (M7.1+M7.2). dry_run: ничего не пишет; ops.db не создаётся."""
    log_path, ops_path = Path(log_path), Path(ops_path)
    source = str(log_path.resolve()).lower()
    ops = OpsDB(ops_path) if (not dry_run or ops_path.exists()) else None
    cursor = ops.get_cursor(source) if ops else None
    rr = read_new_lines(log_path, cursor, max_bytes)
    digest = build_digest(rr.lines)
    digest.update(rotated=rr.rotated, decode_fallback=rr.decode_fallback, truncated=rr.truncated)

    if not rr.lines:
        if ops and not dry_run and (rr.rotated or cursor is None):
            ops.save_cursor(source, rr.cursor)
        return {"status": "no_new_lines", "rotated": rr.rotated, "written": False}

    score, dstatus, details = evaluate_drift(ops, digest, allow_freeze=not dry_run)
    digest["drift"] = details
    answers = digest["metrics"]["n"]
    row_id = None
    if not dry_run:
        row_id = ops.commit_digest(source, rr.cursor, digest["window_start"], digest["window_end"],
                                   digest["lines_total"], answers, digest, score, dstatus)
    lv = digest["levels"]
    log.info("[SLEEP_DIGEST] lines=%d answers=%d warn=%d error=%d drift=%s status=%s rotated=%s%s",
             digest["lines_total"], answers, lv.get("WARNING", 0),
             lv.get("ERROR", 0) + lv.get("CRITICAL", 0),
             "-" if score is None else f"{score:.2f}", dstatus, rr.rotated,
             " dry_run" if dry_run else "")
    return {"status": "dry_run" if dry_run else "written", "id": row_id, "drift_score": score,
            "drift_status": dstatus, "digest": digest, "written": not dry_run}


def should_sleep(now: float, last_activity: float, last_sleep: Optional[float], gate_state: object,
                 idle_s: float, min_interval_s: float) -> Tuple[bool, str]:
    """Решение «пора спать» (чистая функция, время — wall clock, сек).
    -> (да/нет, причина): gate | busy | recent | ok.
    gate_state: строка/Enum TransportGate; спим только в CLOSED (HALF_OPEN/OPEN/неизвестно — нет)."""
    if "CLOSED" not in str(gate_state).upper():
        return False, "gate"
    if now - last_activity < idle_s:
        return False, "busy"
    if last_sleep is not None and now - last_sleep < min_interval_s:
        return False, "recent"
    return True, "ok"


def last_sleep_ts(ops_path: Path) -> Optional[float]:
    """Время последней сводки из ops.db (переживает рестарт бота). Нет БД — None, БД не создаётся."""
    ops_path = Path(ops_path)
    if not ops_path.exists():
        return None
    d = OpsDB(ops_path).last_digest()
    return d.created_at if d else None


def status_line(ops_path: Path) -> str:
    """Строка для /status. Только чтение; при отсутствии ops.db — 'нет данных'."""
    ops_path = Path(ops_path)
    if not ops_path.exists():
        return "SLEEP: нет данных"
    ops = OpsDB(ops_path)
    d = ops.last_digest()
    if d is None:
        return "SLEEP: нет данных"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.created_at))
    drift = "-" if d.drift_score is None else f"{d.drift_score:.2f}"
    extra = ""
    if d.drift_status == "baseline_pending":
        dd = d.payload.get("drift", {})
        extra = (f" база {dd.get('baseline_samples', 0)}/{BASELINE_MIN_SAMPLES} отв., "
                 f"{dd.get('baseline_span_days', 0)}/{BASELINE_DAYS} дн.")
    return f"SLEEP: {when} ответов={d.answers} drift={drift} ({d.drift_status}){extra}"


def _default_ops_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "ops.db"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="M7.1/M7.2: сводка сна и индекс дрейфа (наблюдение)")
    ap.add_argument("--log", default=os.environ.get("OKO_LOG_FILE", ""))
    ap.add_argument("--ops", default=str(_default_ops_path()))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-mb", type=float, default=DEFAULT_MAX_BYTES / 1048576)
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    if a.status:
        print(status_line(Path(a.ops)))
        return 0
    if not a.log or not Path(a.log).is_file():
        print(f"LOG NOT FOUND: {a.log!r} (укажи --log или OKO_LOG_FILE)", file=sys.stderr)
        return 2
    from logger import setup_logging  # точка входа CLI: единый формат проекта
    setup_logging()
    res = run_once(Path(a.log), Path(a.ops), dry_run=a.dry_run, max_bytes=int(a.max_mb * 1048576))
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
