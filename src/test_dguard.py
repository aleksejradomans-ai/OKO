"""D_GUARD (этап D, 01.11.2026): запрет ложного «сделал» в ответе мозга."""
from pathlib import Path

import pytest

import router as R

SRC = Path(__file__).resolve().parent


@pytest.mark.parametrize("q,a,word", [
    ("включи свет в комнате", "Включил свет.", "включил"),
    ("включи свет в комнате", "Готово!", "готово"),
    ("поставь таймер", "Установило таймер на 10 минут.", "установило"),
    ("включи кондиционер", "Кондиционер включён.", "включен"),
    ("включи подсветку как в клубе", "Подсветка переключена в режим вечеринки.", "переключена"),
    ("сделай потише", "Сделал тише.", "сделал"),
])
def test_fake_done_flagged(q, a, word):
    assert R.fake_done(q, a) == word


@pytest.mark.parametrize("q,a", [
    ("включи свет в комнате", "Я не включил свет: такой команды нет."),       # отрицание
    ("включи кондиционер", "Кондиционер не включен: нет такой функции."),
    ("выключи кондиционер", "Не могу выключить кондиционер."),                 # инфинитив
    ("что ты сделал с подсветкой?", "Я переключил подсветку на музыку."),     # вопрос о прошлом
    ("расскажи про погоду", "Готово, вот прогноз"),                           # просьба не о действии
    ("поставь таймер", "Стиральная запустила «Микс»."),                       # женский род -- о технике
    ("привет", "Включил режим ожидания."),                                    # нет просьбы
])
def test_fake_done_not_flagged(q, a):
    assert R.fake_done(q, a) is None


@pytest.mark.parametrize("q,a,word", [
    ("podsvetka zelenaja", "Подсветка: цвет зелёный, hue 60, sat 255, bri 255", "подсветка:"),   # 01.11 21:2x
    ("подсветку синюю", "Готово. hue 210", "hue 210"),
    ("громче", "Громкость: 30/60", "громкость: 30"),
    ("включи подсветку как в кино", "Режим FOLLOW_VIDEO установлен", "follow_video"),
])
def test_module_signature_flagged(q, a, word):
    assert R.fake_done(q, a) == word


def test_module_signature_in_answer_to_question_allowed():
    assert R.fake_done("что ты сделал с подсветкой?", "Подсветка: FOLLOW_AUDIO/ENERGY_ADAPTIVE_BRIGHTNESS") is None


def test_live_case_30_10():
    """30.10 14:29-15:27: риск ложного «готово» на «включи свет под музыку» текстом без /tv."""
    assert R.fake_done("включи свет под музыку", "Готово, подсветка под музыку включена.") == "готово"


def _func(text, name):
    return text.split(f"async def {name}(", 1)[1].split("\nasync def ", 1)[0].split("\ndef ", 1)[0]


def test_bot_guard_before_history_save():
    text = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    body = _func(text, "process_logic")
    i = body.index("router.fake_done(")
    assert i < body.index("_save_exchange")
    tail = body[i:body.index("_save_exchange")]
    assert "return" in tail                                   # при срабатывании -- выход ДО записи
    assert "fake_done" not in _func(text, "process_discuss_logic")
    assert "D_NOTE" in text and "{word}" in text
