"""consent.py -- машинный журнал решений Оператора (согласия [1] / отказы [0]); блок проверяет запись, не текст чата.

Хранилище -- отдельная SQLite-база (по умолчанию data/state/consent.db, переменная OKO_CONSENT_DB), таблица consent.
Почему отдельная база, а не таблица в quarantine_state.db: журнал согласий -- разрешение на действие, а не метрика;
у него другой жизненный цикл (только дописывание, без миграций схемы метрик и без недельных слепков), другая копия
и права доступа; сбой или перенос базы метрик не должен открывать или закрывать действия блоков.

Строка: entry_id (uuid5 канонической записи), ts (время ответа Оператора, ISO; ts_exact = 0 -- время из сводки
неточное), source (telegram | chat | window), question_id (машинный id вопроса), answer (1 | 0),
question_sha16 (sha256[:16] формулировки вопроса -- сам текст не хранится), question_code (короткий машинный код),
task (связь с блоком, B30), origin (live | retro_summary), recorded_at, prev_sha16 / entry_sha16 -- цепочка
(sha16 канонической записи + предыдущего звена): правка или удаление строки задним числом видны в verify().
Только дописывание: функций изменения и удаления нет; повтор той же записи -- та же строка (new=False).

require(task, code): последняя (по ts, затем по порядку записи) запись с этим task + question_code: answer 1 -> GO;
answer 0 -> ASK (отказ); записи нет -> ASK; цепочка нарушена или база недоступна -> ASK. Блок без GO -- не действует.
Только стандартная библиотека; ничего не делает при импорте."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable, Optional

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "oko:consent")
SOURCES = ("telegram", "chat", "window")
ORIGINS = ("live", "retro_summary")
GENESIS = "0" * 16
_QID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_TASK_RE = re.compile(r"^B\d{2}[a-z]?$")
_SHA16_RE = re.compile(r"^[0-9a-f]{16}$")
COLS = ("entry_id", "seq", "ts", "ts_exact", "source", "question_id", "answer", "question_sha16", "question_code", "task",
        "origin", "recorded_at", "prev_sha16", "entry_sha16")
DDL = """CREATE TABLE IF NOT EXISTS consent (
        entry_id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, ts TEXT NOT NULL, ts_exact INTEGER NOT NULL,
        source TEXT NOT NULL, question_id TEXT NOT NULL, answer INTEGER NOT NULL, question_sha16 TEXT NOT NULL,
        question_code TEXT NOT NULL, task TEXT, origin TEXT NOT NULL, recorded_at TEXT NOT NULL,
        prev_sha16 TEXT NOT NULL, entry_sha16 TEXT NOT NULL)"""


class ConsentError(ValueError):
    pass


def default_db() -> Path:
    env = os.getenv("OKO_CONSENT_DB", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "state" / "consent.db"


def canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def question_sha16(text: str) -> str:
    """sha16 формулировки вопроса (UTF-8, пробелы по краям сняты); текст в журнал не пишется."""
    return sha16(str(text).strip().encode("utf-8"))


def _norm_ts(v) -> str:
    try:
        t = dt.datetime.fromisoformat(str(v))
    except ValueError as e:
        raise ConsentError("ts: время ISO 8601") from e
    return t.replace(tzinfo=None, microsecond=0).isoformat()


def validate(e: dict) -> dict:
    """Запись -> нормализованный словарь (поля без seq / recorded_at / цепочки); иначе ConsentError."""
    if not isinstance(e, dict):
        raise ConsentError("запись -- объект")
    ts = _norm_ts(e.get("ts"))
    exact = e.get("ts_exact", True)
    if not isinstance(exact, bool):
        raise ConsentError("ts_exact: true | false")
    src = e.get("source")
    if src not in SOURCES:
        raise ConsentError(f"source: одно из {SOURCES}")
    qid = str(e.get("question_id") or "")
    if not _QID_RE.match(qid):
        raise ConsentError("question_id: [a-z0-9][a-z0-9_.-] до 64")
    ans = e.get("answer")
    if isinstance(ans, bool) or ans not in (0, 1):
        raise ConsentError("answer: 1 | 0")
    qs16 = e.get("question_sha16")
    if qs16 is None and e.get("question_text") is not None:
        qs16 = question_sha16(e["question_text"])
    if not isinstance(qs16, str) or not _SHA16_RE.match(qs16):
        raise ConsentError("question_sha16: 16 строчных hex (или question_text -- хэшируется, не хранится)")
    code = str(e.get("question_code") or "")
    if not _CODE_RE.match(code):
        raise ConsentError("question_code: [a-z][a-z0-9_] до 48")
    task = e.get("task")
    if task is not None and not _TASK_RE.match(str(task)):
        raise ConsentError("task: код задачи вида B30 или null")
    origin = e.get("origin", "live")
    if origin not in ORIGINS:
        raise ConsentError(f"origin: одно из {ORIGINS}")
    return {"ts": ts, "ts_exact": int(exact), "source": src, "question_id": qid, "answer": int(ans),
            "question_sha16": qs16, "question_code": code, "task": None if task is None else str(task), "origin": origin}


def entry_id(v: dict) -> str:
    key = {k: v[k] for k in ("ts", "source", "question_id", "answer", "question_sha16", "question_code", "task")}
    return str(uuid.uuid5(NAMESPACE, hashlib.sha256(canonical(key)).hexdigest()))


def _link(v: dict, prev: str) -> str:
    return sha16(canonical({**{k: v[k] for k in ("entry_id", "seq", "ts", "ts_exact", "source", "question_id", "answer",
                                                 "question_sha16", "question_code", "task", "origin")},
                            "prev": prev}))


def connect(db: Optional[Path] = None) -> sqlite3.Connection:
    db = Path(db or default_db())
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(DDL)
    return con


def add_many(entries: Iterable[dict], db: Optional[Path] = None, now: Optional[dt.datetime] = None) -> dict:
    """Проверка всех записей, затем дописывание в одной транзакции (ни одна не пишется при ошибке любой).
    Повтор записи -- та же строка (new=False). -> {records, new, entry_ids}."""
    now = now or dt.datetime.now()
    vals = [validate(e) for e in entries]
    con = connect(db)
    ids, n_new = [], 0
    try:
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            for v in vals:
                eid = entry_id(v)
                ids.append(eid)
                if con.execute("SELECT 1 FROM consent WHERE entry_id = ?", (eid,)).fetchone():
                    continue
                last = con.execute("SELECT seq, entry_sha16 FROM consent ORDER BY seq DESC LIMIT 1").fetchone()
                seq = 1 if last is None else last[0] + 1
                prev = GENESIS if last is None else last[1]
                row = {**v, "entry_id": eid, "seq": seq}
                row["recorded_at"] = now.replace(microsecond=0).isoformat()
                row["prev_sha16"] = prev
                row["entry_sha16"] = _link(row, prev)
                con.execute("INSERT INTO consent (" + ", ".join(COLS) + ") VALUES (" + ", ".join("?" * len(COLS)) + ")",
                            tuple(row[k] for k in COLS))
                n_new += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return {"records": len(vals), "new": n_new, "entry_ids": ids}


def add(entry: dict, db: Optional[Path] = None, now: Optional[dt.datetime] = None) -> dict:
    r = add_many([entry], db, now)
    return {"entry_id": r["entry_ids"][0], "new": bool(r["new"])}


def _ro(db: Optional[Path]):
    db = Path(db or default_db())
    if not db.is_file():
        return None
    return sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)


def list_entries(db: Optional[Path] = None, task: Optional[str] = None, code: Optional[str] = None) -> list:
    """Строки журнала по порядку записи (только чтение)."""
    con = _ro(db)
    if con is None:
        return []
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'consent'").fetchone():
            return []
        q, a = "SELECT " + ", ".join(COLS) + " FROM consent WHERE 1 = 1", []
        if task is not None:
            q, a = q + " AND task = ?", a + [task]
        if code is not None:
            q, a = q + " AND question_code = ?", a + [code]
        return [dict(zip(COLS, r)) for r in con.execute(q + " ORDER BY seq", a)]
    finally:
        con.close()


def verify(db: Optional[Path] = None) -> dict:
    """Цепочка целиком: seq 1..n без пропусков, prev = предыдущее звено, entry_sha16 и entry_id пересчитываются.
    -> {ok, rows, broken_seq (первое плохое звено | None), reason}."""
    rows = list_entries(db)
    prev = GENESIS
    for i, r in enumerate(rows, start=1):
        why = None
        if r["seq"] != i:
            why = "seq_gap"
        elif r["prev_sha16"] != prev:
            why = "prev_mismatch"
        elif r["entry_sha16"] != _link(r, prev):
            why = "entry_changed"
        else:
            try:
                v = validate({**r, "ts_exact": bool(r["ts_exact"])})
                if entry_id(v) != r["entry_id"]:
                    why = "entry_id_mismatch"
            except ConsentError:
                why = "invalid_row"
        if why:
            return {"ok": False, "rows": len(rows), "broken_seq": r["seq"], "reason": why}
        prev = r["entry_sha16"]
    return {"ok": True, "rows": len(rows), "broken_seq": None, "reason": None}


def require(task: str, code: str, db: Optional[Path] = None) -> dict:
    """Решение для блока: GO только при целой цепочке и последней записи (task, code) с answer = 1; иначе ASK.
    -> {decision: GO | ASK, reason, entry_id, ts}. Никогда не бросает (сбой базы -> ASK)."""
    try:
        if not _TASK_RE.match(str(task)) or not _CODE_RE.match(str(code)):
            return {"decision": "ASK", "reason": "bad_query", "entry_id": None, "ts": None}
        v = verify(db)
        if not v["ok"]:
            return {"decision": "ASK", "reason": f"chain_broken:{v['reason']}", "entry_id": None, "ts": None}
        rows = list_entries(db, task=task, code=code)
        if not rows:
            return {"decision": "ASK", "reason": "no_consent_recorded", "entry_id": None, "ts": None}
        last = sorted(rows, key=lambda r: (r["ts"], r["seq"]))[-1]
        if last["answer"] != 1:
            return {"decision": "ASK", "reason": "operator_answered_0", "entry_id": last["entry_id"], "ts": last["ts"]}
        return {"decision": "GO", "reason": "consent_1", "entry_id": last["entry_id"], "ts": last["ts"]}
    except Exception as e:                                   # недоступная / повреждённая база -- не действие
        return {"decision": "ASK", "reason": f"db_error:{type(e).__name__}", "entry_id": None, "ts": None}
