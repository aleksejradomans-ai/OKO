"""EventRegistry: append-only журнал всех переходов состояний (инвариант 7).

Из него пересчитывается статистика паттернов и считается точность на скользящем окне.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS oko_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    command_id TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS oko_events_op_type ON oko_events (operator_id, type, id);
"""

# Типы событий
OPERATOR_INPUT = "OPERATOR_INPUT"
INTENT_PARSED = "INTENT_PARSED"
CLARIFY_REQUEST = "CLARIFY_REQUEST"
CONFIRM_REQUEST = "CONFIRM_REQUEST"
OPERATOR_RESPONSE = "OPERATOR_RESPONSE"
REQUEST_EXPIRED = "REQUEST_EXPIRED"
REQUEST_CANCELLED = "REQUEST_CANCELLED"
ABSTRACT_ADVISORY = "ABSTRACT_ADVISORY"
BIND_REQUESTED = "BIND_REQUESTED"
EXECUTION = "EXECUTION"
INFRA_INCIDENT = "INFRA_INCIDENT"
VALIDATION = "VALIDATION"
EVIDENCE = "EVIDENCE"
PATTERN_CREATED = "PATTERN_CREATED"
ALIAS_ADDED = "ALIAS_ADDED"
ALIAS_EVICTED = "ALIAS_EVICTED"        # прежнее молчаливое TOP-N вытеснение; новые записи не пишутся
BIND_REFUSED = "BIND_REFUSED"          # «Запомнить связку» без подтверждённого результата
EVICT_REQUEST = "EVICT_REQUEST"        # таблица связок полна: вопрос Оператору
BIND_EVICTED = "BIND_EVICTED"          # вытеснение с согласия Оператора; переигрывается при пересчёте
PATTERN_STATUS = "PATTERN_STATUS"
CALIBRATION_REQUIRED = "CALIBRATION_REQUIRED"
CALIBRATION_RESUMED = "CALIBRATION_RESUMED"
MEMORY_RECOUNT = "MEMORY_RECOUNT"
MEMORY_ROLLBACK = "MEMORY_ROLLBACK"


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    operator_id: str
    session_id: str
    command_id: str
    type: str
    payload: dict


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class EventRegistry:
    def __init__(self, conn: sqlite3.Connection, clock: Optional[Callable[[], _dt.datetime]] = None) -> None:
        self.conn = conn
        self.clock = clock or _utc_now
        conn.executescript(SCHEMA)

    def now(self) -> str:
        return self.clock().isoformat()

    def log(self, type: str, operator_id: str, payload: Optional[dict] = None,
            session_id: str = "", command_id: str = "", ts: Optional[str] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO oko_events (ts, operator_id, session_id, command_id, type, payload) VALUES (?,?,?,?,?,?)",
            (ts or self.now(), operator_id, session_id, command_id, type,
             json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)))
        return int(cur.lastrowid)

    def last_id(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM oko_events").fetchone()
        return int(row[0])

    def events(self, operator_id: Optional[str] = None, types=(), after_id: int = 0,
               last: Optional[int] = None) -> list[Event]:
        """В порядке записи; last — только последние N подходящих."""
        sql, args = "SELECT * FROM oko_events WHERE id > ?", [after_id]
        if operator_id is not None:
            sql += " AND operator_id = ?"
            args.append(operator_id)
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args += list(types)
        sql += " ORDER BY id DESC" if last else " ORDER BY id"
        if last:
            sql += " LIMIT ?"
            args.append(last)
        rows = self.conn.execute(sql, args).fetchall()
        if last:
            rows.reverse()
        return [Event(r["id"], r["ts"], r["operator_id"], r["session_id"], r["command_id"], r["type"],
                      json.loads(r["payload"])) for r in rows]
