"""RZ2 STATE_SNAPSHOT (03.11.2026, решение Оператора: «показать, не писать»).

Живой случай 23:39:48 / 23:42:18: на «подсветка music?» мозг пересказал состояние ТВ из блока фактов
(«подсветка музыка включена, цвет оранжевый (hue 20), sat 255, bri 255») -- обмен записан в историю;
блок фактов свежий на каждый запрос, а копия в истории устаревает и потом выдаётся за текущее.
"""
from pathlib import Path

import pytest

import router as R

SRC = Path(__file__).resolve().parent

LIVE = ("ТВ: подсветка музыка включена, цвет оранжевый (hue 20), насыщенность полная (sat 255), "
        "яркость полная (bri 255). Громкость не указана.")


@pytest.mark.parametrize("q,a,sig", [
    ("подсветка music?", LIVE, "hue 20"),
    ("что с подсветкой?", "Подсветка: FOLLOW_AUDIO/ENERGY_ADAPTIVE_BRIGHTNESS", "подсветка:"),
    ("какая громкость?", "Громкость: 30/60", "громкость: 30"),
    ("а ТВ сейчас в каком режиме?", "Режим follow_video с прошлого раза", "follow_video"),
])
def test_snapshot_on_question(q, a, sig):
    assert R.state_snapshot(q, a) == sig
    assert R.fake_done(q, a) is None          # на вопрос -- не подделка, ответ показывается


@pytest.mark.parametrize("q,a", [
    ("подсветка music", LIVE),                # без «?» -- это fake_done (подделка), не снимок
    ("как дела?", "Всё спокойно, техника дома работает."),
    ("что умеешь?", "Подсветку ТВ меняю фразой «подсветка музыка»."),   # «подсветку» -- не подпись «подсветка:»
    ("", ""),
])
def test_not_snapshot(q, a):
    assert R.state_snapshot(q, a) is None


def test_without_qmark_still_fake_done():
    assert R.fake_done("подсветка music", LIVE) == "hue 20"


def _func(text, name):
    return text.split(f"async def {name}(", 1)[1].split("\nasync def ", 1)[0].split("\ndef ", 1)[0]


def test_bot_snapshot_skips_history_but_returns_answer():
    text = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    body = _func(text, "process_logic")
    i = body.index("router.state_snapshot(")
    assert body.index("router.fake_done(") < i < body.index("_save_exchange")
    branch = body[i:body.index("_save_exchange")]
    assert "[STATE_SNAPSHOT]" in branch and "elif forced" in branch
    assert "return" not in branch.split("elif forced", 1)[0]     # ответ НЕ скрывается -- идёт в общий return
    assert "state_snapshot" not in _func(text, "process_discuss_logic")


# --- QWORD (03.11): вопрос без «?» ---------------------------------------------
LIVE_0013 = ("Телевизор на связи, но информация о текущем режиме работы недоступна. Последняя известная сводка "
             "показывает, что подсветка музыки активна с цветом красный (hue 0), максимальной насыщенностью и яркостью.")


@pytest.mark.parametrize("q", [
    "v kakom rezime rabotaet televizor",       # живой случай 00:13
    "в каком режиме работает телевизор",
    "какая сейчас подсветка",
    "chto s podsvetkoj",
    "работает ли подсветка",
])
def test_question_without_qmark(q):
    assert R.is_question(q)
    assert R.fake_done(q, LIVE_0013) is None
    assert R.state_snapshot(q, LIVE_0013) == "hue 0"


@pytest.mark.parametrize("q", [
    "подсветка музыка",
    "включи подсветку как в кино",              # голое «как» -- не вопрос
    "включи подсветку, где музыка",             # просьба с вопросительным словом -- просьба
    "kakao",                                    # граница слова
])
def test_not_question(q):
    assert not R.is_question(q)


def test_request_with_qword_still_guarded():
    assert R.fake_done("включи подсветку, где музыка", "Включил подсветку под музыку.") == "включил"
