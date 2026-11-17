"""PROMPT_PARTS + CTX_LOAD_MS (02.11.2026): наблюдение, без изменения ответа."""
import pathlib
import re

import pytest

import governor

SRC = pathlib.Path(__file__).resolve().parent


def _text(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("data, want", [
    ({"load_duration": 6_012_345_678}, 6012),
    ({"load_duration": 499_999}, 0),
    ({"load_duration": 500_000}, 0),          # банковское округление round(): 0.5 -> 0
    ({"load_duration": 1_500_000}, 2),
    ({"load_duration": 0}, 0),
    ({"load_duration": 2.5e6}, 2),
    ({}, None),
    ({"load_duration": None}, None),
    ({"load_duration": "123"}, None),
    ({"load_duration": True}, None),
    ({"load_duration": -1}, None),
    (None, None),
])
def test_ctx_load_ms(data, want):
    assert governor.ctx_load_ms(data) == want


def test_ctx_line_ends_with_load_ms():
    src = _text("governor.py")
    fmt = re.findall(r'"\[CTX\] call=[^"]*"', src)
    assert fmt == ['"[CTX] call=%s pe=%s ev=%s nctx=%s fill=%s chars=%d load_ms=%s"']
    assert "ctx_load_ms(data)" in src


def test_ctx_fields_unchanged_keys():
    f = governor.ctx_fields({"prompt_eval_count": 100, "eval_count": 5,
                             "load_duration": 7_000_000}, None, "p", "s")
    assert set(f) == {"pe", "ev", "nctx", "fill", "chars"}


def test_prompt_parts_logged_every_request_before_cut():
    src = _text("bot.py")
    i_fit = src.index("fitted = _fit_knowledge(knowledge, budget)")
    i_parts = src.index('"[PROMPT_PARTS] sys=%d hist=%d layers=%d facts=%d q=%d budget=%d kn=%d->%d"')
    i_cond = src.index("if fitted != knowledge:")
    assert i_fit < i_parts < i_cond      # вне условия обрезки -> на каждый запрос
    assert src.count("[PROMPT_PARTS]") == 1   # одна строка лога


def test_prompt_parts_no_user_id_or_text():
    src = _text("bot.py")
    start = src.index('logger.info("[PROMPT_PARTS]')
    call = src[start:src.index(")\n", src.index("len(fitted)", start)) + 1]
    assert "user_id" not in call
    assert re.search(r"\bsanitized_input\b(?!\))", call) is None  # только len(...)
    assert "len(sanitized_input)" in call
