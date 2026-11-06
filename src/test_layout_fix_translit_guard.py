"""Тесты защиты транслита от ложной «неверной раскладки» (28.10.2026, живой случай: werstj -> цукыео)."""
import pytest

from layout_fix import normalize


@pytest.mark.parametrize("text", ["werstj", "Werstj", "stirka werstj", "hlopok", "sintetika", "shtori"])
def test_translit_kept(text):
    r = normalize(text)
    assert r.kind is None and r.text == text


@pytest.mark.parametrize("text,expected", [
    ("ghbdtn rfr ltkf", "привет как дела"),
    ("ntcn", "тест"),                 # контроль: в транслите «нтчн» нет гласных — это раскладка
    ("wdtn", "цвет"),
    ("yfcnhjqrb", "настройки"),
    ("gjlcdtnrf rhfcyfz", "подсветка красная"),
])
def test_layout_still_fixed(text, expected):
    assert normalize(text).text == expected


def test_guard_changes_nothing_else():
    """Контроль: на этих входах версия до правки (8A097BA0B306FE8C) давала ровно это же. Правка — только про транслит."""
    same_as_before = {
        "ctujlyz": "ctujlyz",            # доля латинских гласных > 0.2 — раскладку не чинили и раньше (отдельное ограничение)
        "ult ,eltn": "ult ,eltn",
        "vfibyf": "vfibyf",              # одиночное слово с гласными — не чинилось и раньше
        "jryj": "jryj",
    }
    for src, exp in same_as_before.items():
        assert normalize(src).text == exp, src
