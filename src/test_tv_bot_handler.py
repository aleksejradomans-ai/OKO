"""Тесты связки телевизора с ботом. Сеть не затрагивается ни в одном тесте.

Клиент подменяется счётчиком вызовов: часть проверок состоит именно в том,
что соединение НЕ создаётся (отказ по правам, неразобранный текст, запрос
подтверждения). Если фабрику дёрнули — тест падает.
"""

from __future__ import annotations

import logging
import re

import pytest

import tv_bot_handler as H
from tv_commands import BLIND, VERIFIED, Command
from tv_control import (
    TVAuthError,
    TVNotPaired,
    TVTransportError,
    TVUnsupported,
)

ADMIN = 100
GUEST = 200


class FakeTV:
    """Минимальный двойник: пишет вызовы, ничего не отправляет."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        def recorder(*args, **kwargs):
            self.calls.append((name, args))
            return self._answer(name)
        return recorder

    def _answer(self, name: str):
        if name == "set_volume":
            return 20          # контракт настоящего клиента: факт, не None
        if name == "volume_step":
            return 13
        if name == "get_volume":
            return {"current": 10, "max": 60, "muted": False}
        if name == "get_ambilight_power":
            return {"power": "On"}
        if name == "get_ambilight_config":
            return {"styleName": "FOLLOW_COLOR", "menuSetting": "MANUAL_HUE"}
        if name == "get_lounge_color":
            return {"hue": 0, "saturation": 255, "brightness": 255}
        if name == "get_current_activity":
            return {"component": {"packageName": "org.droidtv.playtv"}}
        return {}


class Factory:
    """Фабрика с подсчётом: нужна, чтобы доказать отсутствие соединения."""

    def __init__(self, tv=None, error: Exception | None = None) -> None:
        self.tv = tv if tv is not None else FakeTV()
        self.error = error
        self.count = 0

    def __call__(self):
        self.count += 1
        if self.error is not None:
            raise self.error
        return self.tv


@pytest.fixture()
def store():
    return H.PendingStore()


def call(user_id, text, factory, store, now=0.0):
    return H.handle(user_id, text, factory, pending=store,
                    admin_ids=[ADMIN], now=now)


# --- права ------------------------------------------------------------------

def test_guest_refused_and_no_connection(store):
    f = Factory()
    out = call(GUEST, "статус", f, store)
    assert "только администратору" in out
    assert f.count == 0


def test_empty_admin_set_closes_access(store):
    f = Factory()
    out = H.handle(ADMIN, "статус", f, pending=store, admin_ids=[], now=0.0)
    assert "только администратору" in out
    assert f.count == 0


def test_missing_config_closes_access(monkeypatch):
    """Отсутствие config не должно открывать управление всем."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "config":
            raise ImportError("нет модуля")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert H._default_admin_ids() == frozenset()


# --- подсказка --------------------------------------------------------------

def test_empty_payload_gives_help(store):
    f = Factory()
    out = call(ADMIN, "/tv", f, store)
    assert "Подсветка:" in out and "Кнопки:" in out
    assert f.count == 0


def test_help_word(store):
    assert "Состояние:" in call(ADMIN, "помощь", Factory(), store)


# --- разбор -----------------------------------------------------------------

def test_unknown_returns_reason_without_connection(store):
    f = Factory()
    out = call(ADMIN, "свари кофе", f, store)
    assert "Не выполнено" in out
    assert f.count == 0


def test_volume_command_runs(store):
    """Громкость включена 27.10 в 23:13 после живого замера Оператора.

    До этого здесь стоял тест отказа: звук шёл по оптике, запись проходила
    с кодом 200, а на слух не менялось ничего. Оператор переключил вывод
    на динамики телевизора и проверил — громкость меняется.
    """
    f = Factory()
    out = call(ADMIN, "сделай громче на 10", f, store)
    assert ("volume_step", (10,)) in f.tv.calls
    assert "Громкость" in out


