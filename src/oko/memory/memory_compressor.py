"""MemoryCompressor: учёт таблицы связок Оператора.

Решение Оператора 23:15 п.1: связки бессрочны, лимит 500 на Оператора, вытесняется давно
не использованная (LRU). Молча ничего не вытесняется: компрессор только находит кандидата,
решение о замене принимает Оператор (pipeline -> EVICT_REQUEST).
"""
from __future__ import annotations

from typing import Optional

from ..registry.pattern_repository import PatternRecord
from ..settings import Settings


class MemoryCompressor:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()

    @staticmethod
    def count(records: list[PatternRecord]) -> int:
        return sum(len(r.aliases) for r in records)

    @staticmethod
    def oldest(records: list[PatternRecord]) -> Optional[tuple[str, str]]:
        """(template, pattern_id) связки, которой дольше всех не пользовались.
        Порядок — по id события журнала (seq): метки времени на Windows совпадают в пределах тика часов."""
        rows = [(a.get("seq", 0), a["last_seen"], a["template"], r.pattern_id) for r in records for a in r.aliases]
        if not rows:
            return None
        *_, template, pattern_id = min(rows)
        return template, pattern_id

    @staticmethod
    def ratio(records: list[PatternRecord]) -> float:
        """CR = N_raw / N_patterns."""
        return sum(r.observations for r in records) / len(records) if records else 0.0
