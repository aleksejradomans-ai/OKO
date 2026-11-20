"""Registry: честный возврат при неудачной записи на диск.

_save_checkpoint() -> bool; update_state() -> False, если запись на диск не состоялась (значение в памяти при этом
новое -- работающий процесс ведёт себя как раньше); increment_state() возвращает новое значение, а о несохранении
говорят last_save_ok / unsaved_writes(). Счётчик несохранённых подряд, одно предупреждение на серию и одно при
восстановлении. Межпроцессная гонка и блокирующая пауза повторов не затрагиваются.
"""
from __future__ import annotations

import logging

import pytest

import registry as registry_mod
from registry import Registry


@pytest.fixture
def broken(registry, monkeypatch):
    """Переключатель: запись на диск падает (os.replace -> OSError, без пауз повторов) / снова работает."""
    real = registry_mod.os.replace
    state = {"fail": False}

    def replace(a, b):
        if state["fail"]:
            raise OSError("disk says no")
        return real(a, b)
    monkeypatch.setattr(registry_mod.os, "replace", replace)
    return state


@pytest.fixture
def warns():
    class H(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.records = []

        def emit(self, record):
            self.records.append(record)
    h = H()
    lg = registry_mod.logger
    old = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    yield lambda: [r.getMessage() for r in h.records if "[REGISTRY_UNSAVED]" in r.getMessage()]
    lg.removeHandler(h)
    lg.setLevel(old)


def test_happy_path_returns_true_and_no_unsaved(registry):
    assert registry.update_state("current_task", "A") is True
    assert registry.last_save_ok is True and registry.unsaved_writes() == 0
    assert registry._save_checkpoint() is True


def test_disk_failure_update_returns_false_memory_keeps_value(registry, broken):
    broken["fail"] = True
    assert registry.update_state("current_task", "B") is False
    assert registry.get_state("current_task") == "B"                 # в памяти -- новое значение
    assert registry.last_save_ok is False and registry.unsaved_writes() == 1


def test_disk_failure_increment_returns_value_and_flag_tells(registry, broken):
    broken["fail"] = True
    assert registry.increment_state("total_requests") == 1
    assert registry.increment_state("total_requests", 2) == 3        # счёт в памяти не теряется
    assert registry.last_save_ok is False and registry.unsaved_writes() == 2


def test_recovery_returns_true_and_resets_counter(registry, broken):
    broken["fail"] = True
    registry.update_state("current_task", "C")
    registry.increment_state("total_requests")
    broken["fail"] = False
    assert registry.update_state("current_task", "D") is True
    assert registry.last_save_ok is True and registry.unsaved_writes() == 0
    Registry._instance = None
    again = Registry(filepath=registry.filepath)
    assert again.get_state("current_task") == "D" and again.get_state("total_requests") == 1   # всё легло на диск


def test_one_warning_per_series_and_one_on_recovery(registry, broken, warns):
    broken["fail"] = True
    for i in range(5):
        registry.increment_state("total_requests")
    assert len(warns()) == 1 and "не записано на диск" in warns()[0]
    broken["fail"] = False
    registry.increment_state("total_requests")
    assert len(warns()) == 2 and "восстановилась после 5" in warns()[1]
    registry.increment_state("total_requests")
    assert len(warns()) == 2
    broken["fail"] = True
    registry.increment_state("total_requests")
    assert len(warns()) == 3                                        # новая серия -- новое предупреждение


def test_rejected_key_and_value_still_false_and_not_counted_as_unsaved(registry):
    assert registry.update_state("no_such_key", 1) is False
    assert registry.update_state("status", "RUNNUNG") is False
    assert registry.increment_state("no_such_key") is False
    assert registry.unsaved_writes() == 0 and registry.last_save_ok is True


def test_tempfile_failure_is_reported_too(registry, monkeypatch):
    def boom(*a, **k):
        raise OSError("no temp")
    monkeypatch.setattr(registry_mod.tempfile, "mkstemp", boom)
    assert registry.update_state("current_task", "E") is False and registry.unsaved_writes() == 1
    assert registry.get_state("current_task") == "E"


def test_governor_callers_do_not_break_when_disk_fails(registry, broken):
    """Вызывающие в ядре результат не проверяют (кроме счётчика сбоев судьи -- там число) -- они не падают."""
    import governor
    from unittest.mock import MagicMock
    gov = governor.AgentGovernor(http_client=MagicMock())
    broken["fail"] = True
    out = gov._judge_failure(RuntimeError("x"), "first pass")
    assert out == "[ERR: JUDGE_ERROR]" and gov.registry.get_state("judge_fail_streak") == 1
    gov._judge_ok()
    assert gov.registry.get_state("judge_fail_streak") == 0 and gov.registry.unsaved_writes() >= 2


def test_return_annotations():
    import inspect
    assert inspect.signature(Registry._save_checkpoint).return_annotation in (bool, "bool")
    assert inspect.signature(Registry._replace_with_retry).return_annotation in (bool, "bool")
