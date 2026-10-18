"""Тесты для bot.py — интерфейсный I/O-слой.

Два яруса по стоимости мокинга:
1. ДЁШЕВО, без Telegram-объектов: SystemZero (чистая regex-логика),
   _query_knowledge_db (реальный SQLite), process_logic (принимает
   governor/dispatcher/input_monitor напрямую, не Update/Context),
   _check_and_alert (мок monitor/registry + мок context.bot).
2. ДОРОЖЕ, с мок-объектами Update/CallbackQuery/Context (без реального
   python-telegram-bot транспорта): handle_message, handle_status,
   handle_staging_callback, staging_monitor_job.

ВАЖНО про замороженные модульные константы config.py — тот же класс
проблемы, что уже был найден для config.BASE_DIR/config.DB_PATH в
predыдущих тестах (registry.py, test_db.py): config.ADMIN_USER_IDS
вычисляется ОДИН РАЗ при первом импорте config.py из переменной
окружения. monkeypatch.setenv("ADMIN_USER_IDS", ...) НЕ пересчитывает
её — нужно патчить атрибут напрямую: monkeypatch.setattr(config,
"ADMIN_USER_IDS", frozenset({...})). См. фикстуру admin_ids ниже.

Аналогично config.DB_PATH патчится напрямую (см. isolated_db), тот же
паттерн, что в test_db.py.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import config
import db


# ------------------------------------------------------------------
# Общие фикстуры для этого файла
# ------------------------------------------------------------------

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Изолированная SQLite БД — та же техника, что в test_db.py:
    monkeypatch самого атрибута config.DB_PATH, не переменной окружения,
    т.к. db.py читает config.DB_PATH напрямую при каждом вызове
    get_connection() (не кэширует значение), поэтому патч атрибута
    достаточен и не требует пересборки config.

    ВАЖНО (после унификации Split Brain в retrieve_context, см. bot.py):
    retrieve_context() при bot.RAG_RETRIEVAL_AVAILABLE == True вызывает
    retriever.build_context() -> реальный requests.post к Ollama +
    реальное чтение oko_rag/chroma_db, а не изолированную tmp_path.
    Патч config.DB_PATH этот путь не затрагивает вообще. Без явного
    отключения здесь process_logic-тесты ниже (test_happy_path_*,
    test_governor_exception_*), не мокающие retrieve_context напрямую,
    делали бы реальный сетевой вызов на каждый прогон — медленно, флейково,
    и не изолированно от боевых данных в chroma_db. Форсируем фолбэк на
    _query_knowledge_db (который уже читает из изолированной SQLite выше)
    для всех тестов этого файла, как было до унификации.
    """
    db_path = tmp_path / "test_bot.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(bot, "RAG_RETRIEVAL_AVAILABLE", False)
    # R1: таблица sessions удалена; тестам нужен только существующий файл БД.
    import sqlite3
    sqlite3.connect(db_path).close()
    yield db_path


@pytest.fixture
def admin_ids(monkeypatch):
    """Патчит config.ADMIN_USER_IDS напрямую — см. docstring модуля."""
    ids = frozenset({111})
    monkeypatch.setattr(config, "ADMIN_USER_IDS", ids)
    return ids


class _FakeDispatchResult:
    def __init__(self, is_allowed, sanitized_text, incidents=None):
        self.is_allowed = is_allowed
        self.sanitized_text = sanitized_text
        self.incidents = incidents or []


class _FakeIncident:
    def __init__(self, level, category):
        self.level = level
        self.category = category


# ------------------------------------------------------------------
# Ярус 1: SystemZero — чистая regex-логика, без I/O
# ------------------------------------------------------------------

