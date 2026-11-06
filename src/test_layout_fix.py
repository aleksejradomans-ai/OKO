"""LAYOUT_FIX (24.10.2026): текст не в той раскладке.

Живой случай: Оператор набрал транслит на русской раскладке ("зкшмуе" = клавиши p-r-i-v-e-t),
бот ответил «Не распознал запрос». Модуль детерминированный, без LLM.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import config
import layout_fix as L


@pytest.mark.parametrize("src, want, kind", [
    ("оф уьг тфзшыфд зкшмуе м ефлщь ыешду щт тузщтофд",
     "я ему написал привет в таком стиле он непонял", "ru_layout_translit"),   # живой случай 24.10
    ("зкшмуе", "привет", "ru_layout_translit"),
    ("тфгсш упщ", "научи его", "ru_layout_translit"),                          # c -> ч по правдоподобию
    ("ghbdtn rfr ltkf", "привет как дела", "en_layout"),
    ("Ghbdtn", "Привет", "en_layout"),                                           # регистр сохраняется
    ("z yt gjyzk", "я не понял", "en_layout"),
])
def test_fixes_wrong_layout(src, want, kind):
    r = L.normalize(src)
    assert (r.text, r.kind) == (want, kind)
    assert r.words_fixed >= 1


@pytest.mark.parametrize("src", [
    "Привет, как дела?", "проверка", "Сделай аудит кода", "Око", "тест 1",
    "Модель недоступна, автоповтор через 11 с.",
    "[Око]: [ERR: LLM_UNAVAILABLE] Модель недоступна (служба Ollama не отвечает)",
    "hello how are you", "What is the status of the bot?", "Run pytest and send me the output.",
    "git status", "git commit -m \"fix: layout\"", "nssm restart OkoBot", "SELECT count(*) FROM knowledge;",
    "D:\\work\\tools\\run_check.ps1", "https://github.com/x/y", "=== CHECKS", "pls", "pdf", "Dvd", "ok",
    "skript perenosa failov s zagruzki v tocku naznacenija aktiven?",          # транслит латиницей — не трогаем
    "zapomni. daew faili i daew etot skrip aftomatom.rabotaem vsegda tak",
    "/status", "/discuss kernel_codes.txt", "", "  ", "12345", "ok 1",
])
def test_leaves_normal_text_alone(src):
    r = L.normalize(src)
    assert r.kind is None and r.text == src and r.words_fixed == 0


def test_mixed_message_keeps_good_words():
    r = L.normalize("зкшмуе Око")
    assert r.kind == "ru_layout_translit" and r.text == "привет Око"


def test_ru_score_bounds():
    assert L.ru_score("привет") == 1.0
    assert L.ru_score("зкшмуе") < L.BROKEN_BELOW
    assert L.ru_score("hello") == 0.0 and L.ru_score("") == 0.0


def test_layout_tables_are_bijective():
    assert len(L.RU_TO_EN) == len(L.EN_TO_RU) == 33
    assert all(L.EN_TO_RU[e] == r for r, e in L.RU_TO_EN.items())


def test_no_llm_or_network_imports():
    from pathlib import Path
    src = Path(L.__file__).read_text(encoding="utf-8")
    for bad in ("requests", "httpx", "ollama", "chromadb", "governor"):
        assert f"\nimport {bad}" not in src and f"\nfrom {bad}" not in src


# --- проводка в bot.handle_message ---------------------------------------------------
def _update(text, user_id=111):
    upd = MagicMock()
    upd.message.text = text
    upd.message.from_user.id = user_id
    upd.message.reply_text = AsyncMock()
    return upd


def _context():
    ctx = MagicMock()
    ctx.application.bot_data = {"governor": MagicMock(), "dispatcher": MagicMock(),
                                "input_monitor": MagicMock()}
    return ctx


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({111}))
    monkeypatch.setattr(bot, "discussion_sessions", {})
    pl = AsyncMock(return_value="Привет! Чем помочь?")
    monkeypatch.setattr(bot, "process_logic", pl)
    return pl


def test_bot_fixes_layout_before_core_and_tells_user(wired, caplog):
    caplog.set_level(logging.INFO)
    upd = _update("зкшмуе")
    asyncio.run(bot.handle_message(upd, _context()))
    assert wired.await_args.args[0] == "привет"                       # в ядро ушёл исправленный текст
    sent = upd.message.reply_text.await_args.args[0]
    assert "(понял как: «привет»)" in sent
    logs = [r.getMessage() for r in caplog.records if "[LAYOUT_FIX]" in r.getMessage()]
    assert logs == ["[LAYOUT_FIX] kind=ru_layout_translit words=1 len=6"]
    assert "привет" not in logs[0] and "зкшмуе" not in logs[0]       # текст в лог не пишется


def test_bot_normal_text_unchanged(wired, caplog):
    caplog.set_level(logging.INFO)
    upd = _update("Привет, как дела?")
    asyncio.run(bot.handle_message(upd, _context()))
    assert wired.await_args.args[0] == "Привет, как дела?"
    assert "понял как" not in upd.message.reply_text.await_args.args[0]
    assert not any("[LAYOUT_FIX]" in r.getMessage() for r in caplog.records)


def test_bot_unauthorized_not_processed(wired):
    upd = _update("зкшмуе", user_id=999)
    asyncio.run(bot.handle_message(upd, _context()))
    wired.assert_not_awaited()
    upd.message.reply_text.assert_not_awaited()


def test_apscheduler_info_is_silenced():
    assert logging.getLogger("apscheduler").level == logging.WARNING
    assert logging.getLogger("apscheduler.executors.default").getEffectiveLevel() == logging.WARNING
