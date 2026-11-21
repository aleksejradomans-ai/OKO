"""Эмбеддинг документа по кускам (B30 P5): длинный документ не порождает запроса длиннее предела; короткий --
один запрос и тот же вектор, что раньше. Ollama не вызывается: requests.post подменён."""
import json

import pytest


@pytest.fixture
def ki(monkeypatch):
    import knowledge_indexer as mod
    sent = []

    class Resp:
        def __init__(self, n):
            self._n = n

        def raise_for_status(self):
            return None

        def json(self):
            return {"embedding": [float(self._n), 1.0, -1.0]}

    def fake_post(url, json=None, timeout=None):
        sent.append(json["prompt"])
        return Resp(len(sent))

    monkeypatch.setattr(mod.requests, "post", fake_post)
    mod._sent = sent
    return mod


def test_long_document_never_sends_more_than_limit(ki):
    doc = ("абв где ёжз " * 5000)[: 3 * ki.EMBED_MAX_CHARS + 17]
    assert len(doc) > ki.EMBED_MAX_CHARS
    blob = ki.get_embedding(doc)
    assert ki._sent, "no request at all"
    assert max(len(p) for p in ki._sent) <= ki.EMBED_MAX_CHARS
    assert len(ki._sent) == len(ki._doc_chunks(doc)) > 1
    v = json.loads(blob.decode("utf-8"))
    n = len(ki._sent)
    assert v == [sum(range(1, n + 1)) / n, 1.0, -1.0]                     # поэлементное среднее кусков


def test_short_document_one_request_same_vector(ki):
    doc = "PULSE: 96 BPM short note"
    blob = ki.get_embedding(doc)
    assert ki._sent == [doc] and json.loads(blob.decode("utf-8")) == [1.0, 1.0, -1.0]


def test_over_limit_input_is_not_sent(ki):
    assert ki._request_embedding_vector("x" * (ki.EMBED_MAX_CHARS + 1)) is None
    assert ki._sent == []
    assert ki._request_embedding_vector("x" * ki.EMBED_MAX_CHARS) is not None and len(ki._sent) == 1


def test_one_chunk_without_vector_gives_none_and_stops(ki, monkeypatch):
    calls = []

    def vec(text):
        calls.append(text)
        return None if len(calls) == 2 else [1.0, 2.0]
    monkeypatch.setattr(ki, "_request_embedding_vector", vec)
    doc = "y" * (5 * 800)
    assert ki.get_embedding(doc) is None and len(calls) == 2                # дальше кусков не запрашивает


def test_chunks_are_the_chroma_chunker(ki):
    doc = "z" * 2000
    if ki.RAG_SYNC_AVAILABLE:
        assert ki._doc_chunks(doc) == ki.chunk_text(doc, ki.CHUNK_SIZE, ki.CHUNK_OVERLAP)
    assert all(len(c) <= ki.EMBED_MAX_CHARS for c in ki._doc_chunks(doc))
    assert ki.get_embedding("   ") is None and ki._sent == []