class TestSystemZero:
    def test_removes_boilerplate_phrase(self):
        sz = bot.SystemZero()
        result = sz.clean_output("Извините, но пожалуйста поймите — рад помочь!")
        assert "Извините" not in result
        assert "пожалуйста" not in result
        assert "рад помочь" not in result

    def test_greedy_pattern_wipes_rest_of_line(self):
        """ВАЖНО, задокументированное поведение, не баг для исправления:
        паттерн r'(?i)As an AI language model.*' жадный и без re.DOTALL
        захватывает весь ОСТАТОК СТРОКИ после срабатывания — если полезный
        текст идёт на той же строке после триггерной фразы, он тоже
        стирается целиком. Тест фиксирует это как есть."""
        sz = bot.SystemZero()
        result = sz.clean_output("As an AI language model, I cannot help. Вот ответ.")
        assert result == ""

    def test_collapses_multiple_blank_lines(self):
        sz = bot.SystemZero()
        result = sz.clean_output("Строка1\n\n\n\nСтрока2")
        assert "\n\n" not in result
        assert "Строка1" in result and "Строка2" in result

    def test_collapses_multiple_spaces(self):
        sz = bot.SystemZero()
        result = sz.clean_output("много   пробелов")
        assert "   " not in result
        assert result == "много пробелов"

    def test_empty_string_returns_error_marker(self):
        sz = bot.SystemZero()
        assert sz.clean_output("") == "[ERR: EMPTY]"

    def test_none_returns_error_marker(self):
        sz = bot.SystemZero()
        assert sz.clean_output(None) == "[ERR: EMPTY]"

    def test_format_final_wraps_with_name(self):
        sz = bot.SystemZero()
        assert sz.format_final("текст ответа") == "[Око]: текст ответа"


# ------------------------------------------------------------------
# Ярус 1: _query_knowledge_db — реальный SQLite
# ------------------------------------------------------------------

class TestQueryKnowledgeDb:
    def test_missing_db_file_returns_empty_message(self, isolated_db, tmp_path, monkeypatch):
        # isolated_db уже создал файл в фикстуре isolated_db — для
        # ЭТОГО теста нужен путь, который физически не существует.
        monkeypatch.setattr(config, "DB_PATH", tmp_path / "does_not_exist.db")
        result = bot._query_knowledge_db("тест")
        assert "пуста" in result

    def test_query_too_short_rejected(self, isolated_db):
        result = bot._query_knowledge_db("ok")  # все слова <= 2 символов
        assert "короткий" in result

    def test_cyrillic_case_insensitive_match(self, isolated_db):
        """Регрессия на задокументированный в docstring bot.py баг:
        SQLite LOWER() не работает с кириллицей, регистронезависимое
        сравнение сделано в Python после выборки."""
        conn = db.get_connection()
        conn.execute(
            "CREATE TABLE knowledge (id INTEGER PRIMARY KEY, content TEXT, "
            "content_hash TEXT, source TEXT, category TEXT, "
            "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO knowledge (content, content_hash, source, category) "
            "VALUES (?, ?, ?, ?)",
            ("Информация про Пульс и аномалии", "h1", "file", "general"),
        )
        conn.commit()
        conn.close()

        result = bot._query_knowledge_db("ПУЛЬС")
        assert "Информация про Пульс" in result

    def test_no_match_found(self, isolated_db):
        conn = db.get_connection()
        conn.execute(
            "CREATE TABLE knowledge (id INTEGER PRIMARY KEY, content TEXT, "
            "content_hash TEXT, source TEXT, category TEXT, "
            "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO knowledge (content, content_hash, source, category) "
            "VALUES (?, ?, ?, ?)",
            ("Другой текст без совпадений", "h2", "file", "general"),
        )
        conn.commit()
        conn.close()

        result = bot._query_knowledge_db("совершенно_несуществующее_xyz")
        assert "не найден" in result

    def test_missing_knowledge_table_handled_gracefully(self, isolated_db):
        """sessions создана, knowledge — нет. sqlite3.Error должен быть
        пойман, не пробрасываться наружу."""
        result = bot._query_knowledge_db("любой запрос")
        assert "недоступна" in result


# ------------------------------------------------------------------
# Ярус 1: process_logic — принимает governor/dispatcher напрямую
# ------------------------------------------------------------------

