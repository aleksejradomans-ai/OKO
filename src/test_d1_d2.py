"""D1 + D2 (02.11.2026).

Живой случай 23:22: на «подсветка music?» мозг почти дословно повторил выдумку 22:41
(«/tv ambilight/mode expert», «/tv ambilight/power On») при kn=31 -- источник история.
D1: выдуманная команда/путь API в ответе мозга -> ответ скрыт, только пометка, обмен не пишется.
D2: короткий вопрос «?» про предмет ТВ -> подсказка готовых фраз, без модели.
"""
import re
from pathlib import Path

import pytest

import router as R

SRC = Path(__file__).resolve().parent

LIVE_2241 = ("Для режима под музыку отправь /tv ambilight/mode expert, затем /tv ambilight/power On "
             "и выбери в меню «Ambilight» -> «Стиль» -> «Следовать за звуком».")


# ------------------------------------------------------------------ D1
@pytest.mark.parametrize("a,frag", [
    (LIVE_2241, "/tv ambilight/"),
    ("Набери /tv audio/volume 20", "/tv audio/"),
    ("Отправь POST на https://192.168.1.5:1926/6/ambilight/power", "ambilight/power"),
    ("Запрос GET ambilight/currentconfiguration вернёт стиль", "ambilight/currentconfiguration"),
    ("Нужен ключ input/key со значением VolumeUp", "input/key"),
    ("Проверь powerstate телевизора", "powerstate"),
    ("Открой menuitems/settings/current", "menuitems/settings"),
    ("Набери /settings и выбери подсветку", "/settings"),
    ("Команда /ambilight music включит режим", "/ambilight"),
    ("Используй /help", "/help"),
])
def test_fake_api_flagged(a, frag):
    assert R.fake_api(a) == frag
    assert R.fake_done("подсветка music?", a) == frag       # и на вопрос -- до выхода по «?»
    assert R.fake_done("включи подсветку", a) == frag


@pytest.mark.parametrize("a", [
    "Подсветка ТВ: напиши «подсветка музыка».",
    "Команды бота: /tv, /garmin, /status, /memory, /newepoch, /force_brain off.",
    "Через /tv подсветка красная тоже работает.",
    "Громкость: 30/60",                               # дробь -- не команда
    "Режим аудио/видео и и/или -- обычный текст.",
    "Скорость 50 km/h, путь C:\\oko\\src и /usr/bin/python -- не команды бота.",
    "Ссылка https://example.com/help -- не команда.",
    "Звук и audio/video синхронизированы.",            # audio/ без пути API
    "FOLLOW_AUDIO/ENERGY_ADAPTIVE_BRIGHTNESS",
    "",
])
def test_fake_api_not_flagged(a):
    assert R.fake_api(a) is None


def test_existing_dguard_question_exemption_kept():
    """Подпись модуля в ответе на вопрос по-прежнему допустима (test_dguard)."""
    assert R.fake_done("что ты сделал с подсветкой?", "Подсветка: FOLLOW_AUDIO/ENERGY_ADAPTIVE_BRIGHTNESS") is None


def test_bot_commands_match_bot_py():
    """Дрейф: список команд в router == CommandHandler в bot.py."""
    src = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    registered = set(re.findall(r'CommandHandler\("([a-z_]+)"', src))
    assert registered == set(R.BOT_COMMANDS)


def test_hints_contain_no_fake_api():
    """Подсказки D2 сами не должны срабатывать как выдумка."""
    for text in R.TV_HINTS.values():
        assert R.fake_api(text) is None


def _func(text, name):
    return text.split(f"async def {name}(", 1)[1].split("\nasync def ", 1)[0].split("\ndef ", 1)[0]


def test_bot_d1_hides_answer_before_history():
    text = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    body = _func(text, "process_logic")
    i = body.index("router.fake_api(raw_response)")
    assert i < body.index("router.fake_done(") < body.index("_save_exchange")
    branch = body[i:body.index("router.fake_done(")]
    assert "return" in branch and "D1_NOTE.format(cmd=api)" in branch
    assert "raw_response" not in branch.split("return", 1)[1].split("\n", 1)[0]   # выдумка не показывается
    assert "fake_api" not in _func(text, "process_discuss_logic")
    assert "{cmd}" in text and "D1_NOTE" in text
    note = text.split("D1_NOTE = (", 1)[1].split(")\n", 1)[0]
    assert R.fake_api(note.replace("{cmd}", "x")) is None   # пометка сама не похожа на выдумку


# ------------------------------------------------------------------ D2
@pytest.mark.parametrize("q,key", [
    ("подсветка music?", "ambilight"),          # живой случай 23:14 / 23:22
    ("подсветка ок?", "ambilight"),             # 03.11: «подсветка работает?» -- TV_STATE (test_tv_state)
    ("подсветка красная ?", "ambilight"),
    ("свет под музыку?", "ambilight"),
    ("громкость?", "volume"),
    ("громкость 20?", "volume"),
    ("звук?", "volume"),
])
def test_question_with_tv_subject_gives_hint(q, key):
    calls = []
    r = R.route(q, q, lambda t: calls.append(t) or "подсветка музыка")
    assert r.kind == R.HINT and r.reason == f"hint_q:{key}" and r.text == R.TV_HINTS[key], r
    assert calls == []


@pytest.mark.parametrize("q", [
    "что такое амбилайт?",                      # рассказ -- мозгу
    "почему подсветка мигает?",
    "какая громкость лучше для фильмов?",
    "подсветка клавиатуры не работает?",        # другое устройство
    "звук в наушниках трещит?",
    "как дела?",
    "у меня есть одна мысль про то как подсветка у монитора могла бы выглядеть в комнате?",   # длинная
])
def test_question_not_hint(q):
    assert R.route(q, q, None).kind == R.BRAIN


@pytest.mark.parametrize("q,kind", [
    ("что умеешь?", R.SELF),
    ("Как прошла ночь?", R.GARMIN),
    ("как сделать подсветку под музыку?", R.HINT),   # R1 (hint:) -- раньше D2 (hint_q:)
    ("подсветка музыка", R.TV),                      # без «?» -- исполняется
])
def test_priority_unchanged(q, kind):
    r = R.route(q, q, None)
    assert r.kind == kind
    if q == "как сделать подсветку под музыку?":
        assert r.reason == "hint:ambilight"


def test_hint_phrases_route_to_tv():
    """Фразы из подсказки исполняются (без «?»)."""
    for text in R.TV_HINTS.values():
        for ph in re.findall(r"«([^»]+)»", text):
            assert R.route(ph, ph, None).kind == R.TV, ph
