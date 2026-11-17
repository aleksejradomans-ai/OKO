"""QFIX (12.11 14:1x, Оператор: «кавычки убрать, заменить на скобки»): каталог для модели без «», примеры фраз в скобках;
вырезание цитат в router линейное (было 1.1 с на 30 000 незакрытых ««) и считает скобки цитатой."""
import re
import time

import pytest

import capabilities as caps
import router as R


def test_catalog_for_model_has_no_guillemets():
    cat = caps.build_catalog()
    assert "«" not in cat and "»" not in cat, cat
    assert "(сделал/включил)" in caps.RULE


def test_human_text_keeps_guillemets():
    assert "«подсветка красная»" in caps.human_text()          # для человека -- как было


# ids короткие: на Windows pytest кладёт имя теста в PYTEST_CURRENT_TEST, предел переменной -- 32767 символов
# (12.11 14:38: строка в 30 000 символов в id уронила блок; на Linux предела нет -- песочница не видела).
@pytest.mark.parametrize("unit,n", [("«", 30000), ("(", 30000), ('"', 30000), ("«а", 20000), ("`", 30000)],
                         ids=["guillemet", "paren", "dquote", "guillemet_a", "backtick"])
def test_quote_strip_is_linear(unit, n):
    s = unit * n
    t = time.perf_counter()
    R.fake_done("vrubi", s)
    R.state_snapshot("?", s)
    assert time.perf_counter() - t < 0.5


@pytest.mark.parametrize("answer", [
    "Скажи (подсветка красная), и бот исполнит.",
    "Попробуй фразу (яркость минус 3).",
    "Для этого есть фраза (громкость 30).",
])
def test_bracket_examples_are_not_status(answer):
    assert R.fake_status(answer) is None, R.fake_status(answer)


@pytest.mark.parametrize("answer", [
    "ТВ (прочитано с телевизора): Подсветка: яркость 2 из 10",
    "Подсветка музыки (как просили).",
    "Готово: установлен розовый оттенок (по запросу).",
])
def test_status_outside_brackets_still_caught(answer):
    assert R.fake_status(answer), answer