def test_volume_answer_is_not_a_promise(store):
    """Ответ не обещает слышимого результата.

    Слышимость зависит от того, воспроизводит ли звук сам телевизор, и по
    API это не определяется.
    """
    out = call(ADMIN, "громче", Factory(), store)
    for lie in ("слышно", "вы услышите", "стало громче"):
        assert lie not in out.lower()


def test_power_on_sends_wake_and_claims_nothing(store):
    """03.11: «включи телевизор» -- пакет пробуждения (Wake-on-LAN); ответ -- об отправке, с пометкой слепой команды."""
    f = Factory()
    out = call(ADMIN, "включи телевизор", f, store)
    assert out.startswith("Отправил") and "не подтверждает" in out
    assert not store.pending_for(ADMIN)               # без подтверждения: включение безопасно


# --- выполнение -------------------------------------------------------------

def test_verified_command_has_no_blind_note(store):
    f = Factory()
    out = call(ADMIN, "статус", f, store)
    assert f.count == 1
    assert H.PREFIX_NOTE_BLIND not in out


def test_blind_command_states_no_confirmation(store):
    f = Factory()
    out = call(ADMIN, "вверх", f, store)
    assert ("send_key", ("CursorUp",)) in f.tv.calls
    assert H.PREFIX_NOTE_BLIND in out


def test_blind_note_does_not_claim_effect():
    """Текст пометки не должен утверждать результат.

    29.10 (Оператор: «понятные значения выводов»): упоминание кода 200 убрано из ответа человеку —
    проверяется суть (подтверждения нет), а не техническое слово.
    """
    assert "не подтверждает" in H.PREFIX_NOTE_BLIND
    for lie in ("выключен", "включен", "выполнено", "успешно"):
        assert lie not in H.PREFIX_NOTE_BLIND.lower()


# --- подтверждение ----------------------------------------------------------

def test_standby_asks_first_and_sends_nothing(store):
    f = Factory()
    out = call(ADMIN, "выключи телевизор", f, store)
    assert "подтвержд" in out.lower()
    assert f.count == 0
    assert store.pending_for(ADMIN)


def test_standby_executes_after_confirmation(store):
    f = Factory()
    call(ADMIN, "выключи телевизор", f, store)
    out = call(ADMIN, "да", f, store, now=1.0)
    assert ("send_key", ("Standby",)) in f.tv.calls
    assert H.PREFIX_NOTE_BLIND in out
    assert not store.pending_for(ADMIN)


def test_hint_names_a_word_that_is_accepted(store):
    """Регрессия на расхождение подсказки с кодом.

    Во внешнем предложении подсказка предлагала «подтвердить», а список
    принимаемых слов содержал «подтверждаю» — буквальное следование
    инструкции отменяло действие. Тест читает слово из самой подсказки.
    """
    out = call(ADMIN, "выключи телевизор", Factory(), store)
    quoted = re.findall(r"«/tv ([^»]+)»", out)
    assert quoted, f"в подсказке нет слова подтверждения: {out}"
    for word in quoted:
        assert word.lower() in H.CONFIRM_WORDS


@pytest.mark.parametrize("word", ["да", "Да", "подтверждаю", "подтвердить", "yes", "ок"])
def test_confirm_synonyms(store, word):
    f = Factory()
    call(ADMIN, "выключи телевизор", f, store)
    call(ADMIN, word, f, store, now=1.0)
    assert ("send_key", ("Standby",)) in f.tv.calls


def test_other_text_cancels_and_does_not_execute(store):
    f = Factory()
    call(ADMIN, "выключи телевизор", f, store)
    out = call(ADMIN, "статус", f, store, now=1.0)
    assert "Отменено" in out
    assert f.count == 0, "при висящем подтверждении новый текст не выполняется"
    assert not store.pending_for(ADMIN)


def test_expired_confirmation_is_not_executed(store):
    f = Factory()
    call(ADMIN, "выключи телевизор", f, store)
    out = call(ADMIN, "да", f, store, now=H.CONFIRM_TTL_S + 1)
    assert "истёк" in out
    assert f.tv.calls == []


