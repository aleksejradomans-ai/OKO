"""Тесты RAG_READABILITY_GUARD (oko_rag/loader.py)."""
import logging
import pathlib
import sys

import pytest

_OKO_RAG = pathlib.Path(__file__).resolve().parent.parent / "oko_rag"
if str(_OKO_RAG) not in sys.path:
    sys.path.append(str(_OKO_RAG))

import loader  # noqa: E402

TOFU = "\u25a0"
RU = "Векторизация реализована в knowledge_indexer.py с использованием nomic-embed-text."


def test_garbage_ratio_basic():
    assert loader.garbage_ratio("") == 0.0
    assert loader.garbage_ratio("   \n ") == 0.0
    assert loader.garbage_ratio(RU) == 0.0
    assert loader.garbage_ratio(TOFU * 5 + "abcde") == pytest.approx(0.5)
    assert loader.garbage_ratio("\ufffd" * 2 + "ab") == pytest.approx(0.5)


def test_whitespace_is_not_counted():
    assert loader.garbage_ratio(TOFU + " " * 50 + "a") == pytest.approx(0.5)


def test_readable_text_passes():
    loader.check_readable(RU, "ok.txt")


def test_few_bullets_do_not_trigger():
    text = (TOFU + " пункт списка\n") * 3 + RU * 5
    assert loader.garbage_ratio(text) < loader.GARBAGE_MAX_RATIO
    loader.check_readable(text, "bullets.txt")


def test_tofu_document_is_rejected_with_warning(caplog):
    text = f"{TOFU * 40} Ollama (gemma2:latest), {TOFU * 30} Telegram-{TOFU * 20}"
    with caplog.at_level(logging.WARNING, logger="oko_rag.loader"):
        with pytest.raises(loader.UnreadableDocumentError):
            loader.check_readable(text, "bad.pdf")
    assert any("[UNREADABLE_DOCUMENT]" in r.getMessage() and "bad.pdf" in r.getMessage()
               for r in caplog.records)


def test_error_is_value_error():
    assert issubclass(loader.UnreadableDocumentError, ValueError)


def test_load_document_rejects_garbage_txt(tmp_path):
    f = tmp_path / "bad.txt"
    f.write_text(TOFU * 100 + " abc", encoding="utf-8")
    with pytest.raises(loader.UnreadableDocumentError):
        loader.load_document(str(f))


def test_load_document_returns_good_txt(tmp_path):
    f = tmp_path / "ok.txt"
    f.write_text(RU, encoding="utf-8")
    assert loader.load_document(str(f)) == RU


def test_load_document_pdf_goes_through_guard(tmp_path, monkeypatch):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(loader, "read_pdf", lambda p: TOFU * 200)
    with pytest.raises(loader.UnreadableDocumentError):
        loader.load_document(str(f))
    monkeypatch.setattr(loader, "read_pdf", lambda p: RU)
    assert loader.load_document(str(f)) == RU


def test_unsupported_extension_still_value_error(tmp_path):
    f = tmp_path / "x.docx"
    f.write_text("a")
    with pytest.raises(ValueError):
        loader.load_document(str(f))
