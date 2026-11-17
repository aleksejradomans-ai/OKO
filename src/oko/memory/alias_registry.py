"""AliasRegistry: персональная таблица W_O — шаблон реплики -> паттерны Оператора."""
from __future__ import annotations

from ..registry.pattern_repository import CRYSTALLIZED, PatternRecord, PatternRepository


class AliasRegistry:
    def __init__(self, repo: PatternRepository) -> None:
        self.repo = repo

    def crystallized(self, operator_id: str, template: str) -> list[PatternRecord]:
        """Псевдонимы, допущенные к автоматическому применению: паттерн кристаллизован,
        а именно эта формулировка хотя бы раз подтверждена Оператором."""
        return [r for r in self.repo.by_alias(operator_id, template)
                if r.status == CRYSTALLIZED and r.alias(template)["confirmed"] >= 1]

    def suggestions(self, operator_id: str, template: str) -> list[PatternRecord]:
        """Все паттерны, где встречалась формулировка, — только для ранжирования кандидатов."""
        recs = self.repo.by_alias(operator_id, template)
        return sorted(recs, key=lambda r: (-r.alias(template)["confirmed"], -r.confidence, r.pattern_id))

    def conflicts(self, operator_id: str) -> list[str]:
        """Критический конфликт словаря: одна формулировка кристаллизована в разных паттернах."""
        seen: dict = {}
        for rec in self.repo.list(operator_id):
            if rec.status != CRYSTALLIZED:
                continue
            for a in rec.aliases:
                if a["confirmed"] >= 1:
                    seen.setdefault(a["template"], set()).add(rec.pattern_id)
        return sorted(t for t, ids in seen.items() if len(ids) > 1)