def test_expired_then_new_command_runs(store):
    f = Factory()
    call(ADMIN, "выключи телевизор", f, store)
    out = call(ADMIN, "статус", f, store, now=H.CONFIRM_TTL_S + 1)
    assert "истёк" in out
    assert f.count == 1, "после истечения текст разбирается как новая команда"


def test_pending_is_per_user(store):
    f = Factory()
    H.handle(ADMIN, "выключи телевизор", f, pending=store,
             admin_ids=[ADMIN, GUEST], now=0.0)
    out = H.handle(GUEST, "да", f, pending=store,
                   admin_ids=[ADMIN, GUEST], now=1.0)
    assert f.tv.calls == [], "подтверждение одного админа не выполняет команду другого"
    assert "Не выполнено" in out
    assert store.pending_for(ADMIN)


def test_store_isolated_from_module_default(store):
    H.DEFAULT_PENDING.clear()
    call(ADMIN, "выключи телевизор", Factory(), store)
    assert not H.DEFAULT_PENDING.pending_for(ADMIN)


# --- исключения -------------------------------------------------------------

@pytest.mark.parametrize(("error", "needle"), [
    (TVNotPaired("нет пары"), "пайринг"),
    (TVAuthError("401"), "отклонил"),
    (TVUnsupported("404"), "не умеет"),
    (TVTransportError("таймаут"), "недоступен"),
    (ValueError("TV_HOST не задан"), "не настроен"),
    (RuntimeError("щось"), "Непредвиденная"),
])
def test_errors_become_text(store, error, needle):
    f = Factory(error=error)
    out = call(ADMIN, "статус", f, store)
    assert needle in out


def test_unexpected_error_does_not_escape(store):
    """Исключение не должно всплыть в обработчик бота."""
    f = Factory(error=KeyError("ключ"))
    out = call(ADMIN, "статус", f, store)
    assert "KeyError" in out


# --- префикс ----------------------------------------------------------------

@pytest.mark.parametrize(("raw", "expect"), [
    ("/tv статус", "статус"),
    ("/tv@OkoBot статус", "статус"),
    ("/TV  статус  ", "статус"),
    ("статус", "статус"),
    ("/tv", ""),
    ("/status", "/status"),
])
def test_strip_prefix(raw, expect):
    assert H.strip_prefix(raw) == expect


def test_strip_prefix_keeps_other_commands():
    """Чужая команда бота не должна молча становиться командой телевизора."""
    assert H.strip_prefix("/memory что помнишь") == "/memory что помнишь"


# --- журнал -----------------------------------------------------------------

def test_user_text_not_written_to_log(store, caplog):
    """В журнал идёт имя команды, а не фраза Оператора."""
    caplog.set_level(logging.INFO, logger="oko.tv_bot")
    secret = "подсветка красная"
    call(ADMIN, secret, Factory(), store)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "amb_colour" in joined
    assert secret not in joined


def test_describe_command_mentions_key():
    cmd = Command(kind=BLIND, name="key_Standby", run=lambda tv: "", meta={"key": "Standby"})
    assert "Standby" in H.describe_command(cmd)


def test_describe_standby_is_human_readable():
    cmd = Command(kind=BLIND, name="standby", run=lambda tv: "", needs_confirm=True)
    assert "ожидания" in H.describe_command(cmd)


def test_needs_confirm_drives_the_gate(store):
    """Подтверждение запрашивается по признаку, а не по имени команды."""
    marked = Command(kind=VERIFIED, name="invented", run=lambda tv: "сделано",
                     needs_confirm=True)
    f = Factory()
    import tv_commands
    orig = tv_commands.parse
    try:
        tv_commands.parse = lambda text: marked
        out = call(ADMIN, "нечто", f, store)
    finally:
        tv_commands.parse = orig
    assert "подтвержд" in out.lower()
    assert f.count == 0
