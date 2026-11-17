"""AccuracyMonitor: A_N = (1/N) * sum 1(y^_i = y*_i) на скользящем окне.

Эталон y* даёт только Оператор (уточнение или явное подтверждение): отсутствие жалобы меткой
не считается. Сбои устройств сюда не попадают — это качество распознавания, а не исполнения.
"""
from __future__ import annotations

from ..registry import event_registry as ev
from ..registry.event_registry import EventRegistry


class AccuracyMonitor:
    def __init__(self, registry: EventRegistry) -> None:
        self.registry = registry

    def record(self, operator_id: str, correct: bool, axes=(), session_id: str = "", command_id: str = "") -> int:
        """axes — оси, на которых парсер ошибся: T, A, P."""
        return self.registry.log(ev.VALIDATION, operator_id, {"correct": bool(correct), "axes": sorted(axes)},
                                 session_id, command_id)

    def accuracy(self, operator_id: str, n: int, after_id: int = 0) -> tuple[float, int]:
        """(A_N, N_valid) по последним n проверенным командам после события after_id."""
        samples = self.registry.events(operator_id, (ev.VALIDATION,), after_id, last=n)
        if not samples:
            return 1.0, 0
        return sum(e.payload["correct"] for e in samples) / len(samples), len(samples)

    def axis_errors(self, operator_id: str, n: int, after_id: int = 0) -> dict:
        errors = {"T": 0, "A": 0, "P": 0}
        for e in self.registry.events(operator_id, (ev.VALIDATION,), after_id, last=n):
            for axis in e.payload["axes"]:
                errors[axis] = errors.get(axis, 0) + 1
        return errors
