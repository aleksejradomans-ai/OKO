"""CrystallizationEngine: закон кристаллизации.

Уровень паттерна зависит от проверенных результатов, а не от числа повторений слов.
"""
from __future__ import annotations

from typing import Optional

from ..registry.pattern_repository import CANDIDATE, CRYSTALLIZED, DEGRADED, PROVISIONAL, PatternRecord
from ..settings import Settings

_RANK = {DEGRADED: -1, CANDIDATE: 0, PROVISIONAL: 1, CRYSTALLIZED: 2}
VERIFIED = "VERIFIED"


class CrystallizationEngine:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()

    @staticmethod
    def quality(confirmed_correct: int, confirmed_incorrect: int) -> float:
        """q_j = (s_j + 1) / (s_j + f_j + 2) — апостериорное среднее при Beta(1, 1)."""
        return (confirmed_correct + 1) / (confirmed_correct + confirmed_incorrect + 2)

    @staticmethod
    def learn(operator_validated: bool, target_resolved: bool, action_resolved: bool,
              execution_verified: bool) -> bool:
        """Learn = 1 только при одновременном выполнении всех четырёх условий."""
        return operator_validated and target_resolved and action_resolved and execution_verified

    def next_status(self, rec: PatternRecord, frozen: bool = False) -> str:
        s = self.settings
        q = self.quality(rec.confirmed_correct, rec.confirmed_incorrect)
        if rec.confirmed_incorrect and q < s.degrade_q:      # без отказов Оператора деградации нет
            new = DEGRADED
        elif rec.learned >= s.cryst_min_confirmed and rec.session_count >= s.cryst_min_sessions and q >= s.cryst_q:
            new = CRYSTALLIZED
        elif rec.learned >= 1:
            new = PROVISIONAL
        else:
            new = CANDIDATE
        if frozen and _RANK[new] > _RANK[rec.status]:
            return rec.status        # при калибровке повышение статусов заморожено
        return new
