import asyncio

from src.monitor import ComplianceMonitor, ComplianceEvent
from src.circuit_breaker import CircuitBreaker, BreakerState

import pytest
import src.circuit_breaker as _cb


class _ManualClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


_CLOCK = _ManualClock()


@pytest.fixture(autouse=True)
def _manual_clock(monkeypatch):
    """Детерминированное время: автомат видит только ручные часы."""
    global _CLOCK
    _CLOCK = _ManualClock()
    monkeypatch.setattr(_cb, "_now", _CLOCK)
    yield _CLOCK


def _advance(seconds):
    _CLOCK.t += seconds



def _make(monitor=None, **kwargs):
    monitor = monitor or ComplianceMonitor(window_size=10)
    defaults = dict(error_threshold=0.3, min_sample=5, cooldown_s=1.0)
    defaults.update(kwargs)
    return monitor, CircuitBreaker(monitor, **defaults)


def test_starts_closed_with_empty_monitor():
    _, breaker = _make()
    assert asyncio.run(breaker.guard()) == BreakerState.CLOSED


def test_stays_closed_below_min_sample():
    monitor, breaker = _make()
    # 2/3 нарушений — доля выше порога, но выборка меньше min_sample=5
    monitor.record_event(ComplianceEvent.VIOLATION)
    monitor.record_event(ComplianceEvent.VIOLATION)
    monitor.record_event(ComplianceEvent.PASS)
    assert asyncio.run(breaker.guard()) == BreakerState.CLOSED


def test_opens_on_threshold_with_enough_sample():
    monitor, breaker = _make()
    # 2 нарушения из 5 = 40% >= порога 0.3, выборка достаточна
    monitor.record_event(ComplianceEvent.VIOLATION)
    monitor.record_event(ComplianceEvent.VIOLATION)
    monitor.record_event(ComplianceEvent.PASS)
    monitor.record_event(ComplianceEvent.PASS)
    monitor.record_event(ComplianceEvent.PASS)
    assert asyncio.run(breaker.guard()) == BreakerState.OPEN


def test_unchecked_events_excluded_from_error_rate():
    monitor, breaker = _make()
    # UNCHECKED не входит в error_rate (см. monitor.get_error_rate) —
    # 5 UNCHECKED не должны открыть прерыватель сами по себе.
    for _ in range(5):
        monitor.record_event(ComplianceEvent.UNCHECKED)
    assert asyncio.run(breaker.guard()) == BreakerState.CLOSED


def test_stays_open_within_cooldown():
    monitor, breaker = _make(cooldown_s=60.0)
    for _ in range(5):
        monitor.record_event(ComplianceEvent.VIOLATION)
    assert asyncio.run(breaker.guard()) == BreakerState.OPEN
    # Повторный вызов сразу после открытия — всё ещё внутри cooldown
    assert asyncio.run(breaker.guard()) == BreakerState.OPEN


def test_half_open_after_cooldown_elapses():
    monitor, breaker = _make(cooldown_s=0.05)
    for _ in range(5):
        monitor.record_event(ComplianceEvent.VIOLATION)
    assert asyncio.run(breaker.guard()) == BreakerState.OPEN
    import time
    _advance(0.06)
    assert asyncio.run(breaker.guard()) == BreakerState.HALF_OPEN


def test_single_success_not_enough_with_default_threshold():
    # success_threshold по умолчанию = 2: одной пробы недостаточно,
    # чтобы полностью закрыться — иначе один случайный удачный ответ
    # маскирует всё ещё нездоровое окно ComplianceMonitor (дребезг).
    monitor, breaker = _make(cooldown_s=0.05)
    for _ in range(5):
        monitor.record_event(ComplianceEvent.VIOLATION)
    asyncio.run(breaker.guard())
    import time
    _advance(0.06)
    assert asyncio.run(breaker.guard()) == BreakerState.HALF_OPEN
    breaker.record_probe_result(success=True)
    # Всё ещё HALF_OPEN, не CLOSED и не OPEN — ждём вторую успешную пробу
    assert asyncio.run(breaker.guard()) == BreakerState.HALF_OPEN


