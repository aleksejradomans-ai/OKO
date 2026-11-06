"""Этап 1 / Компонент A: наблюдение [CTX] в governor (без сети, без LLM)."""
import logging

import pytest

import governor as g


# --- чистая ctx_fields ---------------------------------------------------------
def test_ctx_fields_basic_fill_and_chars():
    f = g.ctx_fields({"prompt_eval_count": 2048, "eval_count": 100}, None, "p" * 10, "s" * 5)
    assert f == {"pe": 2048, "ev": 100, "nctx": 4096, "fill": 0.5, "chars": 15}


def test_ctx_fields_options_num_ctx_wins():
    f = g.ctx_fields({"prompt_eval_count": 4096}, {"num_ctx": 8192}, "", "")
    assert f["nctx"] == 8192 and f["fill"] == 0.5 and f["ev"] is None


def test_ctx_fields_missing_counts_give_none_not_zero():
    f = g.ctx_fields({}, None, "abc", None)
    assert f["pe"] is None and f["ev"] is None and f["fill"] is None and f["chars"] == 3


def test_ctx_fields_bool_not_treated_as_number():
    assert g.ctx_fields({"prompt_eval_count": True}, None, "", "")["fill"] is None


def test_ctx_fields_overflow_fill_above_one_reported():
    # fill>1 — сигнал молчаливой обрезки окна (R1), не ограничиваем сверху.
    assert g.ctx_fields({"prompt_eval_count": 5000}, {"num_ctx": 4096}, "", "")["fill"] > 1.0


def test_ctx_fields_default_nctx_from_attr():
    old = g.ctx_fields.default_nctx
    try:
        g.ctx_fields.default_nctx = 2048
        assert g.ctx_fields({"prompt_eval_count": 1024}, None, "", "")["nctx"] == 2048
    finally:
        g.ctx_fields.default_nctx = old


# --- ContextVar + CM -----------------------------------------------------------
def test_ctx_call_sets_and_resets():
    assert g.ctx_call_type.get() == "main"
    with g._ctx_call("judge"):
        assert g.ctx_call_type.get() == "judge"
        with g._ctx_call("correct"):
            assert g.ctx_call_type.get() == "correct"
        assert g.ctx_call_type.get() == "judge"
    assert g.ctx_call_type.get() == "main"


def test_ctx_call_resets_on_exception():
    with pytest.raises(ValueError):
        with g._ctx_call("discuss"):
            raise ValueError("boom")
    assert g.ctx_call_type.get() == "main"
