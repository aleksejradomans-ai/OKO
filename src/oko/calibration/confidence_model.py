"""ConfidenceModel: потенциальные паттерны Оператора.

P^_O(y | c) = (n_{c,y} + alpha) / (n_c + alpha * |Y|) и условная энтропия H(Y | C, O).
Оценки только ранжируют кандидатов для уточнения: высокая частота паттерна не является
разрешением на исполнение и не заменяет недостающую координату.
"""
from __future__ import annotations

import math
from typing import Optional

from ..registry.pattern_repository import PatternRepository
from ..settings import Settings


def smoothed(n_cy: int, n_c: int, alpha: float, size: int) -> float:
    return (n_cy + alpha) / (n_c + alpha * max(size, 1))


def conditional_entropy(counts: dict) -> float:
    """counts: контекст -> {паттерн: n}. H(Y|C) = -sum P(c, y) * log2 P(y | c), в битах."""
    total = sum(sum(ys.values()) for ys in counts.values())
    if not total:
        return 0.0
    h = 0.0
    for ys in counts.values():
        n_c = sum(ys.values())
        for n in ys.values():
            if n:
                h -= (n / total) * math.log2(n / n_c)
    return h


class ConfidenceModel:
    def __init__(self, repo: PatternRepository, settings: Optional[Settings] = None) -> None:
        self.repo = repo
        self.settings = settings or Settings()

    def probability(self, operator_id: str, context_key: str, pattern_id: str) -> float:
        counts = self.repo.context_counts(operator_id).get(context_key, {})
        size = len(self.repo.list(operator_id))
        return smoothed(counts.get(pattern_id, 0), sum(counts.values()), self.settings.alpha, size)

    def entropy(self, operator_id: str) -> float:
        return conditional_entropy(self.repo.context_counts(operator_id))

    def rank(self, operator_id: str, context_key: str, field: str, candidates, fixed: dict) -> list:
        """Упорядочивает кандидатов координаты field по подтверждённой частоте в контексте.
        fixed — уже определённые координаты (например {"action": "on"})."""
        counts = self.repo.context_counts(operator_id).get(context_key, {})
        score = {c: 0 for c in candidates}
        for rec in self.repo.list(operator_id):
            value = getattr(rec, field, None)
            if value in score and all(getattr(rec, k) == v for k, v in fixed.items()):
                score[value] += counts.get(rec.pattern_id, 0)
        return sorted(candidates, key=lambda c: -score[c])      # сортировка устойчива
