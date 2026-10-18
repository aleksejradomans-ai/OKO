"""Тесты правок после F1: служебные маркеры сбоя не попадают в историю
диалога, а /status показывает состояние circuit breaker.

Отдельный файл, а не дополнение test_bot.py: тесты не зависят от схемы
хранилища истории (_save_exchange подменяется), поэтому не сломаются при
дальнейшей эволюции MemoryCore.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import config


class _Dispatch:
    def __init__(self, text):
        self.is_allowed = True
        self.sanitized_text = text
        self.incidents = []


@pytest.fixture
def pipeline(monkeypatch):
    """process_logic с отключённым I/O: история пуста, RAG пуст, запись
    обмена подменена шпионом."""
    saved = MagicMock()
    monkeypatch.setattr(bot, "_save_exchange", saved)
    monkeypatch.setattr(bot, "_fetch_history", lambda *a, **k: [])
    monkeypatch.setattr(bot, "retrieve_context", AsyncMock(return_value=""))
    dispatcher = MagicMock()
    dispatcher.dispatch = MagicMock(return_value=_Dispatch("вопрос"))
    return saved, dispatcher


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestErrorMarkersNotPersisted:
    @pytest.mark.parametrize("marker", [
        "[ERR: LLM_UNAVAILABLE]",
        "[ERR: SYSTEM_DEGRADED]",
        "[ERR: KERNEL_FAULT]",
        "[ERR: RETRY_LIMIT_EXCEEDED]",
    ])
    def test_marker_returned_to_user_but_not_saved(self, pipeline, marker):
        saved, dispatcher = pipeline
        governor = MagicMock()
        governor.execute_request = AsyncMock(return_value=marker)

        result = _run(bot.process_logic("вопрос", 1, governor, dispatcher, MagicMock()))

        assert marker in result          # пользователь сбой видит
        saved.assert_not_called()        # а история остаётся чистой

    def test_normal_answer_still_saved(self, pipeline):
        saved, dispatcher = pipeline
        governor = MagicMock()
        governor.execute_request = AsyncMock(return_value="обычный ответ")

        result = _run(bot.process_logic("вопрос", 1, governor, dispatcher, MagicMock()))

        assert "обычный ответ" in result
        saved.assert_called_once()

    def test_answer_that_merely_mentions_err_is_saved(self, pipeline):
        """Только ответ, НАЧИНАЮЩИЙСЯ с [ERR:, считается маркером."""
        saved, dispatcher = pipeline
        governor = MagicMock()
        governor.execute_request = AsyncMock(return_value="Код ошибки выглядит как [ERR: X]")

        _run(bot.process_logic("вопрос", 1, governor, dispatcher, MagicMock()))

        saved.assert_called_once()

    def test_is_error_marker_helper(self):
        assert bot._is_error_marker("[ERR: X]")
        assert bot._is_error_marker("  [ERR: X]")
        assert not bot._is_error_marker("ответ [ERR: X]")
        assert not bot._is_error_marker("")
        assert not bot._is_error_marker(None)


class TestStatusShowsBreaker:
    @staticmethod
    def _governor(breaker_state):
        governor = MagicMock()
        governor.breaker_state = breaker_state
        governor.registry.get_status.return_value = {"state": {
            "status": "READY", "current_task": "IDLE", "last_error": "",
            "total_requests": 5, "compliance_violations": 1,
            "compliance_check_failures": 0, "last_compliance_status": "PASS",
            "input_blocked_count": 0,
            "last_alert_state_compliance": "NORMAL",
            "last_alert_state_input": "NORMAL",
        }}
        governor.monitor.window_size = 100
        governor.monitor.get_sample_size.return_value = 12
        governor.monitor.get_error_rate.return_value = 0.1
        governor.monitor.get_unchecked_rate.return_value = 0.0
        return governor

    @pytest.mark.parametrize("state", ["CLOSED", "OPEN", "HALF_OPEN"])
    def test_report_contains_breaker_state(self, monkeypatch, state):
        monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({111}))
        input_monitor = MagicMock()
        input_monitor.window_size = 100
        input_monitor.get_sample_size.return_value = 0
        input_monitor.get_error_rate.return_value = 0.0

        update = MagicMock()
        update.message.from_user.id = 111
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.application.bot_data = {
            "governor": self._governor(state),
            "input_monitor": input_monitor,
        }

        _run(bot.handle_status(update, context))

        report = update.message.reply_text.await_args.args[0]
        assert "CIRCUIT BREAKER" in report
        assert f"state: {state}" in report


class TestAlertLogLevelDoesNotFeedWatchdog:
    """process_watchdog считает строки ERROR/CRITICAL и при превышении
    лимита убивает процесс. Периодический алерт качества не должен
    попадать в этот счёт."""

    @staticmethod
    def _registry():
        class _R:
            def __init__(self):
                self.s = {}

            def get_state(self, k, d=None):
                return self.s.get(k, d)

            def update_state(self, k, v):
                self.s[k] = v
                return True
        return _R()

    @pytest.mark.parametrize("previous", ["NORMAL", "ALARM"])
    def test_alarm_cycle_logs_no_error_or_critical(self, monkeypatch, previous):
        fake_logger = MagicMock()
        monkeypatch.setattr(bot, "logger", fake_logger)
        monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({111}))
        registry = self._registry()
        registry.s["last_alert_state_compliance"] = previous
        monitor = MagicMock()
        monitor.get_sample_size.return_value = 50
        monitor.get_error_rate.return_value = 0.9
        context = MagicMock()
        context.bot.send_message = AsyncMock()

        _run(bot._check_and_alert(
            context, monitor, "last_alert_state_compliance", registry, "Test"))

        fake_logger.critical.assert_not_called()
        fake_logger.error.assert_not_called()
        fake_logger.warning.assert_called()


class TestTokenDoesNotLeakIntoLogs:
    """URL Telegram Bot API содержит токен; httpx логирует URL на уровне INFO."""

    @pytest.mark.parametrize("name", ["httpx", "httpcore"])
    def test_http_client_loggers_are_not_verbose(self, name):
        import logging
        import bot  # noqa: F401
        assert logging.getLogger(name).level >= logging.WARNING