class TestProcessLogic:
    def test_blocked_input_returns_marker_and_increments_counters(self, isolated_db):
        import asyncio
        dispatcher = MagicMock()
        dispatcher.dispatch = MagicMock(return_value=_FakeDispatchResult(
            is_allowed=False, sanitized_text="",
            incidents=[_FakeIncident("CRITICAL", "API_KEY")],
        ))
        governor = MagicMock()
        input_monitor = MagicMock()

        result = asyncio.run(bot.process_logic(
            "secret key here", 111, governor, dispatcher, input_monitor
        ))

        assert result == "[BLOCKED: API_KEY]"
        governor.registry.increment_state.assert_called_once_with("input_blocked_count")
        input_monitor.record_event.assert_called_once()

    def test_happy_path_calls_governor_and_persists_exchange(self, isolated_db):
        import asyncio
        dispatcher = MagicMock()
        dispatcher.dispatch = MagicMock(
            return_value=_FakeDispatchResult(is_allowed=True, sanitized_text="привет")
        )
        governor = MagicMock()
        governor.execute_request = AsyncMock(return_value="ответ ядра")
        input_monitor = MagicMock()

        result = asyncio.run(bot.process_logic(
            "привет", 222, governor, dispatcher, input_monitor
        ))

        assert result == "ответ ядра"
        governor.execute_request.assert_awaited_once()
        call_kwargs = governor.execute_request.call_args.kwargs
        assert call_kwargs["user_input"] == "привет"
        assert call_kwargs["user_id"] == 222

        history = bot._fetch_history(222, 10)
        assert len(history) == 2
        assert history[0] == ("user", "привет")
        assert history[1] == ("assistant", "ответ ядра")

    def test_governor_exception_returns_none_not_raised(self, isolated_db):
        import asyncio
        dispatcher = MagicMock()
        dispatcher.dispatch = MagicMock(
            return_value=_FakeDispatchResult(is_allowed=True, sanitized_text="вопрос")
        )
        governor = MagicMock()
        governor.execute_request = AsyncMock(side_effect=RuntimeError("kernel exploded"))
        input_monitor = MagicMock()

        result = asyncio.run(bot.process_logic(
            "вопрос", 333, governor, dispatcher, input_monitor
        ))

        assert result is None


# ------------------------------------------------------------------
# Ярус 1: _check_and_alert — гистерезис алертинга
# ------------------------------------------------------------------

class _FakeRegistry:
    def __init__(self):
        self._state = {}

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def update_state(self, key, value):
        self._state[key] = value
        return True


def _make_alert_context():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


class TestCheckAndAlert:
    def test_insufficient_sample_size_is_noop(self, admin_ids):
        import asyncio
        monitor = MagicMock()
        monitor.get_sample_size = MagicMock(return_value=config.ALERT_MIN_SAMPLE_SIZE - 1)
        registry = _FakeRegistry()
        ctx = _make_alert_context()

        asyncio.run(bot._check_and_alert(ctx, monitor, "last_alert_state_compliance", registry, "Test"))

        ctx.bot.send_message.assert_not_called()
        assert registry.get_state("last_alert_state_compliance", "NORMAL") == "NORMAL"

    def test_high_error_rate_normal_to_alarm_sends_alert(self, admin_ids):
        import asyncio
        monitor = MagicMock()
        monitor.get_sample_size = MagicMock(return_value=20)
        monitor.get_error_rate = MagicMock(return_value=0.5)
        registry = _FakeRegistry()
        ctx = _make_alert_context()

        asyncio.run(bot._check_and_alert(ctx, monitor, "last_alert_state_compliance", registry, "Test"))

        assert registry.get_state("last_alert_state_compliance") == "ALARM"
        ctx.bot.send_message.assert_awaited()

    def test_high_error_rate_stays_alarm_no_repeat_alert(self, admin_ids):
        """Защита от спама: пока error_rate остаётся высоким, алерт не
        повторяется на каждый цикл опроса."""
        import asyncio
        monitor = MagicMock()
        monitor.get_sample_size = MagicMock(return_value=20)
        monitor.get_error_rate = MagicMock(return_value=0.5)
        registry = _FakeRegistry()
        registry.update_state("last_alert_state_compliance", "ALARM")
        ctx = _make_alert_context()

        asyncio.run(bot._check_and_alert(ctx, monitor, "last_alert_state_compliance", registry, "Test"))

        ctx.bot.send_message.assert_not_called()
        assert registry.get_state("last_alert_state_compliance") == "ALARM"

    def test_recovery_alarm_to_normal_sends_recovery_alert(self, admin_ids):
        import asyncio
        monitor = MagicMock()
        monitor.get_sample_size = MagicMock(return_value=20)
        monitor.get_error_rate = MagicMock(return_value=0.05)
        registry = _FakeRegistry()
        registry.update_state("last_alert_state_compliance", "ALARM")
        ctx = _make_alert_context()

        asyncio.run(bot._check_and_alert(ctx, monitor, "last_alert_state_compliance", registry, "Test"))

        assert registry.get_state("last_alert_state_compliance") == "NORMAL"
        ctx.bot.send_message.assert_awaited()

    def test_normal_stays_normal_silent(self, admin_ids):
        import asyncio
        monitor = MagicMock()
        monitor.get_sample_size = MagicMock(return_value=20)
        monitor.get_error_rate = MagicMock(return_value=0.05)
        registry = _FakeRegistry()
        ctx = _make_alert_context()

        asyncio.run(bot._check_and_alert(ctx, monitor, "last_alert_state_compliance", registry, "Test"))

        ctx.bot.send_message.assert_not_called()


