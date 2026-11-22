"""layer_events.py -- B31 «счётчик слоёв» L0..L7: одна строка на событие слоя, только числа и машинные коды.

Строка: ts (ISO, секунды), layer (L0..L7), kind (input | fail | catch), code (из закрытого списка CODES), block_sha12
(версия кода, 12 знаков HEAD). Свободного текста нет по построению: поле code принимает только код из CODES для пары
(layer, kind); иное -> отклонение и собственная строка L7 fail SCHEMA_REJECT (сам отвергнутый код не пишется).

Слои (решение 12.11, PLAN_global_audit): L0 ввод · L1 правила · L2 модель · L3 защиты · L4 исполнитель ·
L5 подтверждение · L6 память · L7 журнал/зал.

Хранилище -- отдельная база data/state/layer_events.db (не таблица в quarantine_state.db):
  * бот пишет часто, из разных потоков; quarantine_state.db копируют и сверяют по sha блоки (резервная копия перед
    миграцией: sha копии = sha базы) и ночная задача -- живой писатель в том же файле ломал бы эту сверку;
  * соединение открывается на одну запись и сразу закрывается, journal_mode=DELETE: файл не держится службой --
    копию можно снять между записями, не останавливая бота;
  * резервная копия = копия одного файла; ночной счёт (tools/layer_digest.py) пишет итоги в quarantine_state.db.
Телеметрия fail-open: ошибка записи не бросает исключение в бота -- только счётчик потерь (lost), который при следующей
удачной записи ложится в таблицу lost и виден в ночном счёте. Действий этот модуль не разрешает и не запрещает.

Под pytest (и в дочерних процессах тестов: AI_BASE_DIR из conftest) без явного OKO_LAYER_EVENTS_DB запись идёт в память
процесса -- боевой файл тестами не пишется.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional

LAYERS = ("L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7")
KINDS = ("input", "fail", "catch")

# Закрытый список кодов: (слой, вид) -> коды. Новый код -- только правкой этой таблицы (и теста).
CODES: dict = {
    ("L0", "input"): frozenset({"TEXT", "VOICE"}),
    ("L0", "fail"): frozenset({"VOICE_DL_FAIL", "STT_TIMEOUT", "STT_FAIL"}),
    ("L1", "input"): frozenset({"ROUTE", "ROUTE_MISS"}),   # ROUTE_MISS (ушло в модель) -- вне e_k (Q1 21.11 20:08)
    ("L1", "fail"): frozenset({"ROUTE_ERROR"}),
    ("L2", "input"): frozenset({"MODEL", "JUDGE"}),
    ("L2", "catch"): frozenset({"CJK_GUARD", "ROLE", "HISTORY_SKIPPED", "D_GUARD"}),
    ("L2", "fail"): frozenset({"MODEL_ERROR", "JUDGE_ERROR", "JUDGE_UNPARSEABLE", "JUDGE_TOO_LONG"}),
    ("L3", "input"): frozenset({"INPUT_FILTER", "GOV_CHECK", "SIGMA_CHECK"}),
    ("L3", "catch"): frozenset({"INPUT_BLOCKED", "BREAKER_OPEN", "JUDGE_BLOCK", "SIGMA_HALT"}),
    ("L3", "fail"): frozenset({"GUARD_MISS"}),
    ("L4", "input"): frozenset({"TV_CMD", "ST_POLL"}),
    ("L4", "fail"): frozenset({"TV_TIMEOUT", "TV_ERROR", "TV_NOT_PAIRED", "TV_UNSUPPORTED", "TV_VALUE", "TV_EXC",
                               "ST_TRANSPORT", "ST_AUTH", "ST_RATE"}),
    ("L5", "input"): frozenset({"VERIFY"}),
    ("L5", "fail"): frozenset({"VERIFY_FAIL", "VERIFY_WARN", "VERIFY_BLIND"}),
    ("L6", "input"): frozenset({"BIND", "MEM_WRITE"}),
    ("L6", "catch"): frozenset({"P1_LOOP"}),
    ("L6", "fail"): frozenset({"BIND_FAIL", "MEM_WRITE_FAIL"}),
    ("L7", "input"): frozenset({"JOURNAL_WRITE", "EVENT_WRITE"}),
    ("L7", "fail"): frozenset({"STORE_WRITE_FAIL", "SCHEMA_REJECT"}),
}
CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,23}$")

DDL_EVENTS = """CREATE TABLE IF NOT EXISTS layer_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL CHECK (length(ts) = 19),
    layer TEXT NOT NULL CHECK (layer IN ('L0','L1','L2','L3','L4','L5','L6','L7')),
    kind TEXT NOT NULL CHECK (kind IN ('input','fail','catch')),
    code TEXT NOT NULL CHECK (length(code) BETWEEN 2 AND 24 AND code NOT GLOB '*[^A-Z0-9_]*'),
    block_sha12 TEXT NOT NULL CHECK (length(block_sha12) <= 12 AND block_sha12 NOT GLOB '*[^0-9a-z_-]*'))"""
DDL_INDEX = "CREATE INDEX IF NOT EXISTS layer_events_ts ON layer_events (ts)"
DDL_LOST = """CREATE TABLE IF NOT EXISTS layer_events_lost (
    ts TEXT NOT NULL, n INTEGER NOT NULL CHECK (n > 0))"""

_LOCK = threading.Lock()
_LOST = 0                 # записи, не легшие в базу (ждут следующей удачной записи)
_LOST_TOTAL = 0           # за жизнь процесса
_REJECTED = 0             # отвергнутые схемой за жизнь процесса
_MEMORY: list = []        # приёмник под pytest без OKO_LAYER_EVENTS_DB (и для отладки)
_SHA: Optional[str] = None
MEMORY_MAX = 50000


def default_db() -> Path:
    env = os.getenv("OKO_LAYER_EVENTS_DB", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "state" / "layer_events.db"


def _to_memory() -> bool:
    """под тестами (pytest или дочерний процесс теста: AI_BASE_DIR из conftest) без явной базы -- приёмник памяти."""
    if os.getenv("OKO_LAYER_EVENTS_DB", "").strip():
        return False
    return bool(os.getenv("PYTEST_CURRENT_TEST")) or "oko_pytest_" in os.getenv("AI_BASE_DIR", "")


def valid(layer: object, kind: object, code: object) -> bool:
    """True -- тройка из закрытого списка. Строка с пробелом, кириллицей, длиннее 24 и т.п. -- False."""
    if not isinstance(layer, str) or not isinstance(kind, str) or not isinstance(code, str):
        return False
    if not CODE_RE.match(code):
        return False
    return code in CODES.get((layer, kind), ())


def block_sha12(root: Optional[Path] = None) -> str:
    """12 знаков HEAD репозитория (без запуска git: .git/HEAD -> ref -> sha). Нет -- 'unknown'."""
    env = os.getenv("OKO_BLOCK_SHA", "").strip().lower()
    if env:
        return re.sub(r"[^0-9a-z]", "", env)[:12] or "unknown"
    try:
        g = (root or Path(__file__).resolve().parent.parent) / ".git"
        head = (g / "HEAD").read_text(encoding="ascii").strip()
        if head.startswith("ref: "):
            ref = head[5:].strip()
            p = g / ref
            if p.is_file():
                head = p.read_text(encoding="ascii").strip()
            else:
                head = ""
                pr = g / "packed-refs"
                if pr.is_file():
                    for ln in pr.read_text(encoding="ascii").splitlines():
                        parts = ln.split()
                        if len(parts) == 2 and parts[1] == ref:
                            head = parts[0]
        head = head.lower()
        if re.fullmatch(r"[0-9a-f]{40}", head):
            return head[:12]
    except (OSError, UnicodeDecodeError):
        pass
    return "unknown"


def _sha() -> str:
    global _SHA
    if _SHA is None:
        _SHA = block_sha12()
    return _SHA


def connect(db: Optional[Path] = None, timeout: float = 2.0) -> sqlite3.Connection:
    db = Path(db or default_db())
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db), timeout=timeout)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute(DDL_EVENTS)
    con.execute(DDL_INDEX)
    con.execute(DDL_LOST)
    return con


def _now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _write(rows: list, lost: int) -> None:
    if _to_memory():
        _MEMORY.extend(rows)
        if len(_MEMORY) > MEMORY_MAX:
            del _MEMORY[:len(_MEMORY) - MEMORY_MAX]
        return
    con = connect()
    try:
        con.executemany("INSERT INTO layer_events (ts, layer, kind, code, block_sha12) VALUES (?, ?, ?, ?, ?)", rows)
        if lost:
            con.execute("INSERT INTO layer_events_lost (ts, n) VALUES (?, ?)", (rows[0][0], lost))
        con.commit()
    finally:
        con.close()


def record(layer: str, kind: str, code: str) -> bool:
    """Одна строка события слоя. Никогда не бросает. True -- строка записана; False -- отвергнута или потеряна.

    Неизвестная тройка -> вместо неё строка L7 fail SCHEMA_REJECT (сам код в базу не попадает: это и есть защита от
    текста в поле code). Сбой базы -> счётчик потерь; при следующей удачной записи он ложится в layer_events_lost."""
    global _LOST, _LOST_TOTAL, _REJECTED
    try:
        ts = _now()
        ok = valid(layer, kind, code)
        if not ok:
            _REJECTED += 1
            row = (ts, "L7", "fail", "SCHEMA_REJECT", _sha())
        else:
            row = (ts, layer, kind, code, _sha())
        with _LOCK:
            lost = _LOST
            try:
                _write([row], lost)
                _LOST = 0
            except Exception:  # noqa: BLE001 - телеметрия fail-open: бот работает дальше
                _LOST += 1
                _LOST_TOTAL += 1
                return False
        return ok
    except Exception:  # noqa: BLE001
        return False


def stats() -> dict:
    """счётчики процесса (только числа)."""
    return {"lost_pending": _LOST, "lost_total": _LOST_TOTAL, "rejected": _REJECTED, "memory_rows": len(_MEMORY)}


def memory_rows() -> list:
    """строки приёмника памяти (под pytest): [(ts, layer, kind, code, sha12)]."""
    return list(_MEMORY)


def reset_memory() -> None:
    global _LOST, _LOST_TOTAL, _REJECTED
    with _LOCK:
        _MEMORY.clear()
        _LOST = _LOST_TOTAL = _REJECTED = 0


# --------------------------------------------------------------------------------------------------------------------
# Ночной счёт (tools/layer_digest.py, задача 03:27 -- Q5 21.11 20:08): e_k = fail / input, c_k = catch / (catch + fail) по слою за сутки и за 7 суток.
# Деление на ноль -> None (NO_DATA), не 0. input < MIN_N -> значение пишется, решение = NO_DATA (порог реестра).
# Направление: рост e_k = хуже (up_bad). input = 0 за сутки при живом боте -> STERILE.
MIN_N = 70
DIRECTION = "up_bad"
# Q1 (Оператор 21.11 20:08): «вид вне e_k» -- коды, которые считаются отдельно и не входят в input слоя.
OUT_OF_RATE = {("L1", "input", "ROUTE_MISS")}
# Q2/Q3: раздельные доли внутри слоя. Слой -> {доля: (коды input, коды fail)}; None = все fail слоя.
# Основная e_k слоя = первая доля (L4: ТВ; L5: жёсткая VERIFY_FAIL / VERIFY).
PARTS = {
    "L4": {"tv": ({"TV_CMD"}, {"TV_TIMEOUT", "TV_ERROR", "TV_NOT_PAIRED", "TV_UNSUPPORTED", "TV_VALUE", "TV_EXC"}),
           "st": ({"ST_POLL"}, {"ST_TRANSPORT", "ST_AUTH", "ST_RATE"})},
    "L5": {"hard": ({"VERIFY"}, {"VERIFY_FAIL"}),
           "all": ({"VERIFY"}, None)},
}


def _ratio(a: int, b: int) -> Optional[float]:
    return None if b <= 0 else round(a / b, 6)


def counts(db: Optional[Path], since: str, until: str) -> dict:
    """{layer: {"input": n, "fail": n, "catch": n, "codes": {code: n}}} за [since, until); нет базы -> пусто."""
    out = {L: {"input": 0, "fail": 0, "catch": 0, "codes": {}} for L in LAYERS}
    p = Path(db or default_db())
    if not p.is_file():
        return out
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='layer_events'").fetchone()
        if not has:
            return out
        for layer, kind, code, n in con.execute(
                "SELECT layer, kind, code, COUNT(*) FROM layer_events WHERE ts >= ? AND ts < ? "
                "GROUP BY layer, kind, code", (since, until)):
            if layer in out and kind in KINDS:
                if (layer, kind, code) not in OUT_OF_RATE:
                    out[layer][kind] += n
                out[layer]["codes"][f"{kind}:{code}"] = n
    finally:
        con.close()
    return out


def lost_count(db: Optional[Path], since: str, until: str) -> int:
    p = Path(db or default_db())
    if not p.is_file():
        return 0
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='layer_events_lost'").fetchone():
            return 0
        return int(con.execute("SELECT COALESCE(SUM(n), 0) FROM layer_events_lost WHERE ts >= ? AND ts < ?",
                               (since, until)).fetchone()[0])
    finally:
        con.close()


def _part(codes: dict, ins: set, fails) -> dict:
    i = sum(n for key, n in codes.items() if key.startswith("input:") and key[6:] in ins)
    f = sum(n for key, n in codes.items() if key.startswith("fail:") and (fails is None or key[5:] in fails))
    e = _ratio(f, i)
    return {"input": i, "fail": f, "e_k": e, "decision": "NO_DATA" if (i < MIN_N or e is None) else "OK"}


def layer_row(c: dict, layer: str = "") -> dict:
    i, f, k = c["input"], c["fail"], c["catch"]
    codes = dict(sorted(c["codes"].items()))
    parts = {name: _part(codes, ins, fl) for name, (ins, fl) in PARTS.get(layer, {}).items()}
    if parts:                                   # основная доля слоя = первая (L4 ТВ, L5 жёсткая)
        first = next(iter(parts.values()))
        e, dec = first["e_k"], first["decision"]
    else:
        e = _ratio(f, i)
        dec = "NO_DATA" if (i < MIN_N or e is None) else "OK"
    out_rate = {key: n for key, n in codes.items() if (layer,) + tuple(key.split(":", 1)) in OUT_OF_RATE}
    return {"input": i, "fail": f, "catch": k, "e_k": e, "c_k": _ratio(k, k + f), "decision": dec,
            "parts": parts, "out_of_rate": out_rate, "codes": codes}


def digest(db: Optional[Path] = None, now: Optional[dt.datetime] = None, bot_alive: Optional[bool] = None) -> dict:
    """итог за сутки (вчера 00:00 .. сегодня 00:00 относительно now) и за 7 суток по каждому слою -- только числа."""
    now = now or dt.datetime.now()
    end = dt.datetime.combine(now.date(), dt.time(0, 0))
    d1 = (end - dt.timedelta(days=1)).isoformat()
    d7 = (end - dt.timedelta(days=7)).isoformat()
    e = end.isoformat()
    c1, c7 = counts(db, d1, e), counts(db, d7, e)
    day = {L: layer_row(c1[L], L) for L in LAYERS}
    week = {L: layer_row(c7[L], L) for L in LAYERS}
    any_day = sum(day[L]["input"] + day[L]["fail"] + day[L]["catch"] for L in LAYERS)
    alive = bool(any_day) if bot_alive is None else bool(bot_alive)
    sterile = [L for L in LAYERS if alive and day[L]["input"] == 0]
    return {"day": (end - dt.timedelta(days=1)).date().isoformat(), "window_1d": [d1, e], "window_7d": [d7, e],
            "min_n": MIN_N, "direction": DIRECTION, "bot_alive": alive, "sterile": sterile,
            "lost_1d": lost_count(db, d1, e), "lost_7d": lost_count(db, d7, e), "layers_1d": day, "layers_7d": week}


DDL_DIGEST = """CREATE TABLE IF NOT EXISTS layer_digest (
    day TEXT NOT NULL, window TEXT NOT NULL CHECK (window IN ('1d','7d')), layer TEXT NOT NULL,
    input INTEGER NOT NULL, fail INTEGER NOT NULL, catch INTEGER NOT NULL, e_k REAL, c_k REAL,
    decision TEXT NOT NULL, sterile INTEGER NOT NULL, lost INTEGER NOT NULL, direction TEXT NOT NULL,
    parts_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (day, window, layer))"""


def store_digest(d: dict, qs_db: Path, now: Optional[dt.datetime] = None) -> int:
    """итог -> таблица layer_digest в quarantine_state.db (повтор за те же сутки заменяет строки). Строк записано."""
    now = now or dt.datetime.now()
    qs_db = Path(qs_db)
    qs_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(qs_db), timeout=30.0)
    try:
        con.execute(DDL_DIGEST)
        n = 0
        for win, key, lost in (("1d", "layers_1d", d["lost_1d"]), ("7d", "layers_7d", d["lost_7d"])):
            for L in LAYERS:
                r = d[key][L]
                pj = json.dumps({"parts": r.get("parts", {}), "out_of_rate": r.get("out_of_rate", {})},
                                ensure_ascii=True, sort_keys=True)
                con.execute("INSERT OR REPLACE INTO layer_digest (day, window, layer, input, fail, catch, e_k, c_k, "
                            "decision, sterile, lost, direction, parts_json, created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (d["day"], win, L, r["input"], r["fail"], r["catch"], r["e_k"], r["c_k"], r["decision"],
                             int(win == "1d" and L in d["sterile"]), int(lost), d["direction"], pj,
                             now.replace(microsecond=0).isoformat()))
                n += 1
        con.commit()
        return n
    finally:
        con.close()


def read_digest(qs_db: Path, end_day: str, days: int = 7) -> dict:
    """для weekly: {day: {layer: {e_k, decision, input}}} окна 1d за days суток до end_day; нет таблицы -> {}."""
    p = Path(qs_db)
    if not p.is_file():
        return {}
    end = dt.date.fromisoformat(end_day)
    start = (end - dt.timedelta(days=days - 1)).isoformat()
    con = sqlite3.connect(str(p), timeout=30.0)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='layer_digest'").fetchone():
            return {}
        out: dict = {}
        for day, layer, i, e_k, dec, st, lost, pj in con.execute(
                "SELECT day, layer, input, e_k, decision, sterile, lost, parts_json FROM layer_digest WHERE "
                "window = '1d' AND day >= ? AND day <= ? ORDER BY day, layer", (start, end.isoformat())):
            row = {"input": i, "e_k": e_k, "decision": dec, "sterile": bool(st), "lost": lost}
            parts = (json.loads(pj) if pj else {}).get("parts") or {}
            if parts:
                row["parts"] = {k: v.get("e_k") for k, v in parts.items()}
            out.setdefault(day, {})[layer] = row
        return out
    finally:
        con.close()


def _fmt(v: Optional[float]) -> str:
    return "NO_DATA" if v is None else f"{v * 100:.1f} %"


def register_table(d: dict, window: str = "7d") -> str:
    """таблица как в REGISTER_L0_L7 (слой / входы / сбои / e_k / достоверность) -- только числа из базы."""
    key = "layers_7d" if window == "7d" else "layers_1d"
    names = {"L0": "ввод", "L1": "правила", "L2": "модель", "L3": "защиты", "L4": "исполнитель",
             "L5": "подтверждение", "L6": "память", "L7": "журнал/зал"}
    lines = [f"# РЕЕСТР L0–L7 (автомат) · окно {window} {d['window_' + window][0]} .. {d['window_' + window][1]} · "
             f"порог решения input >= {d['min_n']} · рост e_k = хуже",
             "| слой | входы | сбои | перехваты | e_k | c_k | достоверность |", "|---|---|---|---|---|---|---|"]
    for L in LAYERS:
        r = d[key][L]
        dec = "EXEC" if r["decision"] == "OK" else "NO_DATA"
        if window == "1d" and L in d["sterile"]:
            dec = "STERILE"
        lines.append(f"| {L} {names[L]} | {r['input']} | {r['fail']} | {r['catch']} | {_fmt(r['e_k'])} | "
                     f"{_fmt(r['c_k'])} | {dec} |")
    for L in LAYERS:
        for name, p in d[key][L].get("parts", {}).items():
            lines.append(f"| {L} · {name} | {p['input']} | {p['fail']} | — | {_fmt(p['e_k'])} | — | "
                         f"{'EXEC' if p['decision'] == 'OK' else 'NO_DATA'} |")
        for code, n in d[key][L].get("out_of_rate", {}).items():
            lines.append(f"| {L} · {code.split(':', 1)[1]} (вне e_k) | {n} | — | — | — | — | EXEC |")
    lines.append(f"потери телеметрии: {d['lost_' + window]} · бот жив: {d['bot_alive']} · стерильно (1d): "
                 f"{','.join(d['sterile']) or '-'}")
    return "\n".join(lines) + "\n"
