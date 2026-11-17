"""Тесты словаря программ Samsung (28.10.2026): полнота по коду, кириллица наружу, нет слияний фраз разных программ."""
import re

import pytest

import samsung_names_ru as N
from layout_fix import phonetic_key

# коды из supportedCycles живых машин 28.10 (st_course_map.json)
LIVE = {
    "washer": "1C 1B 1E 96 1F 25 26 33 24 32 20 22 23 2F 21 2A 2E 2D 34 30 27 28 3A".split(),
    "dryer": "51 23 1F 18 19 1D 1B 1C 21 1A 1E 20 27 25 24 26".split(),
}


@pytest.mark.parametrize("kind", ["washer", "dryer"])
def test_every_live_code_has_russian_name(kind):
    assert set(N.BY_KIND[kind]) == set(LIVE[kind])
    for code in LIVE[kind]:
        ru = N.name_ru(kind, f"Table_0X_Course_{code}")
        assert re.search(r"[А-Яа-яЁё]", ru), (code, ru)
        assert not re.search(r"[A-Za-z]", ru), f"латиница в названии для Оператора: {code} {ru}"


@pytest.mark.parametrize("kind", ["washer", "dryer"])
def test_no_phrase_points_to_two_programs(kind):
    """Разные программы не должны сливаться в один фонетический ключ — иначе бот выберет не ту."""
    by_key: dict[str, set[str]] = {}
    for phrase, code in N.phrases(kind):
        by_key.setdefault(phonetic_key(phrase), set()).add(code)
    clashes = {k: v for k, v in by_key.items() if len(v) > 1}
    assert not clashes, clashes


def test_unknown_code_never_leaks_code():
    out = N.name_ru("washer", "Table_02_Course_FF")
    assert "FF" not in out and "Table" not in out


def test_operator_input_resolves():
    """То, что Оператор вводил у дисплея, должно находить свою программу."""
    w = {phonetic_key(p): c for p, c in N.phrases("washer")}
    assert w[phonetic_key("hlopok")] == "1B"
    assert w[phonetic_key("werstj")] == "22"
    assert w[phonetic_key("шерсть")] == "22"
    d = {phonetic_key(p): c for p, c in N.phrases("dryer")}
    assert d[phonetic_key("time dry")] == "27"
    assert d[phonetic_key("сушка по времени")] == "27"


def test_collision_check_can_fail(monkeypatch):
    """Контрольное условие: подмешанная чужая фраза обязана давать слияние."""
    entry = dict(N.WASHER["1B"], aliases=[*N.WASHER["1B"]["aliases"], "sintetika"])
    monkeypatch.setitem(N.WASHER, "1B", entry)
    by_key: dict[str, set[str]] = {}
    for phrase, code in N.phrases("washer"):
        by_key.setdefault(phonetic_key(phrase), set()).add(code)
    assert any(len(v) > 1 for v in by_key.values())