# ------------------------------------------------------------------
# Ярус 2: handle_message — мок Update/Context
# ------------------------------------------------------------------

def _make_update(text="привет", user_id=555, has_message=True, has_text=True):
    update = MagicMock()
    if not has_message:
        update.message = None
        return update
    update.message = MagicMock()
    update.message.text = text if has_text else None
    update.message.from_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


def _make_message_context(governor=None, dispatcher=None, input_monitor=None):
    context = MagicMock()
    context.application.bot_data = {
        "governor": governor or MagicMock(),
        "dispatcher": dispatcher or MagicMock(),
        "input_monitor": input_monitor or MagicMock(),
    }
    return context


class TestHandleMessage:
    def test_no_message_silent_return(self):
        import asyncio
        update = _make_update(has_message=False)
        context = _make_message_context()
        asyncio.run(bot.handle_message(update, context))  # не должно бросить

    def test_no_text_silent_return(self):
        import asyncio
        update = _make_update(has_text=False)
        context = _make_message_context()
        asyncio.run(bot.handle_message(update, context))
        update.message.reply_text.assert_not_called()

    def test_happy_path_sends_cleaned_formatted_response(self, isolated_db, admin_ids):
        import asyncio
        dispatcher = MagicMock()
        dispatcher.dispatch = MagicMock(
            return_value=_FakeDispatchResult(is_allowed=True, sanitized_text="привет мир")
        )
        governor = MagicMock()
        governor.execute_request = AsyncMock(return_value="Привет! Извините, рад помочь.")
        update = _make_update(text="привет мир", user_id=111)
        context = _make_message_context(governor=governor, dispatcher=dispatcher)

        asyncio.run(bot.handle_message(update, context))

        update.message.reply_text.assert_awaited_once()
        sent = update.message.reply_text.await_args.args[0]
        assert sent.startswith("[Око]:")
        assert "Извините" not in sent

    def test_process_logic_none_sends_system_fault(self, isolated_db, admin_ids):
        import asyncio
        dispatcher = MagicMock()
        dispatcher.dispatch = MagicMock(
            return_value=_FakeDispatchResult(is_allowed=True, sanitized_text="упаду")
        )
        governor = MagicMock()
        governor.execute_request = AsyncMock(side_effect=RuntimeError("boom"))
        update = _make_update(text="упаду", user_id=111)
        context = _make_message_context(governor=governor, dispatcher=dispatcher)

        asyncio.run(bot.handle_message(update, context))

        update.message.reply_text.assert_awaited_once_with("[ERR: SYSTEM_FAULT]")

    def test_reply_text_failure_does_not_propagate(self, isolated_db, admin_ids):
        import asyncio
        dispatcher = MagicMock()
        dispatcher.dispatch = MagicMock(
            return_value=_FakeDispatchResult(is_allowed=True, sanitized_text="test")
        )
        governor = MagicMock()
        governor.execute_request = AsyncMock(return_value="ok")
        update = _make_update(text="test", user_id=111)
        update.message.reply_text = AsyncMock(side_effect=Exception("telegram down"))
        context = _make_message_context(governor=governor, dispatcher=dispatcher)

        asyncio.run(bot.handle_message(update, context))  # не должно бросить


