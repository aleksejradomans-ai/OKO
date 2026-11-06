"""Тесты порога релевантности OKO_RAG_MAX_DISTANCE в retriever.build_context.

search() подменяется, поэтому Ollama и ChromaDB не нужны; сам модуль
retriever импортирует chromadb через indexer, поэтому без него тесты
пропускаются, а не падают.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

pytest.importorskip("chromadb")

_OKO_RAG = pathlib.Path(__file__).resolve().parent.parent / "oko_rag"
if str(_OKO_RAG) not in sys.path:
    sys.path.append(str(_OKO_RAG))

retriever = pytest.importorskip("retriever")


def _hits():
    return [
        {"text": "близкий", "source": "a.pdf", "chunk": 1, "distance": 0.30},
        {"text": "средний", "source": "a.pdf", "chunk": 2, "distance": 0.45},
        {"text": "далёкий", "source": "b.txt", "chunk": 0, "distance": 0.60},
    ]


@pytest.fixture
def fake_search(monkeypatch):
    monkeypatch.setattr(retriever, "search", lambda q, top_k=5: _hits())


def test_threshold_off_by_default(fake_search, monkeypatch):
    monkeypatch.delenv("OKO_RAG_MAX_DISTANCE", raising=False)
    ctx, hits = retriever.build_context("q")
    assert [h["chunk"] for h in hits] == [1, 2, 0]


@pytest.mark.parametrize("raw", ["0", "", "abc", "-1"])
def test_zero_empty_or_invalid_value_disables_filter(fake_search, monkeypatch, raw):
    monkeypatch.setenv("OKO_RAG_MAX_DISTANCE", raw)
    _, hits = retriever.build_context("q")
    assert len(hits) == 3


def test_threshold_drops_far_chunks(fake_search, monkeypatch):
    monkeypatch.setenv("OKO_RAG_MAX_DISTANCE", "0.5")
    ctx, hits = retriever.build_context("q")
    assert [h["distance"] for h in hits] == [0.30, 0.45]
    assert "далёкий" not in ctx


def test_threshold_boundary_is_inclusive(fake_search, monkeypatch):
    monkeypatch.setenv("OKO_RAG_MAX_DISTANCE", "0.45")
    _, hits = retriever.build_context("q")
    assert [h["distance"] for h in hits] == [0.30, 0.45]


def test_everything_filtered_gives_empty_context(fake_search, monkeypatch):
    monkeypatch.setenv("OKO_RAG_MAX_DISTANCE", "0.1")
    ctx, hits = retriever.build_context("q")
    assert ctx == "" and hits == []


def test_search_itself_stays_raw(monkeypatch):
    """search() порог не применяет: диагностические скрипты видят всё."""
    monkeypatch.setenv("OKO_RAG_MAX_DISTANCE", "0.1")
    assert not hasattr(retriever, "MAX_DISTANCE")
    import inspect
    assert "_max_distance" not in inspect.getsource(retriever.search)
