"""SQLite Registry: журнал событий (проверяемая история) и G6 (производное сжатое состояние)."""
from __future__ import annotations

import sqlite3

from .event_registry import Event, EventRegistry
from .pattern_repository import PatternRecord, PatternRepository

__all__ = ["Event", "EventRegistry", "PatternRecord", "PatternRepository", "open_db"]


def open_db(path: str = ":memory:", timeout: float = 10.0, check_same_thread: bool = True) -> sqlite3.Connection:
    """check_same_thread=False — для бота: вызовы идут из потоков asyncio.to_thread, доступ сериализует RLock вызывающего."""
    conn = sqlite3.connect(str(path), timeout=timeout, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn
