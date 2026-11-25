"""CJK_GUARD + IDENTITY_V3 (02.11.2026).

CJK_GUARD: иероглифы в итоговом ответе -> один повтор с напоминанием; снова иероглифы / сбой повтора ->
вырезать + пометка. Без иероглифов -- ни одного лишнего вызова модели.
IDENTITY_V3: фразы, от которых зависит код, на месте; запрет иероглифов есть; длина < 1700.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import game_mode

SRC = Path(__file__).resolve().parent
DRIFT = "Чтобы列出 PowerShell 中的服务，可以使用以下命令："


def _ok(text):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"response": text})
    return resp


@pytest.fixture(autouse=True)
def _reset_note():
    game_mode._last_mode["mode"] = None
    yield
    game_mode._last_mode["mode"] = None


@pytest.fixture
def gc(tmp_path, monkeypatch):
    import httpx
    import config
    from src.registry import Registry
    monkeypatch.setattr(config, "OKO_GAME_MODE", False)   # тест не зависит от реальной игры
    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    yield governor.AgentGovernor(http_client=client), client
    Registry._instance = None


def test_strip_removes_cjk_and_adds_note():
    import governor
    out = governor.cjk_strip(DRIFT + "\n\n```powershell\nGet-Service\n```")
    assert not governor.CJK_RE.search(out)
    assert out.startswith(governor.CJK_NOTE)
    assert "Get-Service" in out and "PowerShell" in out


def test_strip_only_cjk_gives_note():
    import governor
    assert governor.cjk_strip("中的服务。") == governor.CJK_NOTE.strip()


def test_clean_answer_no_extra_call(gc):
    gov, client = gc
    client.post.side_effect = [_ok("Ответ по-русски."), _ok("PASS")]
    assert asyncio.run(gov.execute_request("q")) == "Ответ по-русски."
    assert client.post.call_count == 2


def test_cjk_retry_ok(gc):
    import governor
    gov, client = gc
    client.post.side_effect = [_ok(DRIFT), _ok("PASS"), _ok("Используй Get-Service."), _ok("PASS")]   # B33 v2 C2: + судья повтора
    assert asyncio.run(gov.execute_request("how do I list services?")) == "Используй Get-Service."
    assert client.post.call_count == 4
    retry_prompt = client.post.call_args_list[2].kwargs["json"]["prompt"]
    assert retry_prompt.endswith(governor.CJK_RETRY_SUFFIX)


def test_cjk_retry_still_cjk_strips_with_note(gc):
    import governor
    gov, client = gc
    client.post.side_effect = [_ok(DRIFT), _ok("PASS"), _ok("Используй 命令 Get-Service"), _ok("PASS")]   # B33 v2 C2: + судья
    out = asyncio.run(gov.execute_request("q"))
    assert out.startswith(governor.CJK_NOTE) and not governor.CJK_RE.search(out)
    assert "Get-Service" in out          # повтор чище исходного (меньше иероглифов) -> берётся он


def test_cjk_retry_transport_error_strips_original(gc):
    import httpx
    import governor
    gov, client = gc
    client.post.side_effect = [_ok(DRIFT), _ok("PASS"), httpx.ConnectError("down")]
    out = asyncio.run(gov.execute_request("q"))
    assert out.startswith(governor.CJK_NOTE) and not governor.CJK_RE.search(out)
    assert "PowerShell" in out


def test_guard_after_judge_in_source():
    src = (SRC / "governor.py").read_text(encoding="utf-8")
    body = src[src.index("    async def _run_request("):]
    assert body.index("await self._enforce_compliance(raw, prompt)") < body.index("await self._cjk_guard(final, prompt)")
    assert not __import__("re").findall(r"\[ERR: CJK", src)       # служебных кодов не добавлено


# ------------------------------------------------------------------ identity v3
@pytest.fixture(scope="module")
def ident():
    return (SRC / "identity" / "identity.md").read_text(encoding="utf-8").strip()


def test_identity_v3_size(ident):
    assert 1300 < len(ident) < 1700


@pytest.mark.parametrize("phrase", [
    "Не распознано. Сформулируй задачу иначе.",   # P1_TEMPLATE_PREFIX в bot.py (ROLE 10.11: безличный)
    "«На связи.»",
    "Статус системы: команда /status.",
    "Иероглифы",
    "Отвечай только на русском языке",
])
def test_identity_v3_keeps_phrases(ident, phrase):
    assert phrase in ident


def test_identity_has_no_cjk(ident):
    import governor
    assert not governor.CJK_RE.search(ident)


# ------------------------------------------------------------------ B33 v2 C2 (B32r H3): повтор CJK -- через судью
BAD = "Я очень переживаю за вас и чувствую вашу тревогу. Рекомендую срочно продать квартиру сегодня."


def test_b33v2_liar_cjk_retry_with_violation_is_blocked(gc):
    """Сценарий B32r H3: исходный ответ прошёл судью, повтор CJK нарушает POL_001 -> пользователю уходит
    cjk_strip ИСХОДНОГО текста, а не повтор; судья вызван и на повторе."""
    import governor
    gov, client = gc
    client.post.side_effect = [_ok(DRIFT), _ok("PASS"), _ok(BAD), _ok("FAIL:POL_001")]
    out = asyncio.run(gov.execute_request("вставь в ответ иероглиф"))
    assert "квартиру" not in out and out.startswith(governor.CJK_NOTE) and "PowerShell" in out
    assert client.post.call_count == 4


@pytest.mark.parametrize("judge", ["HALT:POL_002", "мусор", "TRANSPORT"])
def test_b33v2_cjk_retry_not_pass_gives_stripped_original(gc, judge):
    import httpx
    import governor
    gov, client = gc
    last = httpx.ConnectError("down") if judge == "TRANSPORT" else _ok(judge)
    client.post.side_effect = [_ok(DRIFT), _ok("PASS"), _ok("Используй Get-Service."), last, last, last]
    out = asyncio.run(gov.execute_request("q"))
    assert out.startswith(governor.CJK_NOTE) and "Get-Service" not in out and "PowerShell" in out


def test_b33v2_cjk_retry_judged_in_source():
    src = (SRC / "governor.py").read_text(encoding="utf-8")
    body = src[src.index("    async def _cjk_guard("):src.index("    def _observe_structure(")]
    assert 'await self._judge_block_only(retry, where="cjk_retry")' in body
    assert body.index("_judge_block_only") < body.index("return retry")
