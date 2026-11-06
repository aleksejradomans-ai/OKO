"""Тесты _explain_error: пояснение только к строке-маркеру, цитаты не трогаются.

Функция и словарь извлекаются из исходника bot.py через ast и исполняются
изолированно: тест не зависит от установленного telegram и проверяет
именно тот текст, что лежит в bot.py."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent / "bot.py"


@pytest.fixture(scope="module")
def ns():
    tree = ast.parse(SRC.read_text(encoding="utf-8-sig"))
    keep = [
        n for n in tree.body
        if (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_ERR_TEXT" for t in n.targets))
        or (isinstance(n, ast.FunctionDef) and n.name == "_explain_error")
    ]
    assert len(keep) == 2, "не найдены _ERR_TEXT и _explain_error"
    space = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(SRC), "exec"), space)
    return space


def test_exact_marker_is_explained_for_every_known_code(ns):
    for code, text in ns["_ERR_TEXT"].items():
        marker = f"[ERR: {code}]"
        assert ns["_explain_error"](marker) == f"{marker} {text}"


def test_unknown_code_unchanged(ns):
    assert ns["_explain_error"]("[ERR: NO_SUCH_CODE]") == "[ERR: NO_SUCH_CODE]"


def test_quote_at_start_of_explanation_unchanged(ns):
    s = "[ERR: SYSTEM_DEGRADED] означает, что предохранитель открыт."
    assert ns["_explain_error"](s) == s


def test_quote_inside_sentence_unchanged(ns):
    s = "Код [ERR: HALT_CONDITION] выдаётся при противоречии."
    assert ns["_explain_error"](s) == s


def test_marker_with_trailing_text_on_same_line_unchanged(ns):
    s = "[ERR: HALT_CONDITION] и дальше произвольный текст"
    assert ns["_explain_error"](s) == s


def test_marker_in_middle_of_multiline_text_unchanged(ns):
    s = "Первая строка\n[ERR: SYSTEM_DEGRADED]\nПоследняя строка текста"
    assert ns["_explain_error"](s) == s


def test_notice_before_marker_keeps_notice_and_explains_marker(ns):
    s = "⚠️ уведомление слоя B\n[ERR: HALT_CONDITION]"
    out = ns["_explain_error"](s)
    assert out.startswith("⚠️ уведомление слоя B\n[ERR: HALT_CONDITION] ")
    assert ns["_ERR_TEXT"]["HALT_CONDITION"] in out


def test_trailing_whitespace_and_newlines_tolerated(ns):
    out = ns["_explain_error"]("[ERR: RECOVERY_FAILED]  \n\n")
    assert out.startswith("[ERR: RECOVERY_FAILED] ")
    assert ns["_ERR_TEXT"]["RECOVERY_FAILED"] in out


def test_crlf_marker_line(ns):
    out = ns["_explain_error"]("текст\r\n[ERR: SYSTEM_DEGRADED]\r\n")
    assert ns["_ERR_TEXT"]["SYSTEM_DEGRADED"] in out


@pytest.mark.parametrize("value", [None, "", "   ", "\n\n"])
def test_empty_values_returned_as_is(ns, value):
    assert ns["_explain_error"](value) == value


def test_source_marker_and_no_search():
    src = SRC.read_text(encoding="utf-8-sig")
    assert "MARKER_LINE_ONLY" in src
    assert 're.search(r"\\[ERR: (\\w+)\\]"' not in src