# ------------------------------------------------------------------
# Ярус 2: handle_status — админ-гейтинг
# ------------------------------------------------------------------

def _make_status_update(user_id, has_message=True, has_from_user=True):
    update = MagicMock()
    if not has_message:
        update.message = None
        return update
    update.message = MagicMock()
    if not has_from_user:
        update.message.from_user = None
    else:
        update.message.from_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


def _make_status_governor():
    governor = MagicMock()
    governor.registry.get_status = MagicMock(return_value={"state": {
        "status": "READY", "current_task": "IDLE", "last_error": None,
        "total_requests": 5, "compliance_violations": 0,
        "compliance_check_failures": 0, "last_compliance_status": "PASS",
        "input_blocked_count": 0, "last_alert_state_compliance": "NORMAL",
        "last_alert_state_input": "NORMAL",
    }})
    governor.monitor.window_size = 100
    governor.monitor.get_sample_size = MagicMock(return_value=5)
    governor.monitor.get_error_rate = MagicMock(return_value=0.0)
    governor.monitor.get_unchecked_rate = MagicMock(return_value=0.0)
    return governor


def _make_status_context(governor):
    context = MagicMock()
    context.application.bot_data = {
        "governor": governor,
        "input_monitor": MagicMock(
            get_sample_size=MagicMock(return_value=0),
            get_error_rate=MagicMock(return_value=0.0),
            window_size=100,
        ),
    }
    return context


class TestHandleStatus:
    def test_non_admin_silent_fail_closed(self, admin_ids):
        import asyncio
        governor = _make_status_governor()
        update = _make_status_update(user_id=999)  # не в admin_ids
        context = _make_status_context(governor)

        asyncio.run(bot.handle_status(update, context))

        update.message.reply_text.assert_not_called()

    def test_no_message_early_return(self, admin_ids):
        import asyncio
        governor = _make_status_governor()
        update = _make_status_update(user_id=111, has_message=False)
        context = _make_status_context(governor)
        asyncio.run(bot.handle_status(update, context))  # не должно бросить

    def test_admin_receives_full_report(self, admin_ids):
        import asyncio
        governor = _make_status_governor()
        update = _make_status_update(user_id=111)  # в admin_ids
        context = _make_status_context(governor)

        asyncio.run(bot.handle_status(update, context))

        update.message.reply_text.assert_awaited_once()
        report = update.message.reply_text.await_args.args[0]
        assert "KERNEL STATUS" in report
        assert "status: READY" in report
        assert "total_requests: 5" in report


# ------------------------------------------------------------------
# Ярус 2: handle_staging_callback — approve/reject, маппинг ошибок
# ------------------------------------------------------------------

def _make_callback_update(user_id, data, has_query=True, has_from_user=True):
    update = MagicMock()
    if not has_query:
        update.callback_query = None
        return update
    query = MagicMock()
    if not has_from_user:
        query.from_user = None
    else:
        query.from_user.id = user_id
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    return update


def _make_callback_context(staging_manager=None, governor=None):
    context = MagicMock()
    context.application.bot_data = {
        "governor": governor or MagicMock(),
        "staging_manager": staging_manager or MagicMock(),
    }
    return context


