"""PatternRepository: хранение памяти G6 в SQLite, версии памяти и состояние калибровки.

Размер G6 зависит от числа уникальных паттернов, а не от числа команд: повтор обновляет счётчики.
Каждое структурное изменение (новый паттерн, псевдоним, смена статуса) даёт новую версию —
полный снимок памяти Оператора, к которому можно откатиться.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS oko_patterns (
    pattern_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS oko_patterns_op ON oko_patterns (operator_id);
CREATE TABLE IF NOT EXISTS oko_aliases (
    operator_id TEXT NOT NULL,
    template TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    PRIMARY KEY (operator_id, template, pattern_id)
);
CREATE TABLE IF NOT EXISTS oko_memory_versions (
    operator_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL,
    last_event_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (operator_id, version)
);
CREATE TABLE IF NOT EXISTS oko_context_counts (
    operator_id TEXT NOT NULL,
    context_key TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (operator_id, context_key, pattern_id)
);
CREATE TABLE IF NOT EXISTS oko_calibration (
    operator_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    since_event_id INTEGER NOT NULL
);
"""

CANDIDATE = "CANDIDATE"          # наблюдался, но подтверждённого проверенного исполнения ещё нет
PROVISIONAL = "PROVISIONAL"
CRYSTALLIZED = "CRYSTALLIZED"
DEGRADED = "DEGRADED"


@dataclass
class PatternRecord:
    pattern_id: str
    operator_id: str
    canonical_template: str
    target: str
    action: str
    parameter_schema: dict = field(default_factory=dict)
    aliases: list = field(default_factory=list)   # {"template", "count", "confirmed", "last_seen"}
    observations: int = 0
    confirmed_correct: int = 0        # s_j: сопоставление подтверждено Оператором
    confirmed_incorrect: int = 0      # f_j: сопоставление отвергнуто Оператором
    verified_executions: int = 0      # фактическое состояние устройства совпало с командой
    execution_failures: int = 0       # сбои исполнительного контура; на f_j не влияют
    learned: int = 0                  # Learn = 1: подтверждено Оператором и проверено по устройству
    confidence: float = 0.5           # q_j = (s + 1) / (s + f + 2)
    status: str = CANDIDATE
    first_seen: str = ""
    last_seen: str = ""
    session_count: int = 0
    sessions: list = field(default_factory=list)  # последние сессии с Learn = 1 (для подсчёта разных)
    examples: list = field(default_factory=list)

    def alias(self, template: str) -> Optional[dict]:
        return next((a for a in self.aliases if a["template"] == template), None)


class PatternRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.executescript(SCHEMA)

    # ---- паттерны -----------------------------------------------------------------------
    def get(self, pattern_id: str) -> Optional[PatternRecord]:
        row = self.conn.execute("SELECT body FROM oko_patterns WHERE pattern_id = ?", (pattern_id,)).fetchone()
        return PatternRecord(**json.loads(row[0])) if row else None

    def save(self, rec: PatternRecord) -> None:
        self.conn.execute("INSERT OR REPLACE INTO oko_patterns (pattern_id, operator_id, body) VALUES (?,?,?)",
                          (rec.pattern_id, rec.operator_id, json.dumps(asdict(rec), ensure_ascii=False)))
        self.conn.execute("DELETE FROM oko_aliases WHERE pattern_id = ?", (rec.pattern_id,))
        self.conn.executemany("INSERT INTO oko_aliases (operator_id, template, pattern_id) VALUES (?,?,?)",
                              [(rec.operator_id, a["template"], rec.pattern_id) for a in rec.aliases])

    def list(self, operator_id: str) -> list[PatternRecord]:
        rows = self.conn.execute("SELECT body FROM oko_patterns WHERE operator_id = ? ORDER BY pattern_id",
                                 (operator_id,)).fetchall()
        return [PatternRecord(**json.loads(r[0])) for r in rows]

    def by_alias(self, operator_id: str, template: str) -> list[PatternRecord]:
        rows = self.conn.execute(
            "SELECT p.body FROM oko_aliases a JOIN oko_patterns p USING (pattern_id) "
            "WHERE a.operator_id = ? AND a.template = ? ORDER BY p.pattern_id", (operator_id, template)).fetchall()
        return [PatternRecord(**json.loads(r[0])) for r in rows]

    def clear(self, operator_id: str) -> None:
        for table in ("oko_patterns", "oko_aliases", "oko_context_counts"):
            self.conn.execute(f"DELETE FROM {table} WHERE operator_id = ?", (operator_id,))

    # ---- версии памяти ------------------------------------------------------------------
    def snapshot(self, operator_id: str, reason: str, last_event_id: int, ts: str) -> int:
        version = self.version(operator_id) + 1
        body = json.dumps({
            "patterns": [asdict(r) for r in self.list(operator_id)],
            "context": [list(r) for r in self.conn.execute(
                "SELECT context_key, pattern_id, n FROM oko_context_counts WHERE operator_id = ?", (operator_id,))],
        }, ensure_ascii=False)
        self.conn.execute("INSERT INTO oko_memory_versions VALUES (?,?,?,?,?,?)",
                          (operator_id, version, ts, reason, last_event_id, body))
        return version

    def version(self, operator_id: str) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(version), 0) FROM oko_memory_versions WHERE operator_id = ?",
                                (operator_id,)).fetchone()
        return int(row[0])

    def versions(self, operator_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT version, ts, reason, last_event_id FROM oko_memory_versions "
                                 "WHERE operator_id = ? ORDER BY version", (operator_id,)).fetchall()
        return [dict(r) for r in rows]

    def restore(self, operator_id: str, version: int) -> int:
        """Возвращает память к снимку; отдаёт id последнего события, вошедшего в снимок."""
        row = self.conn.execute("SELECT last_event_id, body FROM oko_memory_versions "
                                "WHERE operator_id = ? AND version = ?", (operator_id, version)).fetchone()
        if row is None:
            raise KeyError(f"memory version {version} not found for operator {operator_id!r}")
        body = json.loads(row["body"])
        self.clear(operator_id)
        for item in body["patterns"]:
            self.save(PatternRecord(**item))
        self.conn.executemany("INSERT INTO oko_context_counts VALUES (?,?,?,?)",
                              [(operator_id, *c) for c in body["context"]])
        return int(row["last_event_id"])

    # ---- статистика контекста: n_{c,y} --------------------------------------------------
    def bump_context(self, operator_id: str, context_key: str, pattern_id: str) -> None:
        self.conn.execute(
            "INSERT INTO oko_context_counts VALUES (?,?,?,1) "
            "ON CONFLICT (operator_id, context_key, pattern_id) DO UPDATE SET n = n + 1",
            (operator_id, context_key, pattern_id))

    def context_counts(self, operator_id: str) -> dict:
        """context_key -> {pattern_id: n}."""
        out: dict = {}
        for r in self.conn.execute("SELECT context_key, pattern_id, n FROM oko_context_counts "
                                   "WHERE operator_id = ?", (operator_id,)):
            out.setdefault(r[0], {})[r[1]] = r[2]
        return out

    # ---- состояние калибровки -----------------------------------------------------------
    def calibration(self, operator_id: str) -> tuple[str, int]:
        row = self.conn.execute("SELECT mode, since_event_id FROM oko_calibration WHERE operator_id = ?",
                                (operator_id,)).fetchone()
        return (row[0], int(row[1])) if row else ("NORMAL", 0)

    def set_calibration(self, operator_id: str, mode: str, since_event_id: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO oko_calibration VALUES (?,?,?)",
                          (operator_id, mode, since_event_id))
