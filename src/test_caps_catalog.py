"""CAPS_CATALOG (этап A, 30.10.2026): каталог функций в system prompt основного
контура governor. Каталог получают _process и _self_correct; судья и рефлексия
холодные; /discuss -- только identity. _main_system() не трогает диск."""
import asyncio
import builtins
from unittest.mock import AsyncMock, MagicMock

import pytest

import governor as g

IDENTITY = "Я Око."
CAPS = "[ФУНКЦИИ ОКА]\n- Телевизор: /tv"
COMPOSED = IDENTITY + "\n\n" + CAPS


def _gov(identity=IDENTITY):
    gov = g.AgentGovernor.__new__(g.AgentGovernor)
    gov.identity_text = identity
    gov.registry = MagicMock()
    gov.registry.get_state.return_value = None
    gov.manifesto = MagicMock()
    gov.manifesto.HARD_PROHIBITION.text = "rule"
    gov.manifesto.HARD_PROHIBITION.id = "POL_001"
    gov.manifesto.HALT_CONDITION.text = "halt"
    gov.manifesto.HALT_CONDITION.id = "POL_002"
    gov.manifesto.PRIMARY_OBJECTIVE = "objective"
    gov._call_llm_with_timeout = AsyncMock(return_value="PASS")
    return gov


def _sys(gov):
    return gov._call_llm_with_timeout.call_args.kwargs.get("system_prompt")


@pytest.mark.parametrize(
    "identity,caps,expected",
    [
        (IDENTITY, CAPS, COMPOSED),
        (IDENTITY, None, IDENTITY),
        (None, CAPS, CAPS),
        (None, None, None),
        ("  ", " \n ", None),
    ],
)
def test_compose_system_prompt(identity, caps, expected):
    assert g.compose_system_prompt(identity, caps) == expected


def test_main_system_defaults_to_identity():
    gov = _gov()
    assert not hasattr(gov, "_main_system_prompt")
    assert gov._main_system() == IDENTITY


def test_set_capabilities_composes():
    gov = _gov()
    gov.set_capabilities(CAPS)
    assert gov._main_system() == COMPOSED


def test_set_capabilities_none_resets():
    gov = _gov()
    gov.set_capabilities(CAPS)
    gov.set_capabilities(None)
    assert gov._main_system() == IDENTITY


def test_set_capabilities_truncates():
    gov = _gov()
    gov.set_capabilities("x" * (g.CAPABILITIES_MAX_CHARS + 500))
    assert gov._main_system() == IDENTITY + "\n\n" + "x" * g.CAPABILITIES_MAX_CHARS


def test_process_gets_catalog():
    gov = _gov()
    gov.set_capabilities(CAPS)
    asyncio.run(gov._process("prompt", depth=0))
    assert _sys(gov) == COMPOSED


def test_self_correct_gets_catalog():
    gov = _gov()
    gov.set_capabilities(CAPS)
    asyncio.run(gov._self_correct("previous", "prompt"))
    assert _sys(gov) == COMPOSED


def test_judge_stays_cold():
    gov = _gov()
    gov.set_capabilities(CAPS)
    asyncio.run(gov._verify_compliance("text"))
    assert _sys(gov) is None


def test_reflect_stays_cold():
    gov = _gov()
    gov.set_capabilities(CAPS)
    asyncio.run(gov._meta_reflect(RuntimeError("boom"), "prompt"))
    assert _sys(gov) is None


def test_discuss_identity_only():
    gov = _gov()
    gov.set_capabilities(CAPS)
    gov._call_llm_with_timeout = AsyncMock(return_value="answer")
    asyncio.run(gov.execute_discuss_request("question", "document"))
    assert _sys(gov) == IDENTITY


def test_main_system_no_disk(monkeypatch):
    gov = _gov()
    gov.set_capabilities(CAPS)

    def _no_open(*a, **k):
        raise AssertionError("disk access in _main_system")

    monkeypatch.setattr(builtins, "open", _no_open)
    assert gov._main_system() == COMPOSED
