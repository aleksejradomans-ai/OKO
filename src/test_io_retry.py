"""Тесты для io_retry.py.

Единственная функция модуля — retry_on_permission_error(). Все её
контракты явно описаны в докстринге и уже используются как гарантия в
Registry._replace_with_retry: (1) успех не порождает лишних попыток,
(2) PermissionError повторяется до max_attempts, потом (False, None),
(3) любая ДРУГАЯ ошибка пробрасывается немедленно, без retry — это
структурная защита, не просто деталь реализации: Registry явно опирается
на то, что не-PermissionError не проглатывается ретраями.

time.sleep подменяется автоматически для всех тестов в этом файле — без
этого тест на исчерпание всех 5 попыток с дефолтной base_delay_s=0.1
ждал бы реальные ~1.5+ секунд (экспоненциальный рост задержки).
"""
import time as time_module

import pytest

import src.io_retry as io_retry
from src.io_retry import retry_on_permission_error


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(io_retry.time, "sleep", lambda seconds: None)


def test_success_on_first_attempt_no_retry():
    call_count = 0

    def operation():
        nonlocal call_count
        call_count += 1
        return "ok"

    success, result = retry_on_permission_error(operation)

    assert success is True
    assert result == "ok"
    assert call_count == 1


def test_success_returning_none_is_still_reported_as_success():
    """os.replace/os.remove возвращают None даже при успехе — success-флаг
    должен отличать это от "упало после всех попыток", а не путать оба
    случая через result is None."""
    def operation():
        return None

    success, result = retry_on_permission_error(operation)

    assert success is True
    assert result is None


def test_succeeds_after_some_permission_errors():
    call_count = 0

    def operation():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise PermissionError("locked")
        return "recovered"

    success, result = retry_on_permission_error(operation, max_attempts=5)

    assert success is True
    assert result == "recovered"
    assert call_count == 3


def test_exhausts_all_attempts_then_returns_false_none():
    call_count = 0

    def operation():
        nonlocal call_count
        call_count += 1
        raise PermissionError("always locked")

    success, result = retry_on_permission_error(operation, max_attempts=4)

    assert success is False
    assert result is None
    assert call_count == 4  # ровно max_attempts, не больше и не меньше


def test_non_permission_error_propagates_immediately_without_retry():
    """Структурная ошибка (не PermissionError) не должна повторяться —
    Registry._replace_with_retry явно опирается на это поведение для
    собственной очистки временного файла."""
    call_count = 0

    def operation():
        nonlocal call_count
        call_count += 1
        raise OSError("disk does not exist")

    with pytest.raises(OSError):
        retry_on_permission_error(operation)

    assert call_count == 1  # никаких повторных попыток


def test_delay_uses_exponential_backoff_with_jitter(monkeypatch):
    """Проверяем реальные значения, переданные в time.sleep — задержка
    должна расти экспоненциально (base_delay_s * 2**(attempt-1)),
    с джиттером в пределах ±jitter_s."""
    sleep_calls = []
    monkeypatch.setattr(io_retry.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    call_count = 0

    def operation():
        nonlocal call_count
        call_count += 1
        raise PermissionError("locked")

    retry_on_permission_error(
        operation, max_attempts=4, base_delay_s=0.1, jitter_s=0.05,
    )

    # 4 попытки -> 3 задержки между ними (после последней попытки сна нет)
    assert len(sleep_calls) == 3

    expected_bases = [0.1 * (2 ** i) for i in range(3)]  # 0.1, 0.2, 0.4
    for actual, expected_base in zip(sleep_calls, expected_bases):
        assert expected_base - 0.05 <= actual <= expected_base + 0.05


def test_delay_never_negative_even_with_large_jitter(monkeypatch):
    """actual_delay = max(0.0, delay + jitter) — джиттер не должен уводить
    задержку в отрицательную область при маленьком base_delay_s."""
    sleep_calls = []
    monkeypatch.setattr(io_retry.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    def operation():
        raise PermissionError("locked")

    retry_on_permission_error(
        operation, max_attempts=2, base_delay_s=0.01, jitter_s=10.0,
    )

    assert all(delay >= 0.0 for delay in sleep_calls)
