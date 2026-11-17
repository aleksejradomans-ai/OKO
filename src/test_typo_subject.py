"""TYPO_SUBJECT (02.11.2026, Red Zone 1): опечатка в слове-предмете ТВ.

Живой случай 23:42:18: «одсветка music?» -> AMB_RE не узнал предмет -> мозг (через 24 с ответ «подсветка … hue 20»).
Нечёткое совпадение (Дамерау-Левенштейн <= 1) -- только длинные слова-предметы, только кириллица, только запасной путь.
Проверка на ложные срабатывания по 200 000 частых русских слов (wordfreq, 02.11): единственное -- «яростью» (в стоп-списке).
"""
import pytest

import router as R


@pytest.mark.parametrize("w,fixed", [
    ("одсветка", "подсветка"),      # пропуск
    ("подстветка", "подсветка"),    # вставка
    ("подсвтека", "подсветка"),     # перестановка соседних
    ("подсветкц", "подсветка"),     # замена
    ("одсветку", "подсветку"),
    ("ромкость", "громкость"),
    ("гормкость", "громкость"),
    ("амбилат", "амбилайт"),
    ("ярксоти", "яркости"),
])
def test_typo_fixed(w, fixed):
    assert R._typo_word(w) == fixed


@pytest.mark.parametrize("w", [
    "подсветка", "громкостью",               # без опечатки -- как есть
    "яростью", "ярости",                     # настоящие слова (стоп-список / короче порога)
    "сеть", "звон", "свят",                  # короткие -- нечёткого совпадения нет
    "краткость", "легкость", "мягкость", "подсветить",
    "одсвтека",                              # расстояние 2 -- не исправляется
])
def test_typo_not_fixed(w):
    assert R._typo_word(w) == w


def test_fix_keeps_qmark_and_latin():
    assert R.fix_subject_typos("Одсветка music?") == "подсветка music?"


def _no_intent(t):
    raise AssertionError("модель смысла не должна вызываться")


def test_live_case_23_42_question_gives_hint():
    r = R.route("одсветка music?", "одсветка music?", _no_intent)
    assert r.kind == R.HINT and r.reason == "typo:hint_q:ambilight" and r.text == R.TV_HINTS["ambilight"]


@pytest.mark.parametrize("q,reason", [
    ("одсветка красная", "typo:tv_rule:amb_colour"),
    ("ромкость плюс 5", "typo:tv_rule:volume_up"),
])
def test_typo_command_executes_with_note(q, reason):
    r = R.route(q, q, _no_intent)
    assert r.kind == R.TV and r.reason == reason
    assert r.note == f"Понял как «{r.text}». "       # Оператор видит, как понят текст


def test_typo_how_question_gives_hint():
    r = R.route("как убавить ромкость", "как убавить ромкость", _no_intent)
    assert r.kind == R.HINT and r.reason == "typo:hint:volume"


def test_exact_route_has_no_typo_prefix():
    r = R.route("подсветка музыка", "подсветка музыка", _no_intent)
    assert r.kind == R.TV and r.reason == "tv_rule:amb_audio" and r.note == ""


@pytest.mark.parametrize("q", [
    "с яростью?",
    "привет",
    "одсветка клавиатуры не работает?",      # другое устройство -- мозг и после исправления
    "почему одсветка мигает?",
])
def test_typo_brain(q):
    assert R.route(q, q, None).kind == R.BRAIN


def test_typo_note_passes_tv_outcome():
    r = R.route("одсветка красная", "одсветка красная", _no_intent)
    reply, outcome = R.run_tv(1, r, lambda u, t, f: "Подсветка: FIXED_COLOR", None)
    assert reply.startswith("Понял как «") and outcome == "PASS"