class TestHandleStagingCallback:
    def test_unauthorized_user_rejected(self, admin_ids):
        import asyncio
        bot._notified_uuids.clear()
        update = _make_callback_update(999, "approve_abc")
        context = _make_callback_context()

        asyncio.run(bot.handle_staging_callback(update, context))

        update.callback_query.answer.assert_awaited_once_with("Not authorized.", show_alert=True)

    def test_unknown_action_prefix(self, admin_ids):
        import asyncio
        bot._notified_uuids.clear()
        update = _make_callback_update(111, "unknown_xyz")
        context = _make_callback_context()

        asyncio.run(bot.handle_staging_callback(update, context))

        update.callback_query.answer.assert_awaited_once_with("Unknown action.")

    def test_successful_approve_indexes_and_clears_cache(self, admin_ids, isolated_db, monkeypatch):
        import asyncio
        bot._notified_uuids.clear()
        bot._notified_uuids.add("uuid-3")

        staging_manager = MagicMock()
        staging_manager.approve = MagicMock(return_value=Path("/fake/staging/doc.txt"))
        monkeypatch.setattr(bot.knowledge_indexer, "process_single", MagicMock(return_value="processed"))

        update = _make_callback_update(111, "approve_uuid-3")
        context = _make_callback_context(staging_manager=staging_manager)

        asyncio.run(bot.handle_staging_callback(update, context))

        staging_manager.approve.assert_called_once_with("uuid-3")
        msg = update.callback_query.edit_message_text.await_args.args[0]
        assert "Approved" in msg
        assert "uuid-3" not in bot._notified_uuids

    def test_successful_reject_clears_cache(self, admin_ids):
        import asyncio
        bot._notified_uuids.clear()
        bot._notified_uuids.add("uuid-4")

        staging_manager = MagicMock()
        staging_manager.reject = MagicMock()
        update = _make_callback_update(111, "reject_uuid-4")
        context = _make_callback_context(staging_manager=staging_manager)

        asyncio.run(bot.handle_staging_callback(update, context))

        staging_manager.reject.assert_called_once_with("uuid-4")
        msg = update.callback_query.edit_message_text.await_args.args[0]
        assert "Rejected" in msg
        assert "uuid-4" not in bot._notified_uuids

    def test_key_error_task_not_found(self, admin_ids):
        import asyncio
        bot._notified_uuids.clear()
        staging_manager = MagicMock()
        staging_manager.approve = MagicMock(side_effect=KeyError("uuid-5"))
        update = _make_callback_update(111, "approve_uuid-5")
        context = _make_callback_context(staging_manager=staging_manager)

        asyncio.run(bot.handle_staging_callback(update, context))

        msg = update.callback_query.edit_message_text.await_args.args[0]
        assert "not found" in msg

    def test_value_error_hash_mismatch_security_message(self, admin_ids):
        import asyncio
        bot._notified_uuids.clear()
        staging_manager = MagicMock()
        staging_manager.approve = MagicMock(side_effect=ValueError("Hash mismatch for doc.txt"))
        update = _make_callback_update(111, "approve_uuid-6")
        context = _make_callback_context(staging_manager=staging_manager)

        asyncio.run(bot.handle_staging_callback(update, context))

        msg = update.callback_query.edit_message_text.await_args.args[0]
        assert "SECURITY" in msg

    def test_generic_exception_still_answers_callback(self, admin_ids):
        """finally-блок обязан вызвать query.answer() даже при
        непредвиденном исключении — иначе Telegram будет показывать
        "часики" на кнопке у пользователя бесконечно."""
        import asyncio
        bot._notified_uuids.clear()
        staging_manager = MagicMock()
        staging_manager.reject = MagicMock(side_effect=RuntimeError("disk full"))
        update = _make_callback_update(111, "reject_uuid-7")
        context = _make_callback_context(staging_manager=staging_manager)

        asyncio.run(bot.handle_staging_callback(update, context))

        msg = update.callback_query.edit_message_text.await_args.args[0]
        assert "Error processing" in msg
        update.callback_query.answer.assert_awaited()


# ------------------------------------------------------------------
# Ярус 2: staging_monitor_job — дедупликация уведомлений
# ------------------------------------------------------------------

