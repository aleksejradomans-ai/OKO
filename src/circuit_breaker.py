"""Circuit breaker поверх ComplianceMonitor: управляет состоянием
деградации контура, отделяя логику переходов от транспорта и истории
событий (история остаётся единственной ответственностью ComplianceMonitor)."""
from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Optional

from src.monitor import ComplianceMonitor


def _now() -> float:
    """Единственная точка чтения времени в модуле. Тесты подменяют её ручными
    часами: на Windows time.monotonic() до Python 3.13 идёт шагом ~15.6 мс,
    и тайминговые тесты с запасом в 10 мс падали примерно в 1 из 6 прогонов."""
    return time.monotonic()



class BreakerState(str, Enum):
    CLOSED = "CLOSED"        # Нормальный режим, запросы идут напрямую
    OPEN = "OPEN"            # Деградация зафиксирована, запросы блокируются
    HALF_OPEN = "HALF_OPEN"  # Колдаун истёк, разрешена одна пробная операция


class CircuitBreaker:
    def __init__(
        self,
        monitor: ComplianceMonitor,
        error_threshold: float = 0.3,
        min_sample: int = 10,
        cooldown_s: float = 60.0,
        success_threshold: int = 2,
    ) -> None:
        self._monitor = monitor
        self._error_threshold = error_threshold
        self._min_sample = min_sample
        self._cooldown_s = cooldown_s
        # Сколько подряд успешных проб в HALF_OPEN нужно для полного
        # закрытия. Не чистим историю ComplianceMonitor при успехе —
        # он разделяемый компонент (governor использует его и для
        # get_unchecked_rate(), не только через breaker), и один успех
        # статистически слишком слаб, чтобы объявлять весь "хвост"
        # ошибок в окне нерелевантным. Вместо этого гасим дребезг
        # (chattering) требованием серии успехов — стандартный паттерн
        # circuit breaker (resilience4j и аналоги), а не спец-решение.
        self._success_threshold = success_threshold
        self._opened_at: Optional[float] = None
        self._half_open_successes = 0
        self._lock = asyncio.Lock()

        # Размер окна ComplianceMonitor на момент последнего перехода в
        # CLOSED через успешные пробы. Пока свежих событий (после этой
        # отметки) меньше min_sample, _should_open() не имеет права
        # переоткрыть прерыватель — иначе решение принимается по данным,
        # накопленным ДО восстановления, и брейкер дребезжит: закрылся —
        # тут же открылся снова по тому же старому "хвосту" нарушений.
        # None — grace-период неактивен (обычный холодный старт).
        self._recovery_baseline: Optional[int] = None

    def _should_open(self) -> bool:
        if not (
            self._monitor.get_sample_size() >= self._min_sample
            and self._monitor.get_error_rate() >= self._error_threshold
        ):
            return False
        if self._recovery_baseline is not None:
            fresh_samples = self._monitor.get_sample_size() - self._recovery_baseline
            if fresh_samples < self._min_sample:
                return False  # ещё не набрали свежую выборку после восстановления
        return True

    async def guard(self) -> BreakerState:
        """Проверяет состояние перед выполнением запроса. Вызывающий код
        (governor) решает, что делать с HALF_OPEN (пропустить как пробу)
        и OPEN (отказать без похода к LLM)."""
        async with self._lock:
            if self._opened_at is None:
                if self._should_open():
                    self._opened_at = _now()
                    self._recovery_baseline = None  # grace-период исчерпан/недействителен
                    return BreakerState.OPEN
                return BreakerState.CLOSED

            elapsed = _now() - self._opened_at
            if elapsed < self._cooldown_s:
                return BreakerState.OPEN
            return BreakerState.HALF_OPEN

    def record_probe_result(self, success: bool) -> None:
        """Фиксирует исход пробного запроса в режиме HALF_OPEN.
        Не берёт lock — вызывается строго после guard() вернул HALF_OPEN
        и governor уже выполнил (или не выполнил) пробный запрос вне
        критической секции guard().

        Любая неудача в HALF_OPEN немедленно обнуляет счётчик успехов и
        переоткрывает окно cooldown — не даёт "почти восстановившейся"
        системе проскочить закрытие на случайном одном хорошем ответе.
        """
        if success:
            self._half_open_successes += 1
            if self._half_open_successes >= self._success_threshold:
                self._opened_at = None  # Полное закрытие -> CLOSED
                self._half_open_successes = 0
                # Фиксируем точку отсчёта для grace-периода: следующее
                # решение об открытии будет ждать min_sample СВЕЖИХ
                # событий, а не смотреть на текущий (всё ещё "грязный")
                # снимок окна monitor.
                self._recovery_baseline = self._monitor.get_sample_size()
            # иначе остаёмся в HALF_OPEN: _opened_at не трогаем, следующий
            # guard() снова вернёт HALF_OPEN (cooldown уже истёк раньше)
        else:
            self._half_open_successes = 0
            self._opened_at = _now()  # Обратно в OPEN, сброс таймера

    @property
    def state_hint(self) -> BreakerState:
        """Синхронное чтение состояния для логов/метрик, без изменения
        состояния и без лока — не использовать для решений о пропуске
        запроса, только для наблюдаемости."""
        if self._opened_at is None:
            return BreakerState.CLOSED
        elapsed = _now() - self._opened_at
        return BreakerState.OPEN if elapsed < self._cooldown_s else BreakerState.HALF_OPEN
