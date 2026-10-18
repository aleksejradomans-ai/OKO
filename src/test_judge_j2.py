"""Тесты J2: промпт судьи содержит пункт про описание правил/кодов."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC_DIR = Path(__file__).resolve().parent
FALLBACK = "If none of the above clearly applies, answer PASS."
J2_KEY = "merely EXPLAINS, QUOTES or DOCUMENTS"


@pytest.fixture
def gov(tmp_path):
    import httpx
    from src.registry import Registry

    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    yield governor.AgentGovernor(http_client=client)
    Registry._instance = None


def _prompt_for(gov, text="проверяемый текст", raw="PASS"):
    seen = []

    async def fake(prompt, *a, **kw):
        seen.append(prompt)
        return raw

    gov._call_llm_with_timeout = fake
    asyncio.run(gov._verify_compliance(text))
    assert len(seen) == 1
    return seen[0]


def test_j2_present_exactly_once(gov):
    assert _prompt_for(gov).count(J2_KEY) == 1


def test_j2_before_fallback_and_text_block(gov):
    p = _prompt_for(gov)
    assert p.index(J2_KEY) < p.index(FALLBACK) < p.index("TEXT TO CHECK")


def test_j2_does_not_touch_rule_texts(gov):
    p = _prompt_for(gov)
    assert gov.manifesto.HARD_PROHIBITION.text in p
    assert gov.manifesto.HALT_CONDITION.text in p
    assert "HALT:POL_002" in p and "FAIL:POL_001" in p


def test_verdict_parsing_unchanged(gov):
    _prompt_for(gov)  # prime
    gov._call_llm_with_timeout = AsyncMock(return_value="HALT:POL_002")
    assert asyncio.run(gov._verify_compliance("x")).verdict.value == "HALT"


def test_source_marker():
    src = (SRC_DIR / "governor.py").read_text(encoding="utf-8")
    assert "CLARIFICATIONS (J2)" in src