def test_consecutive_successes_close_breaker_and_survive_stale_window():
    # window_size=10 нарочно больше 5 старых VIOLATION — они остаются в
    # окне и после закрытия, ComplianceMonitor их не чистит.
    monitor, breaker = _make(cooldown_s=0.05, success_threshold=2)
    for _ in range(5):
        monitor.record_event(ComplianceEvent.VIOLATION)
    asyncio.run(breaker.guard())
    import time
    _advance(0.06)
    asyncio.run(breaker.guard())
    breaker.record_probe_result(success=True)
    asyncio.run(breaker.guard())
    breaker.record_probe_result(success=True)
    # Вторая подряд успешная проба — полное закрытие.
    assert asyncio.run(breaker.guard()) == BreakerState.CLOSED
    # Ключевая проверка: 5 старых VIOLATION всё ещё в окне (100% error
    # rate), но без grace-периода это немедленно переоткрыло бы брейкер
    # на следующем же guard(). С grace-периодом — остаётся CLOSED, пока
    # не накопится min_sample=5 СВЕЖИХ событий.
    assert asyncio.run(breaker.guard()) == BreakerState.CLOSED
    assert monitor.get_error_rate() == 1.0  # окно объективно всё ещё "грязное"


def test_grace_period_expires_after_enough_fresh_bad_samples():
    monitor, breaker = _make(cooldown_s=0.05, success_threshold=1, min_sample=3)
    monitor.record_event(ComplianceEvent.VIOLATION)
    monitor.record_event(ComplianceEvent.VIOLATION)
    monitor.record_event(ComplianceEvent.VIOLATION)
    asyncio.run(breaker.guard())
    import time
    _advance(0.06)
    asyncio.run(breaker.guard())
    breaker.record_probe_result(success=True)  # закрылись, baseline = 4 события
    assert asyncio.run(breaker.guard()) == BreakerState.CLOSED
    # Реальный трафик после восстановления снова начинает сбоить —
    # 3 новых VIOLATION поверх baseline исчерпывают grace-период.
    monitor.record_event(ComplianceEvent.VIOLATION)
    monitor.record_event(ComplianceEvent.VIOLATION)
    monitor.record_event(ComplianceEvent.VIOLATION)
    assert asyncio.run(breaker.guard()) == BreakerState.OPEN


def test_failure_during_half_open_resets_success_streak():
    monitor, breaker = _make(cooldown_s=0.05, success_threshold=3)
    for _ in range(5):
        monitor.record_event(ComplianceEvent.VIOLATION)
    asyncio.run(breaker.guard())
    import time
    _advance(0.06)
    asyncio.run(breaker.guard())
    breaker.record_probe_result(success=True)   # 1/3
    asyncio.run(breaker.guard())
    breaker.record_probe_result(success=False)  # сброс -> 0/3, обратно в OPEN
    assert asyncio.run(breaker.guard()) == BreakerState.OPEN


def test_failed_probe_keeps_open_and_resets_timer():
    monitor, breaker = _make(cooldown_s=0.05)
    for _ in range(5):
        monitor.record_event(ComplianceEvent.VIOLATION)
    asyncio.run(breaker.guard())
    import time
    _advance(0.06)
    assert asyncio.run(breaker.guard()) == BreakerState.HALF_OPEN
    breaker.record_probe_result(success=False)
    assert asyncio.run(breaker.guard()) == BreakerState.OPEN


def test_state_hint_follows_lifecycle_without_mutating_state():
    monitor, breaker = _make(cooldown_s=0.05)
    assert breaker.state_hint == BreakerState.CLOSED
    for _ in range(5):
        monitor.record_event(ComplianceEvent.VIOLATION)
    # Окно уже "грязное", но пока не было guard() — подсказка не
    # пересчитывает пороги и не открывает брейкер сама.
    assert breaker.state_hint == BreakerState.CLOSED
    asyncio.run(breaker.guard())
    assert breaker.state_hint == BreakerState.OPEN
    import time
    _advance(0.06)
    assert breaker.state_hint == BreakerState.HALF_OPEN

