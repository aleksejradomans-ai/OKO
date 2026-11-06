"""Тесты гейта судьи: пропуск только по ТОЧНОМУ служебному маркеру."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC_DIR = Path(__file__).resolve().parent


@pytest.fixture
def gov(tmp_path):
    import httpx
    from src.registry import Registry

    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    g = governor.AgentGovernor(http_client=client)
    yield g
    Registry._instance = None


def _run(gov, raw):
    """Прогоняет _run_request с подменённой генерацией; судью считает."""
    import governor

    gov._process = AsyncMock(return_value=raw)
    gov._verify_compliance = AsyncMock(
        return_value=governor.ComplianceResult(verdict=governor.ComplianceVerdict.PASS)
    )
    out = asyncio.run(gov._run_request("вопрос", None))
    return out, gov._verify_compliance.await_count


def test_every_known_marker_is_recognized():
    import governor

    for code in governor.SERVICE_CODES:
        assert governor.is_service_marker(f"[ERR: {code}]")
        assert governor.is_service_marker(f"  [ERR: {code}]\n")


@pytest.mark.parametrize("text", [
    "",
    "[ERR:",
    "[ERR: ]",
    "[ERR: UNKNOWN_CODE]",
    "[ERR: HALT_CONDITION] и дальше произвольный текст",
    "[ERR: HALT_CONDITION][ERR: HALT_CONDITION]",
    "текст [ERR: HALT_CONDITION]",
    "[err: halt_condition]",
    "[ERR:HALT_CONDITION]",
])
def test_non_exact_texts_are_not_markers(text):
    import governor

    assert not governor.is_service_marker(text)


def test_non_string_is_not_marker():
    import governor

    assert not governor.is_service_marker(None)
    assert not governor.is_service_marker(123)


def test_exact_marker_skips_judge_and_is_unchecked(gov):
    out, judge_calls = _run(gov, "[ERR: LLM_TIMEOUT]")
    assert out == "[ERR: LLM_TIMEOUT]"
    assert judge_calls == 0
    assert gov.monitor.get_unchecked_rate() == 1.0


def test_marker_plus_payload_goes_to_judge(gov):
    payload = "[ERR: KERNEL_FAULT] и дальше произвольный текст"
    out, judge_calls = _run(gov, payload)
    assert judge_calls == 1
    assert out == payload
    assert gov.monitor.get_unchecked_rate() == 0.0


def test_answer_quoting_marker_is_judged(gov):
    out, judge_calls = _run(gov, "[ERR: SYSTEM_DEGRADED] означает, что предохранитель открыт.")
    assert judge_calls == 1


def test_marker_plus_payload_is_blocked_by_halt_verdict(gov):
    import governor

    gov._process = AsyncMock(return_value="[ERR: KERNEL_FAULT] вредоносный текст")
    gov._verify_compliance = AsyncMock(
        return_value=governor.ComplianceResult(
            verdict=governor.ComplianceVerdict.HALT, violation_id="POL_002"
        )
    )
    out = asyncio.run(gov._run_request("вопрос", None))
    assert out == "[ERR: HALT_CONDITION]"


def test_source_has_no_prefix_gate_on_raw():
    src = (SRC_DIR / "governor.py").read_text(encoding="utf-8")
    assert 'raw.startswith("[ERR:")' not in src
    assert "def is_service_marker" in src