def _make_job_context(pending_tasks):
    """Мок под РЕАЛЬНЫЙ контракт staging_monitor_job (bot.py:751-752):
    context.application.bot_data["staging_manager"].repo.get_all_tasks().

    Намеренно НЕ мокает несколько альтернативных имён метода "на всякий
    случай" — если производственный код вызовет другой метод, тест должен
    упасть с понятной ошибкой (AttributeError на несуществующем атрибуте
    MagicMock либо пустой результат), а не тихо пройти благодаря
    угадывающему мок-каскаду. Мок, отвечающий на любое имя вызова,
    перестаёт быть тестом контракта — он не заметит регресс, если кто-то
    переименует метод в staging_manager.py.
    """
    staging_manager = MagicMock()
    staging_manager.repo.get_all_tasks = MagicMock(return_value=pending_tasks)

    context = MagicMock()
    context.application.bot_data = {"staging_manager": staging_manager}
    context.bot.send_message = AsyncMock()
    return context


class TestStagingMonitorJob:
    def test_new_task_triggers_notification_and_caching(self, admin_ids):
        import asyncio
        bot._notified_uuids.clear()
        pending = {"uuid-a": {"original_path": "/data/knowledge/report.txt"}}
        context = _make_job_context(pending)

        asyncio.run(bot.staging_monitor_job(context))

        assert "uuid-a" in bot._notified_uuids
        context.bot.send_message.assert_awaited()

    def test_already_notified_task_not_repeated(self, admin_ids):
        import asyncio
        bot._notified_uuids.clear()
        bot._notified_uuids.add("uuid-a")
        pending = {"uuid-a": {"original_path": "/data/knowledge/report.txt"}}
        context = _make_job_context(pending)

        asyncio.run(bot.staging_monitor_job(context))

        context.bot.send_message.assert_not_called()

    def test_stale_uuid_removed_from_cache(self, admin_ids):
        import asyncio
        bot._notified_uuids.clear()
        bot._notified_uuids.add("uuid-a")
        context = _make_job_context({})  # pending_tasks теперь пуст

        asyncio.run(bot.staging_monitor_job(context))

        assert "uuid-a" not in bot._notified_uuids
        context.bot.send_message.assert_not_called()


# ------------------------------------------------------------------
# ADMIN_ALLOWLIST_CHAT: обычные сообщения -- только от ADMIN_USER_IDS
# ------------------------------------------------------------------

class _BotLogCapture:
    def __init__(self):
        import logging

        class _H(logging.Handler):
            def __init__(self, sink):
                super().__init__(logging.DEBUG)
                self.sink = sink

            def emit(self, record):
                self.sink.append(record)

        self.records = []
        self._handler = _H(self.records)

    def __enter__(self):
        import logging
        self._old = bot.logger.level
        bot.logger.addHandler(self._handler)
        bot.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        bot.logger.removeHandler(self._handler)
        bot.logger.setLevel(self._old)


def _spy_pipeline(monkeypatch):
    """Шпионы на всё, до чего посторонний не должен дотянуться."""
    calls = {"history": [], "save": [], "rag": []}
    monkeypatch.setattr(bot, "_fetch_history", lambda *a: calls["history"].append(a) or [])
    monkeypatch.setattr(bot, "_save_exchange", lambda *a: calls["save"].append(a))

    async def _rag(q):
        calls["rag"].append(q)
        return "секрет из базы знаний"

    monkeypatch.setattr(bot, "retrieve_context", _rag)
    governor = MagicMock()
    governor.execute_request = AsyncMock(return_value="ответ с секретом")
    governor.execute_discuss_request = AsyncMock(return_value="ответ по документу")
    dispatcher = MagicMock()
    dispatcher.dispatch = MagicMock(
        return_value=_FakeDispatchResult(is_allowed=True, sanitized_text="что в базе")
    )
    input_monitor = MagicMock()
    return calls, governor, dispatcher, input_monitor


