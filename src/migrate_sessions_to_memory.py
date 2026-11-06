"""Однократный перенос sessions (knowledge_base.db) -> events (memory.db).

Идемпотентно: повторный запуск ничего не делает (флаг в meta).
Исходная таблица sessions НЕ удаляется — откат = просто старый bot.py.
Запуск (как служба):  $env:PYTHONPATH="C:\\oko"; & $py C:\\oko\\src\\migrate_sessions_to_memory.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import config
from src.data_hygiene import DataHygieneFilter
from src.memory_core import MemoryCore

FLAG = "migrated_sessions_v1"


class _NullIndex:
    def upsert(self, event_id, subject_id, text): pass
    def delete_subject(self, subject_id): pass
    def search(self, subject_id, query, k): return []


def _ts(value) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    f = DataHygieneFilter()
    core = MemoryCore(config.MEMORY_DB_PATH, _NullIndex(),
                      lambda t: (f.dispatch(t).sanitized_text, []),
                      backup_dir=config.MEMORY_BACKUP_DIR)
    if core.meta_get(FLAG):
        print(f"[skip] already migrated at {core.meta_get(FLAG)}")
        return

    src = sqlite3.connect(str(config.DB_PATH))
    try:
        users = [r[0] for r in src.execute("SELECT DISTINCT user_id FROM sessions")]
        total = 0
        for uid in users:
            rows = [(role, content, _ts(ts)) for role, content, ts in src.execute(
                "SELECT role, content, timestamp FROM sessions WHERE user_id=? ORDER BY id",
                (uid,))]
            total += core.import_legacy("telegram", str(uid), rows)
    finally:
        src.close()

    core.meta_set(FLAG, datetime.now(timezone.utc).isoformat())
    print(f"[ok] users={len(users)} events={total} -> {config.MEMORY_DB_PATH}")
    print(f"[ok] snapshot: {core.snapshot()}")


if __name__ == "__main__":
    main()
