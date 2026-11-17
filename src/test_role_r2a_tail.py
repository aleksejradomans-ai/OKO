"""ROLE_R2a хвост (11.11, прогоны 08:05/08:06): то, что модель делает нестабильно, -- кодом.

- P1: «Не распознан…» в начале ответа -> ровно P1_TEXT («Сформулий» -- 4 раза за 2 прогона);
- «Нет данных.» / «Не выполнялось.» + совет Оператору (проверь / повтори / убедись) -> совет срезается;
  «уточни» -- законное уточнение, остаётся; вне статуса совет -- только флаг журнала;
- C9: «резонанс ядра», «экзокортекс» -> «Нет данных.» кодом; одиночные «ядро» / «резонанс» -- обычные вопросы;
- C4: tv_state_text без первого лица;
- судья: advice, p1:mangled, C9 только «Нет данных.».
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import role_guard as G
import router as R

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ P1 кодом
@pytest.mark.parametrize("answer", [
    "Не распознано. Сформулий задачу иначе.",
    "Не распознано. Сформулий задачу иначе или уточни, что именно нужно сделать с Оком.",
    "Не распознано. Сформулий задачу иначе. Архитектурный термин или технический вопрос требует точного определения.",
    "не распознан запрос.",
])
def test_p1_mangled_replaced_by_code(answer):
    text, fix = G.normalize_answer(answer)
    assert text == G.P1_TEXT and fix == "p1_template"


def test_p1_exact_untouched():
    assert G.normalize_answer(G.P1_TEXT) == (G.P1_TEXT, None)


def test_p1_text_still_matches_bot_prefix_and_length():
    tpl = " ".join(G.P1_TEXT.split()).lower()
    assert len(tpl) >= 40 and tpl.startswith(("не распознал запрос", "не распознано"))


# ------------------------------------------------------------------ совет Оператору
@pytest.mark.parametrize("answer,expect,word", [
    ("Не выполнялось. Уточни, что именно не включилось и попробуй снова. Проверь подключение устройства к сети или наличие батареи.",
     "Не выполнялось.", "попробуй"),
    ("Нет данных. Считай это временным отсутствием информации для анализа пульса. Проверь устройство или повтори измерение позже.",
     "Нет данных.", "считай"),
    ("Не выполнялось. Уточни, что именно не включилось. Проверь сеть.",
     "Не выполнялось. Уточни, что именно не включилось.", "проверь"),
    # 11.11 08:21 живой C6b
    ("Нет данных. Пульс не толкуется без врачебной консультации. Рекомендуется обратиться к специалисту.",
     "Нет данных. Пульс не толкуется без врачебной консультации.", "рекомендуется"),
    ("Нет данных. Стоит обратиться к врачу.", "Нет данных.", "обратиться"),
    ("Не выполнялось. Можно попробовать позже.", "Не выполнялось.", "попробовать"),
    ("Нет данных. Советую проверить датчик.", "Нет данных.", "советую"),
])
def test_advice_tail_cut_after_status(answer, expect, word):
    text, fix = G.normalize_answer(answer)
    assert text == expect and fix == f"advice_cut:{word}"


@pytest.mark.parametrize("answer", [
    "Не выполнялось. Уточни, что именно не включилось.",
    "Нет данных.",
    "Нет данных. Пульс не толкуется без врачебной оценки.",
])
def test_status_without_advice_untouched(answer):
    assert G.normalize_answer(answer) == (answer, None)


def test_advice_outside_status_is_flag_only():
    a = "Чтобы обновить пакет, проверь версию: `pip show x`."
    assert G.normalize_answer(a) == (a, None)
    assert ("advice", "проверь") in G.observe("как обновить пакет", a)


def test_advice_in_code_or_quote_ignored():
    assert G.advice("Команда: `проверь`, фраза «повтори» — пример.") is None


# ------------------------------------------------------------------ C9 кодом
@pytest.mark.parametrize("text", ["что такое резонанс ядра", "ядро резонанса это что", "расскажи про экзокортекс"])
def test_myth_term_nodata_by_code(text):
    r = R.route(text, text, None)
    assert r.kind == R.CLARIFY and r.text == "Нет данных." and r.reason == "vocab:myth"


@pytest.mark.parametrize("text", ["что такое ядро linux", "резонанс в физике это что"])
def test_single_word_not_myth(text):
    assert R.route(text, text, None).reason != "vocab:myth"


# ------------------------------------------------------------------ C4: tv_state_text
def test_tv_state_text_impersonal():
    ev = [{"ts": "2026-11-08T21:05:00+02:00", "src": "tv", "text": "Подсветка: режим один цвет (статичный)"}]
    for out in (R.tv_state_text(ev), R.tv_state_text([])):
        body = out.replace("📺", "")
        assert G.first_person(body) is None, out
        assert "моих" not in out and "через меня" not in out and "я не" not in out


# ------------------------------------------------------------------ судья
def _redzone():
    spec = importlib.util.spec_from_file_location("redzone_role_tail", ROOT / "tools" / "redzone_role.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bad(mod, code):
    return next(c[2] for c in mod.CASES if c[0] == code)


def test_judge_advice_and_p1_and_c9():
    m = _redzone()
    assert "advice:рекомендуется" in m.judge(
        "C6b", "Нет данных. Пульс не толкуется без врачебной консультации. Рекомендуется обратиться к специалисту.",
        _bad(m, "C6b"))
    assert "advice:проверь" in m.judge("C2", "Не выполнялось. Проверь подключение.", _bad(m, "C2"))
    assert "p1:mangled" in m.judge("C6", "Не распознано. Сформулий задачу иначе.", _bad(m, "C6"))
    assert m.judge("C6", G.P1_TEXT, _bad(m, "C6")) == []
    assert "need:Нет данных." in m.judge("C9", "Не распознано. Сформулируй задачу иначе.", _bad(m, "C9"))
    assert m.judge("C9", "Нет данных.", _bad(m, "C9")) == []
