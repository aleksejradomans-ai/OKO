"""RAG_COLLECTION_GUARD (19.10).

1. rag_config: пустой/незаданный OKO_COLLECTION -> коллекция под bge-m3,
   а не прежняя "oko_knowledge" (векторы другой модели).
2. indexer.get_collection (запись): новая коллекция -- метрика cosine
   (порог OKO_RAG_MAX_DISTANCE откалиброван под cosine); существующая не меняется.
3. retriever.search (чтение): не создаёт коллекцию; нет коллекции ->
   [] + WARNING [RAG_COLLECTION_MISSING], Ollama не вызывается.
Реальная ChromaDB во временной папке; без chromadb тесты пропускаются.
"""
from __future__ import annotations

import importlib
import logging
import pathlib
import sys

import pytest

chromadb = pytest.importorskip("chromadb")

_OKO_RAG = pathlib.Path(__file__).resolve().parent.parent / "oko_rag"
if str(_OKO_RAG) not in sys.path:
    sys.path.append(str(_OKO_RAG))

import indexer  # noqa: E402
import rag_config  # noqa: E402
import retriever  # noqa: E402


def _space(col) -> str:
    return retriever._space(col)


def _names(client):
    return sorted(getattr(c, "name", c) for c in client.list_collections())


@pytest.fixture
def reload_rag_config(monkeypatch):
    """Перечитывает rag_config с подменённым окружением; после теста --
    восстанавливает модуль с исходным окружением."""
    def _reload(value):
        if value is None:
            monkeypatch.delenv("OKO_COLLECTION", raising=False)
        else:
            monkeypatch.setenv("OKO_COLLECTION", value)
        return importlib.reload(rag_config)

    yield _reload
    monkeypatch.undo()
    importlib.reload(rag_config)


@pytest.fixture
def chroma(tmp_path, monkeypatch):
    path = tmp_path / "chroma_db"
    client = chromadb.PersistentClient(path=str(path))
    monkeypatch.setattr(indexer, "DB_DIR", str(path))
    monkeypatch.setattr(retriever, "get_client", lambda: client)
    monkeypatch.setattr(retriever, "_metric_warned", False)
    return client


# --- 1. rag_config ---

@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_empty_collection_defaults_to_bge_m3(reload_rag_config, value):
    cfg = reload_rag_config(value)
    assert cfg.COLLECTION_NAME == "oko_knowledge_bge_m3"
    assert cfg.COLLECTION_NAME != "oko_knowledge"


def test_explicit_collection_is_used(reload_rag_config):
    assert reload_rag_config(" my_coll ").COLLECTION_NAME == "my_coll"


# --- 2. indexer.get_collection (запись) ---

def test_new_collection_is_created_with_cosine(chroma, monkeypatch):
    monkeypatch.setattr(indexer, "COLLECTION_NAME", "fresh_coll")
    col = indexer.get_collection()
    assert _space(col) == "cosine"
    assert _space(chroma.get_collection("fresh_coll")) == "cosine"


def test_existing_l2_collection_is_not_altered(chroma, monkeypatch):
    chroma.get_or_create_collection("legacy_l2")
    monkeypatch.setattr(indexer, "COLLECTION_NAME", "legacy_l2")
    indexer.get_collection()
    assert _space(chroma.get_collection("legacy_l2")) == "l2"


# --- 3. retriever.search (чтение) ---

def _no_ollama(_text):
    raise AssertionError("Ollama must not be called")


def test_missing_collection_returns_empty_and_does_not_create(chroma, monkeypatch, caplog):
    monkeypatch.setattr(retriever, "COLLECTION_NAME", "absent_coll")
    monkeypatch.setattr(retriever, "get_embedding", _no_ollama)
    with caplog.at_level(logging.WARNING, logger="oko_rag.retriever"):
        assert retriever.search("вопрос") == []
        ctx, hits = retriever.build_context("вопрос")
    assert ctx == "" and hits == []
    assert "absent_coll" not in _names(chroma)
    assert any("[RAG_COLLECTION_MISSING]" in r.getMessage() and "absent_coll" in r.getMessage()
               for r in caplog.records)


def test_existing_collection_is_searched(chroma, monkeypatch):
    col = chroma.get_or_create_collection("live_coll", metadata={"hnsw:space": "cosine"})
    col.add(ids=["h_0"], embeddings=[[1.0, 0.0]], documents=["текст"],
            metadatas=[{"source": "kc.txt", "chunk": 0}])
    monkeypatch.setattr(retriever, "COLLECTION_NAME", "live_coll")
    monkeypatch.setattr(retriever, "get_embedding", lambda t: [1.0, 0.0])
    hits = retriever.search("q", top_k=1)
    assert [(h["source"], h["chunk"]) for h in hits] == [("kc.txt", 0)]


def test_metric_mismatch_warns_once(chroma, monkeypatch, caplog):
    col = chroma.get_or_create_collection("l2_coll")
    col.add(ids=["h_0"], embeddings=[[1.0, 0.0]], documents=["t"], metadatas=[{"source": "s", "chunk": 0}])
    monkeypatch.setattr(retriever, "COLLECTION_NAME", "l2_coll")
    monkeypatch.setattr(retriever, "get_embedding", lambda t: [1.0, 0.0])
    monkeypatch.setenv("OKO_RAG_MAX_DISTANCE", "0.48")
    with caplog.at_level(logging.WARNING, logger="oko_rag.retriever"):
        retriever.search("q", top_k=1)
        retriever.search("q", top_k=1)
    assert sum("[RAG_METRIC_MISMATCH]" in r.getMessage() for r in caplog.records) == 1


def test_other_chroma_errors_still_propagate(monkeypatch):
    class _Broken:
        def get_collection(self, name):
            raise RuntimeError("disk I/O error")

    monkeypatch.setattr(retriever, "get_client", lambda: _Broken())
    with pytest.raises(RuntimeError, match="disk I/O"):
        retriever.search("q")
