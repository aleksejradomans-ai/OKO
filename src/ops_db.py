"""ops_db.py — служебная БД «Ока» (data\\ops.db): сводки сна (M7.1) и индекс дрейфа (M7.2).

Инварианты:
1. Только служебные данные. Ни текста сообщений, ни знаний: knowledge_base.db,
   memory.db и Chroma этот модуль не открывает. rag_resync/экспорт её не видят.
2. WAL + короткие транзакции (X4). Сводка и курсор чтения лога пишутся одной
   транзакцией: либо обе записи, либо ни одной (повторный прогон не задвоит окно).
3. База дрейфа замораживается один раз и дальше не перезаписывается
   (сброс — только явной командой, отдельным решением).
4. Решений здесь нет: только хранение. Пороги/действия — после калибровки (как M6).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log_cursor (
    source TEXT PRIMARY KEY,
    offset INTEGER NOT NULL,
    head_hash TEXT NOT NULL,
    head_len INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sleep_digest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    source TEXT NOT NULL,
    window_start REAL,
    window_end REAL,
    lines INTEGER NOT NULL,
    answers INTEGER NOT NULL,
    payload TEXT NOT NULL,
    drift_score REAL,
    drift_status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sleep_digest_window ON sleep_digest(window_end);
CREATE TABLE IF NOT EXISTS drift_baseline (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    created_at REAL NOT NULL,
    window_start REAL NOT NULL,
    window_end REAL NOT NULL,
    samples INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Cursor:
    offset: int
    head_hash: str
    head_len: int


@dataclass(frozen=True)
class DigestRow:
    id: int
    created_at: float
    window_start: Optional[float]
    window_end: Optional[float]
    lines: int
    answers: int
    payload: dict
    drift_score: Optional[float]
    drift_status: str


class OpsDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as c:
            c.executescript(_SCHEMA)
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                c.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(f"ops.db schema {row[0]} newer than code {SCHEMA_VERSION}")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")  # служебные данные; потеря последней сводки допустима
        return c

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = self._conn()
        try:
            with c:
                yield c
        finally:
            c.close()

    # --- курсор лога ------------------------------------------------------
    def get_cursor(self, source: str) -> Optional[Cursor]:
        with self._tx() as c:
            row = c.execute("SELECT offset, head_hash, head_len FROM log_cursor WHERE source=?",
                            (source,)).fetchone()
        return Cursor(*row) if row else None

    def save_cursor(self, source: str, cur: Cursor) -> None:
        with self._tx() as c:
            self._put_cursor(c, source, cur)

    @staticmethod
    def _put_cursor(c: sqlite3.Connection, source: str, cur: Cursor) -> None:
        c.execute("INSERT OR REPLACE INTO log_cursor VALUES (?, ?, ?, ?, ?)",
                  (source, cur.offset, cur.head_hash, cur.head_len, time.time()))

    # --- сводки -------------------------------------------------------------
    def commit_digest(self, source: str, cur: Cursor, window_start: Optional[float],
                      window_end: Optional[float], lines: int, answers: int, payload: dict,
                      drift_score: Optional[float], drift_status: str) -> int:
        """Сводка + курсор одной транзакцией."""
        with self._tx() as c:
            rid = c.execute(
                "INSERT INTO sleep_digest (created_at, source, window_start, window_end, lines, "
                "answers, payload, drift_score, drift_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), source, window_start, window_end, lines, answers,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 drift_score, drift_status)).lastrowid
            self._put_cursor(c, source, cur)
        return int(rid)

    def recent_digests(self, limit: int = 50) -> List[DigestRow]:
        """Последние сводки, от новых к старым."""
        with self._tx() as c:
            rows = c.execute(
                "SELECT id, created_at, window_start, window_end, lines, answers, payload, "
                "drift_score, drift_status FROM sleep_digest ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [DigestRow(r[0], r[1], r[2], r[3], r[4], r[5], json.loads(r[6]), r[7], r[8])
                for r in rows]

    def last_digest(self) -> Optional[DigestRow]:
        rows = self.recent_digests(1)
        return rows[0] if rows else None

    def first_window_start(self) -> Optional[float]:
        with self._tx() as c:
            return c.execute("SELECT MIN(window_start) FROM sleep_digest").fetchone()[0]

    def all_metric_payloads(self) -> List[dict]:
        with self._tx() as c:
            rows = c.execute("SELECT payload FROM sleep_digest ORDER BY id").fetchall()
        return [json.loads(r[0]) for r in rows]

    # --- база дрейфа ----------------------------------------------------------
    def get_baseline(self) -> Optional[dict]:
        with self._tx() as c:
            row = c.execute("SELECT window_start, window_end, samples, payload "
                            "FROM drift_baseline WHERE id=1").fetchone()
        if not row:
            return None
        return {"window_start": row[0], "window_end": row[1], "samples": row[2],
                "stats": json.loads(row[3])}

    def freeze_baseline(self, window_start: float, window_end: float, samples: int,
                        stats: dict) -> bool:
        """Записывает базу, только если её ещё нет. True — записано сейчас."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO drift_baseline VALUES (1, ?, ?, ?, ?, ?)",
                (time.time(), window_start, window_end, samples,
                 json.dumps(stats, ensure_ascii=False, sort_keys=True)))
            return cur.rowcount == 1
