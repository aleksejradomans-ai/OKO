"""Тесты phonetic_key / key_collisions (28.10.2026). Образцы ввода — живые сообщения Оператора."""
import pytest

from layout_fix import key_collisions, normalize, phonetic_key

SAME = [
    # (кириллица, варианты набора транслитом)
    ("пылесос", ["pilesos", "pylesos", "pilesoss"]),
    ("можем", ["mozem", "mozhem"]),
    ("через", ["cerez", "cherez"]),
    ("вывод", ["vivod", "vyvod", "wiwod"]),
    ("подсветка красная", ["podsvetka krasnaya", "podswetka krasnaja", "podsvetka krasnaia"]),
    ("громкость 20", ["gromkostj 20", "gromkost 20", "gromkost' 20"]),
    ("громче", ["gromche", "gromce"]),
    ("тише", ["tishe", "tise"]),
    ("объём", ["objem", "obem"]),
]


@pytest.mark.parametrize("ru,variants", SAME)
def test_variants_meet_cyrillic(ru, variants):
    k = phonetic_key(ru)
    for v in variants:
        assert phonetic_key(v) == k, (v, phonetic_key(v), k)


@pytest.mark.parametrize("a,b", [
    ("громче", "тише"), ("свет", "звук"), ("включи", "выключи"),
    ("громкость 20", "громкость 30"), ("volume", "громкость"),
])
def test_distinct_stay_distinct(a, b):
    # контроль: ключ не должен склеивать всё подряд
    assert phonetic_key(a) != phonetic_key(b)


def test_idempotent_and_empty():
    k = phonetic_key("podsvetka krasnaya")
    assert phonetic_key(k) == k
    assert phonetic_key("") == "" and phonetic_key(None) == ""


def test_collision_detector_must_fire():
    # контрольное условие: детектор обязан найти слияние там, где оно есть
    assert key_collisions(["жар", "зар", "свет"]) == {"зар": ["жар", "зар"]}
    assert key_collisions(["громче", "тише", "свет"]) == {}


def test_normalize_unchanged_for_correct_translit():
    # поведение normalize() сохранено: свободный транслит не переводится
    assert normalize("podsvetka krasnaya").kind is None
    assert normalize("ghbdtn rfr ltkf").text == "привет как дела"
