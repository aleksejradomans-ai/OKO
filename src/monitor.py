"""Скользящее окно для метрик деградации качества ответов.

Изолирован от Registry: другая модель данных (история событий за
последние N запросов, а не текущее состояние системы), другие требования
к сериализации (не персистентно — см. обсуждение в истории решений).
"""
from __future__ import annotations

import threading
from collections import deque
from enum import Enum


class ComplianceEvent(Enum):
    PASS = 0
    VIOLATION = 1
    UNCHECKED = 2  # судья/корректор сбойнул технически — не успех и не нарушение


class ComplianceMonitor:
    """Потокобезопасное in-memory окно последних N событий.
    Не персистентно намеренно: заполняется за первые N запросов после
    рестарта, и эта метрика про оперативное состояние "сейчас", а не
    про исторический аудит (для аудита — Registry.compliance_violations).
    """

    def __init__(self, window_size: int = 100) -> None:
        self._window: deque[ComplianceEvent] = deque(maxlen=window_size)
        self._lock = threading.Lock()
        self.window_size = window_size

    def record_event(self, event: ComplianceEvent) -> None:
        with self._lock:
            self._window.append(event)

    def get_error_rate(self) -> float:
        """Доля VIOLATION среди ПРОВЕРЕННЫХ событий (UNCHECKED исключены).
        Возвращает 0.0 если нет проверенных событий в окне — не путать
        с "всё хорошо", проверь get_sample_size перед интерпретацией.
        """
        with self._lock:
            checked = [e for e in self._window if e != ComplianceEvent.UNCHECKED]
            if not checked:
                return 0.0
            violations = sum(1 for e in checked if e == ComplianceEvent.VIOLATION)
            return violations / len(checked)

    def get_unchecked_rate(self) -> float:
        """Доля технических сбоев судьи в окне. Отдельный сигнал —
        рост этой метрики означает деградацию инфраструктуры (Ollama),
        не деградацию качества модели."""
        with self._lock:
            if not self._window:
                return 0.0
            unchecked = sum(1 for e in self._window if e == ComplianceEvent.UNCHECKED)
            return unchecked / len(self._window)

    def get_sample_size(self) -> int:
        with self._lock:
            return len(self._window)
