"""CalibrationEngine: A_N <= 0.60 -> FREEZE -> CALIBRATE -> VALIDATE -> RESUME.

Защищает обученную память от накопления ошибок. Работает автономно на статистике
подтверждений; активные словари меняет только сам (пересчёт из журнала), с версией для отката.
"""
from __future__ import annotations

from typing import Optional

from ..memory.pattern_engine import PatternEngine
from ..registry import event_registry as ev
from ..registry.event_registry import EventRegistry
from ..registry.pattern_repository import PatternRepository
from ..settings import Settings
from .accuracy_monitor import AccuracyMonitor

NORMAL_MODE = "NORMAL"
DEGRADED_MODE = "DEGRADED"


class CalibrationEngine:
    def __init__(self, repo: PatternRepository, registry: EventRegistry, monitor: AccuracyMonitor,
                 engine: PatternEngine, settings: Optional[Settings] = None) -> None:
        self.repo = repo
        self.registry = registry
        self.monitor = monitor
        self.engine = engine
        self.settings = settings or Settings()

    def mode(self, operator_id: str) -> str:
        return self.repo.calibration(operator_id)[0]

    def is_degraded(self, operator_id: str) -> bool:
        return self.mode(operator_id) == DEGRADED_MODE

    def check(self, operator_id: str) -> str:
        """Вызывается после каждой проверенной Оператором команды."""
        s = self.settings
        mode, since = self.repo.calibration(operator_id)
        if mode == NORMAL_MODE:
            acc, n = self.monitor.accuracy(operator_id, s.window, since)
            if n >= s.min_valid and acc <= s.theta_calibration:
                self._degrade(operator_id, acc, n, since)
                return DEGRADED_MODE
        else:
            acc, n = self.monitor.accuracy(operator_id, s.recover_n, since)
            conflicts = self.engine.aliases.conflicts(operator_id)
            if n >= s.recover_n and acc >= s.theta_recover and not conflicts:
                self.repo.set_calibration(operator_id, NORMAL_MODE, self.registry.last_id())
                self.registry.log(ev.CALIBRATION_RESUMED, operator_id, {"accuracy": acc, "n_valid": n})
                self.engine.reevaluate(operator_id)      # отложенные заморозкой повышения
                return NORMAL_MODE
        return mode

    def _degrade(self, operator_id: str, acc: float, n: int, since: int) -> None:
        # прежняя версия словарей сохраняется: к ней можно откатиться
        kept = self.repo.snapshot(operator_id, ev.CALIBRATION_REQUIRED, self.registry.last_id(),
                                  self.registry.now())
        errors = self.monitor.axis_errors(operator_id, self.settings.window, since)
        self.repo.set_calibration(operator_id, DEGRADED_MODE, self.registry.last_id())
        self.registry.log(ev.CALIBRATION_REQUIRED, operator_id, {
            "accuracy": acc, "n_valid": n, "axis_errors": errors, "kept_memory_version": kept})
        self.engine.recount(operator_id)                 # статистика — только по подтверждённым данным
