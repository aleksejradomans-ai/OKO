"""ROLE_R2b (11.11, ТЗ ROLE_R2 v2 §2; решение 23:15 п.4 + 23:37): лампы потолка отдельно от подсветки ТВ.

До R2b (проверено на коде 2cd48f9): «люстра цвет синий» -> tv_rule:amb_colour, «yarkost lustry 50» ->
tv_rule:amb_brightness_set -- перекрашивалась ПОДСВЕТКА ТВ. Прямого управления лампами Deltaco нет (выключатель PENDING),
поэтому -- честный ответ кодом, без ТВ и без модели; голое «лампы» -- два предмета -> уточнение.
"""
from __future__ import annotations

import pytest

import role_guard as G
import router as R


@pytest.mark.parametrize("text", [
    "люстра цвет синий", "yarkost lustry 50",                 # живые дыры: красили подсветку ТВ
    "включи люстру", "выключи люстру", "vkljuci ljustru", "сделай люстру красной", "люстру поярче",
    "свет на потолке включи", "потолок ярче", "верхний свет выключи", "штанга синяя",
    "почему люстра не горит?",
])
def test_ceiling_lamps_answered_by_code_not_tv(text):
    r = R.route(text, text, None)
    assert r.kind == R.CLARIFY and r.text == R.LAMPS_TEXT and r.reason == "lamps:ceiling", r


@pytest.mark.parametrize("text", ["лампы", "лампа", "лампы синие", "включи лампы", "vkljuci lampy"])
def test_bare_lamps_ask_which(text):
    r = R.route(text, text, None)
    assert r.kind == R.CLARIFY and r.text == R.LAMP_WHICH_TEXT and r.reason == "lamps:which", r


@pytest.mark.parametrize("text,reason", [
    ("подсветка синяя", "tv_rule:amb_colour"),
    ("podsvetka zelenaja", "tv_rule:amb_colour"),
])
def test_tv_backlight_still_tv(text, reason):
    r = R.route(text, text, None)
    assert (r.kind, r.reason) == (R.TV, reason)


@pytest.mark.parametrize("text", ["какой потолок у этой модели", "потолок производительности", "планка памяти"])
def test_weak_words_without_light_action_not_lamps(text):
    assert not R.route(text, text, None).reason.startswith("lamps:")


def test_lamp_texts_impersonal_honest_no_advice():
    for t in (R.LAMPS_TEXT, R.LAMP_WHICH_TEXT, R.LIGHT_TEXT):
        assert G.first_person(t) is None, t
        assert G.advice(t) is None, t
    assert R.LAMPS_TEXT.startswith("Не выполнялось.")
    assert "люстра" in R.LIGHT_TEXT


def test_lamps_checked_before_remember_is_not_shadowed():
    r = R.route("запомни: люстра — штанга", "запомни: люстра — штанга", None)
    assert r.reason == "mem:request"


def test_amb_music_doc_names_tv_leds_apart():
    import amb_music
    doc = amb_music.__doc__
    assert "13 светодиодов подсветки ТВ" in doc and "13 ламп" not in doc
