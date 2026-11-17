from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class PolicyRule:
    """Одно правило манифеста с машиночитаемым ID.
    ID нужен, чтобы судья мог сослаться на конкретный пункт (FAIL:POL_001),
    а не просто вернуть бинарный вердикт без объяснения (см. историю решений
    — Вердикт 2.0, аудит от 16.07.2026)."""
    id: str
    text: str


@dataclass(frozen=True)
class Manifesto:
    # PRIMARY_OBJECTIVE намеренно БЕЗ id — это не запрет, который можно
    # "нарушить" бинарно, а общая цель, задающая тон ответа. Судья использует
    # её как контекст интерпретации, не как отдельный проверяемый пункт.
    PRIMARY_OBJECTIVE: Final[str] = (
        "Maximum accuracy, verifiability, logical consistency."
    )

    HARD_PROHIBITION: Final[PolicyRule] = PolicyRule(
        id="POL_001",
        text=(
            "Do not simulate emotions, do not give advice without factual "
            "grounding, do not distort data for user comfort."
        ),
    )
    HALT_CONDITION: Final[PolicyRule] = PolicyRule(
        id="POL_002",
        text="Logical contradiction within the response exceeding the entropy threshold.",
    )

    def rules_by_id(self) -> dict[str, PolicyRule]:
        """Единая точка поиска правила по ID — используется _parse_verdict
        для валидации, что судья сослался на реально существующий пункт,
        а не на выдуманный/искажённый ID."""
        return {r.id: r for r in (self.HARD_PROHIBITION, self.HALT_CONDITION)}
