"""RAG_EXCLUDE (R2) + TV_HINT (R1), 02.11.2026.

Живой случай 22:41: «как сделать подсветку под музыку?» -> мозг пересказал JSON API ТВ из справки RAG
(tv_philips_api_v6.txt). R2: такие источники не идут в справку чата. R1: вопрос «как …» про ТВ -> подсказка
фраз маршрутизатора без модели.
"""
import re
import sys
from pathlib import Path

import pytest

import router as R

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent


# ------------------------------------------------------------------ R1
@pytest.mark.parametrize("q,key", [
    ("как сделать подсветку под музыку?", "ambilight"),
    ("Как поменять цвет подсветки", "ambilight"),
    ("как настроить яркость подсветки", "ambilight"),
    ("как поменять цвет света", "ambilight"),
    ("как убавить громкость", "volume"),
    ("каким образом выключить звук", "volume"),
])
def test_how_question_gives_hint(q, key):
    r = R.route(q, q, None)
    assert r.kind == R.HINT and r.reason == f"hint:{key}"
    assert r.text == R.TV_HINTS[key]


@pytest.mark.parametrize("q", [
    "как включить подсветку клавиатуры",        # другое устройство
    "как работает подсветка",                  # нет глагола действия -- рассказ, мозгу
    "почему подсветка мигает",
    "подсветка музыка",                        # команда -- исполняется, не подсказка
    "как дела",
])
def test_not_hint(q):
    assert R.route(q, q, None).kind != R.HINT


def test_every_hint_phrase_routes_to_tv():
    for key, text in R.TV_HINTS.items():
        phrases = re.findall(r"«([^»]+)»", text)
        assert phrases, key
        for ph in phrases:
            r = R.route(ph, ph, None)
            assert r.kind == R.TV, (key, ph, r)


def test_hint_works_in_game_without_intent():
    calls = []
    r = R.route("как сделать подсветку под музыку?", "как сделать подсветку под музыку?",
                lambda t: calls.append(t) or None)
    assert r.kind == R.HINT and calls == []


# ------------------------------------------------------------------ R2
@pytest.fixture
def retriever(monkeypatch):
    p = str(ROOT / "oko_rag")
    if p not in sys.path:
        sys.path.insert(0, p)
    import retriever as ret
    hits = [
        {"text": "API table", "source": "tv_philips_api_v6.txt", "chunk": 7, "distance": 0.2},
        {"text": "user note", "source": "notes.txt", "chunk": 1, "distance": 0.3},
        {"text": "nested", "source": r"C:\oko\data\knowledge\tv_philips_api_v7.txt", "chunk": 2, "distance": 0.35},
    ]
    monkeypatch.setattr(ret, "search", lambda q, top_k=5: list(hits))
    monkeypatch.setattr(ret, "_max_distance", lambda: 0.0)
    return ret


def test_exclude_drops_masked_sources(retriever):
    ctx, used = retriever.build_context("q", exclude=("tv_philips_api_*",))
    assert [h["source"] for h in used] == ["notes.txt"]
    assert "API table" not in ctx and "nested" not in ctx and "user note" in ctx


def test_no_exclude_keeps_all(retriever):
    ctx, used = retriever.build_context("q")
    assert len(used) == 3


def test_config_default_excludes_tv_api():
    import config
    assert "tv_philips_api_*" in config.OKO_RAG_BRAIN_EXCLUDE


def test_bot_chat_passes_exclude_and_handles_hint():
    src = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    assert '_rag_build_context(query, exclude=getattr(config, "OKO_RAG_BRAIN_EXCLUDE", ()))' in src
    body = src[src.index("async def _route_reply("):]
    assert body.index("router.HINT") < body.index("router.DEVICES")
