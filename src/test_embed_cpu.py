"""EMBED_CPU (01.11): все запросы эмбеддингов несут options.num_gpu из config.OKO_EMBED_NUM_GPU."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
PRUNE = {".git", "data", "chroma_db", "__pycache__", "venv", ".venv", "node_modules",
         "logs", "backups", "snapshots", "voice_tmp", "documents"}


class _Resp:
    def __init__(self, vec=(0.1, 0.2)):
        self._vec = list(vec)

    def raise_for_status(self):
        pass

    def json(self):
        return {"embedding": self._vec}


@pytest.fixture
def cfg(monkeypatch):
    import config
    monkeypatch.setattr(config, "OKO_EMBED_NUM_GPU", 0)
    return config


def test_body_cpu_default(cfg):
    assert cfg.embed_request_body("bge-m3", "x") == {
        "model": "bge-m3", "prompt": "x", "options": {"num_gpu": 0}}


def test_body_auto_has_no_options(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "OKO_EMBED_NUM_GPU", None)
    assert cfg.embed_request_body("bge-m3", "x") == {"model": "bge-m3", "prompt": "x"}


@pytest.mark.parametrize("raw,want", [
    (None, 0), ("0", 0), ("", None), ("auto", None), (" AUTO ", None),
    ("99", 99), ("-1", 0), ("bad", 0),
])
def test_env_parse(monkeypatch, raw, want):
    import config
    if raw is None:
        monkeypatch.delenv("OKO_EMBED_NUM_GPU", raising=False)
    else:
        monkeypatch.setenv("OKO_EMBED_NUM_GPU", raw)
    assert config._get_embed_num_gpu() == want


def test_knowledge_indexer_sends_num_gpu(cfg, monkeypatch):
    import knowledge_indexer as ki
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"], seen["json"] = url, json
        return _Resp()

    monkeypatch.setattr(ki.requests, "post", fake_post)
    assert ki._request_embedding_vector("текст") == [0.1, 0.2]
    assert seen["url"].endswith("/api/embeddings")
    assert seen["json"]["options"] == {"num_gpu": 0}
    assert seen["json"]["model"] == cfg.OLLAMA_EMBED_MODEL


def test_oko_rag_embeddings_sends_num_gpu(cfg, monkeypatch):
    import knowledge_indexer  # noqa: F401  (добавляет oko_rag/ в sys.path)
    import embeddings
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["json"] = json
        return _Resp()

    monkeypatch.setattr(embeddings.requests, "post", fake_post)
    assert embeddings.get_embedding("q") == [0.1, 0.2]
    assert seen["json"]["options"] == {"num_gpu": 0}


def test_rebuild_embed_sends_num_gpu(cfg, monkeypatch):
    import requests
    import rebuild_index_bge_m3 as reb
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["json"] = json
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    assert reb._embed("q", "bge-m3") == [0.1, 0.2]
    assert seen["json"] == {"model": "bge-m3", "prompt": "q", "options": {"num_gpu": 0}}


def test_governor_vector_s_sends_num_gpu(cfg):
    from governor import AgentGovernor
    gov = AgentGovernor.__new__(AgentGovernor)
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"embedding": [0.3]})
    gov._http = MagicMock()
    gov._http.post = AsyncMock(return_value=resp)
    assert asyncio.run(gov._embed_for_marker("ответ")) == [0.3]
    body = gov._http.post.await_args.kwargs["json"]
    assert body["options"] == {"num_gpu": 0}


def test_guard_every_embeddings_post_uses_body():
    """Сторож: любой нетестовый .py с '/api/embeddings' обязан строить тело через embed_request_body.
    Пропущенный вызов вернёт bge-m3 на GPU и вытеснение qwen -- функционально это незаметно."""
    offenders = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in PRUNE]
        for name in filenames:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            p = Path(dirpath) / name
            if p.parent.name == "tools" and name.startswith("patch_"):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "/api/embeddings" in text and "embed_request_body" not in text:
                offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, f"POST /api/embeddings без embed_request_body: {offenders}"