class TestHandleMessageAllowlist:
    def test_non_admin_gets_nothing_and_touches_nothing(self, isolated_db, admin_ids, monkeypatch):
        import asyncio
        calls, governor, dispatcher, input_monitor = _spy_pipeline(monkeypatch)
        update = _make_update(text="что у тебя в базе знаний?", user_id=999)
        context = _make_message_context(governor, dispatcher, input_monitor)

        asyncio.run(bot.handle_message(update, context))

        update.message.reply_text.assert_not_called()
        governor.execute_request.assert_not_called()
        dispatcher.dispatch.assert_not_called()
        input_monitor.record_event.assert_not_called()
        governor.registry.increment_state.assert_not_called()
        assert calls == {"history": [], "save": [], "rag": []}

    def test_non_admin_in_discuss_session_is_not_served(self, isolated_db, admin_ids, monkeypatch):
        import asyncio
        calls, governor, dispatcher, input_monitor = _spy_pipeline(monkeypatch)
        monkeypatch.setitem(bot.discussion_sessions, 999, "doc.pdf")
        update = _make_update(text="процитируй документ", user_id=999)
        context = _make_message_context(governor, dispatcher, input_monitor)

        asyncio.run(bot.handle_message(update, context))

        update.message.reply_text.assert_not_called()
        governor.execute_discuss_request.assert_not_called()

    def test_empty_allowlist_ignores_everyone(self, isolated_db, monkeypatch):
        import asyncio
        monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset())
        calls, governor, dispatcher, input_monitor = _spy_pipeline(monkeypatch)
        update = _make_update(text="привет", user_id=111)
        context = _make_message_context(governor, dispatcher, input_monitor)

        asyncio.run(bot.handle_message(update, context))

        update.message.reply_text.assert_not_called()
        governor.execute_request.assert_not_called()

    def test_unauthorized_attempt_logged_as_warning_without_text(self, isolated_db, admin_ids, monkeypatch):
        import asyncio
        import logging
        calls, governor, dispatcher, input_monitor = _spy_pipeline(monkeypatch)
        update = _make_update(text="мой секретный вопрос", user_id=999)
        context = _make_message_context(governor, dispatcher, input_monitor)

        with _BotLogCapture() as cap:
            asyncio.run(bot.handle_message(update, context))

        hits = [r for r in cap.records if "Unauthorized message ignored" in r.getMessage()]
        assert len(hits) == 1
        assert hits[0].levelno == logging.WARNING
        assert "999" in hits[0].getMessage()
        assert all("секретный" not in r.getMessage() for r in cap.records)

    def test_admin_is_still_served(self, isolated_db, admin_ids, monkeypatch):
        import asyncio
        calls, governor, dispatcher, input_monitor = _spy_pipeline(monkeypatch)
        update = _make_update(text="что в базе", user_id=111)
        context = _make_message_context(governor, dispatcher, input_monitor)

        asyncio.run(bot.handle_message(update, context))

        governor.execute_request.assert_awaited_once()
        update.message.reply_text.assert_awaited_once()


# ------------------------------------------------------------------
# GUARDRAILS_REMOVED: мёртвый слой guardrails больше не подключается
# ------------------------------------------------------------------

class TestGuardrailsRemoved:
    @staticmethod
    def _src():
        return Path(bot.__file__).read_text(encoding="utf-8-sig")

    def test_bot_does_not_use_guardrails(self):
        src = self._src()
        assert "initialize_guardrail(" not in src
        assert "from src.guardrails" not in src
        assert "guardrail_dispatcher" not in src

    def test_input_filter_is_data_hygiene(self):
        assert 'app.bot_data["dispatcher"] = DataHygieneFilter()' in self._src()


# ------------------------------------------------------------------
# DEGRADED_TEXT_FIX: пояснение к SYSTEM_DEGRADED соответствует поведению
# (при открытом предохранителе запрос к модели не отправляется вовсе)
# ------------------------------------------------------------------

class TestSystemDegradedText:
    def test_explains_that_requests_are_not_sent(self):
        text = bot._ERR_TEXT["SYSTEM_DEGRADED"]
        assert "не отправляются модели" in text
        assert "проверка ответов временно отключена" not in text

    def test_marker_gets_new_explanation(self):
        out = bot._explain_error("[ERR: SYSTEM_DEGRADED]")
        assert out.startswith("[ERR: SYSTEM_DEGRADED] Предохранитель открыт")
