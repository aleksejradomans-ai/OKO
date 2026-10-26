"""Метрика S (vector_marker + проводка в governor): только наблюдение, без сети."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import vector_marker as vm
from src import governor
from src.manifesto import Manifesto

LOG = logging.getLogger("test.vector_s")


def run(coro):
    return asyncio.run(coro)


class FakeEmbed:
    """Детерминированные векторы по тексту; считает вызовы."""

    def __init__(self, table=None, delay=0.0, exc=None):
        self.table = table or {}
        self.delay = delay
        self.exc = exc
        self.calls = []

    async def __call__(self, text):
        self.calls.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        for key, vec in self.table.items():
            if text.startswith(key):
                return vec
        return [1.0, 0.0, 0.0]


def marker(embed, identity="Я Око.", gpu=None):
    return vm.VectorMarker(identity, Manifesto(), embed, LOG, gpu=gpu, model="bge-m3")


# --- чистые функции ---------------------------------------------------------------
def test_cosine_basic_and_errors():
    assert vm.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert vm.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert vm.cosine([1, 1], [2, 2]) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        vm.cosine([1, 0], [1, 0, 0])
    with pytest.raises(ValueError):
        vm.cosine([0, 0], [1, 0])


def test_manifest_text_deterministic_and_from_manifesto():
    m = Manifesto()
    t = vm.manifest_text(m)
    assert t == vm.manifest_text(Manifesto())
    assert t.index("POL_001") < t.index("POL_002") < t.index("OBJECTIVE")
    assert m.HARD_PROHIBITION.text in t and m.PRIMARY_OBJECTIVE in t


def test_truncate():
    assert vm.truncate("abc", 5) == ("abc", False)
    assert vm.truncate("x" * 10, 4) == ("xxxx", True)


# --- VectorMarker -----------------------------------------------------------------
def test_observe_logs_one_info_line_with_hashes(caplog):
    emb = FakeEmbed({"Я Око": [1.0, 0.0, 0.0], "POL_001": [0.0, 1.0, 0.0], "ответ": [1.0, 1.0, 0.0]})
    m = marker(emb)
    with caplog.at_level(logging.INFO, logger="test.vector_s"):
        r = run(m.observe("ответ модели", "B"))
    assert r["s_identity"] == pytest.approx(0.7071, abs=1e-3)
    assert r["s_manifest"] == pytest.approx(0.7071, abs=1e-3)
    lines = [x.getMessage() for x in caplog.records]
    assert lines[0].startswith("[VECTOR_S] core rebuilt id=")
    id_h, man_h = m.core_hashes()
    assert lines[1].startswith(f"[VECTOR_S] point=B s_identity=0.707 s_manifest=0.707 id={id_h} man={man_h} trunc=0 ms=")
    assert "ответ" not in lines[1]                     # текст ответа в лог не пишется
    assert all(x.levelno == logging.INFO for x in caplog.records)


def test_core_cached_by_hash_and_rebuilt_on_change(caplog):
    emb = FakeEmbed()
    m = marker(emb)
    run(m.observe("a", "B"))
    run(m.observe("b", "B"))
    assert len(emb.calls) == 4                         # 2 опорных + 2 ответа
    m.identity_text = "Новая роль."
    with caplog.at_level(logging.INFO, logger="test.vector_s"):
        run(m.observe("c", "B"))
    assert len(emb.calls) == 6                         # пересчитан только id + ответ
    assert sum("core rebuilt" in r.getMessage() for r in caplog.records) == 1


def test_no_identity_writes_na():
    emb = FakeEmbed()
    m = marker(emb, identity=None)
    r = run(m.observe("ответ", "B"))
    assert r["s_identity"] is None and m.core_hashes()[0] == "none"


def test_long_answer_truncated():
    emb = FakeEmbed()
    m = marker(emb)
    r = run(m.observe("ы" * (vm.MAX_CHARS + 500), "B"))
    assert r["trunc"] is True and len(emb.calls[-1]) == vm.MAX_CHARS


@pytest.mark.parametrize("emb", [FakeEmbed(exc=OSError("refused")), FakeEmbed(exc=ValueError("empty embedding"))])
def test_failure_is_one_warning_never_raises(emb, caplog):
    with caplog.at_level(logging.INFO, logger="test.vector_s"):
        assert run(marker(emb).observe("ответ", "B")) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert caplog.records[0].getMessage().startswith("[VECTOR_S] point=B fail=")


def test_timeout_is_warning(monkeypatch, caplog):
    monkeypatch.setattr(vm, "CORE_TIMEOUT_S", 0.05)
    with caplog.at_level(logging.INFO, logger="test.vector_s"):
        assert run(marker(FakeEmbed(delay=1.0)).observe("ответ", "B")) is None
    assert "fail=TimeoutError" in caplog.records[-1].getMessage()
    assert caplog.records[-1].levelno == logging.WARNING


def test_embed_timeout_comes_from_config():
    """Переключатель живой: константа модуля = значение из config, не литерал."""
    assert vm.EMBED_TIMEOUT_S == pytest.approx(float(config.OKO_EMBED_TIMEOUT_S))
    assert vm.EMBED_TIMEOUT_S > 0
    from pathlib import Path
    src = Path(config.__file__).read_text(encoding="utf-8")
    assert '_get_env_float("OKO_EMBED_TIMEOUT_S"' in src


def test_embed_timeout_applies_to_answer_not_core(monkeypatch, caplog):
    """EMBED_TIMEOUT_S режет только замер ответа; опорные идут по CORE_TIMEOUT_S."""
    monkeypatch.setattr(vm, "EMBED_TIMEOUT_S", 0.05)
    calls = []

    async def embed(text):
        calls.append(text)
        if text.startswith("ответ"):
            await asyncio.sleep(1.0)
        return [1.0, 0.0, 0.0]

    with caplog.at_level(logging.INFO, logger="test.vector_s"):
        assert run(marker(embed).observe("ответ модели", "B")) is None
    assert len(calls) == 3                             # опорные посчитались, упал замер ответа
    assert "fail=TimeoutError" in caplog.records[-1].getMessage()
    assert caplog.records[-1].levelno == logging.WARNING


def test_gpu_busy_skips_and_releases(monkeypatch, caplog):
    monkeypatch.setattr(vm, "QUEUE_WAIT_S", 0.05)

    async def scenario():
        gpu = asyncio.Semaphore(1)
        m = marker(FakeEmbed(), gpu=gpu)
        await gpu.acquire()                            # генерация держит GPU
        r = await m.observe("ответ", "B")
        gpu.release()
        r2 = await m.observe("ответ", "B")
        return r, r2, gpu.locked()

    with caplog.at_level(logging.INFO, logger="test.vector_s"):
        r, r2, locked = run(scenario())
    assert r is None and "fail=QueueBusy" in caplog.text
    assert r2 is not None and locked is False          # семафор отпущен после замера


# --- проводка в governor -----------------------------------------------------------
def _resp(payload):
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=payload)
    return r


def _client_with_embeddings():
    """generate -> PASS-судья; /api/embeddings -> вектор."""
    async def post(url, json=None):
        if url.endswith("/api/embeddings"):
            return _resp({"embedding": [1.0, 0.5, 0.0]})
        if "RULE " in json["prompt"]:
            return _resp({"response": "PASS"})
        return _resp({"response": "Ответ по фактам."})
    c = MagicMock()
    c.post = AsyncMock(side_effect=post)
    return c


def _embed_calls(client):
    return [c for c in client.post.await_args_list if c.args[0].endswith("/api/embeddings")]


def test_governor_injected_client_never_measures():
    async def scenario():
        c = _client_with_embeddings()
        gov = governor.AgentGovernor(http_client=c)
        await gov.start()
        out = await gov.execute_request("вопрос")
        await asyncio.sleep(0.05)
        await gov.shutdown()
        return out, c

    out, c = run(scenario())
    assert out == "Ответ по фактам." and _embed_calls(c) == []


def test_governor_measures_in_background_after_answer(caplog):
    async def scenario():
        c = _client_with_embeddings()
        gov = governor.AgentGovernor(http_client=c)
        await gov.start()
        gov._vector_s_on = True                        # боевой режим (свой клиент)
        out = await gov.execute_request("вопрос")
        n_at_return = len(_embed_calls(c))
        await asyncio.gather(*list(gov._bg_tasks))
        await gov.shutdown()
        return out, n_at_return, c, gov

    with caplog.at_level(logging.INFO):
        out, n_at_return, c, gov = run(scenario())
    assert out == "Ответ по фактам."
    assert n_at_return == 0                            # ответ вернулся до эмбеддинга
    assert len(_embed_calls(c)) == 3                   # identity + манифест + ответ
    assert _embed_calls(c)[0].kwargs["json"]["model"] == config.OLLAMA_EMBED_MODEL
    assert any(r.getMessage().startswith("[VECTOR_S] point=B s_identity=") for r in caplog.records)
    assert gov.transport_gate_state == "CLOSED"


def test_governor_embedding_failure_does_not_touch_transport_gate(caplog):
    async def scenario():
        c = _client_with_embeddings()
        base = c.post.side_effect

        async def post(url, json=None):
            if url.endswith("/api/embeddings"):
                raise OSError("embed down")
            return await base(url, json=json)
        c.post.side_effect = post
        gov = governor.AgentGovernor(http_client=c)
        await gov.start()
        gov._vector_s_on = True
        outs = []
        for _ in range(4):                             # больше порога TransportGate (3)
            outs.append(await gov.execute_request("вопрос"))
            await asyncio.gather(*list(gov._bg_tasks))
        state = gov.transport_gate_state
        await gov.shutdown()
        return outs, state

    with caplog.at_level(logging.INFO):
        outs, state = run(scenario())
    assert outs == ["Ответ по фактам."] * 4 and state == "CLOSED"
    fails = [r for r in caplog.records if "[VECTOR_S]" in r.getMessage() and "fail=" in r.getMessage()]
    assert len(fails) == 4 and all(r.levelno == logging.WARNING for r in fails)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_governor_skips_when_gate_not_closed():
    async def scenario():
        c = _client_with_embeddings()
        gov = governor.AgentGovernor(http_client=c)
        await gov.start()
        gov._vector_s_on = True
        for _ in range(3):                             # порог TransportGate -> OPEN
            gov._transport_gate.record_failure("test")
        assert gov.transport_gate_state == "OPEN"
        gov._observe_vector("ответ", "B")
        n = len(gov._bg_tasks)
        await gov.shutdown()
        return n

    assert run(scenario()) == 0


def test_governor_service_marker_not_measured():
    async def scenario():
        c = MagicMock()
        c.post = AsyncMock(side_effect=OSError("down"))
        gov = governor.AgentGovernor(http_client=c)
        await gov.start()
        gov._vector_s_on = True
        out = await gov.execute_request("вопрос")
        n = len(gov._bg_tasks)
        await gov.shutdown()
        return out, n

    out, n = run(scenario())
    assert governor.is_service_marker(out) and n == 0


def test_shutdown_cancels_pending_measurement():
    async def scenario():
        gov = governor.AgentGovernor(http_client=_client_with_embeddings())
        await gov.start()
        gov._vector_s_on = True

        async def slow(_text):
            await asyncio.sleep(10)
        gov._vector_marker._embed_fn = slow
        gov._observe_vector("ответ", "B")
        await asyncio.sleep(0)
        await asyncio.wait_for(gov.shutdown(), 1.0)
        return gov._bg_tasks

    assert run(scenario()) == set()


def test_config_flag_default_on():
    assert config.VECTOR_S_ENABLED in (True, False)
    from pathlib import Path
    src = Path(config.__file__).read_text(encoding="utf-8")
    assert 'os.getenv("OKO_VECTOR_S", "1")' in src
