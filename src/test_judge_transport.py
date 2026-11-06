"""Судья и коррекция при обрыве связи с Ollama (F1 для контура проверки).

Размещение: C:\\oko\\src\\test_judge_transport.py
Запуск:     python -m pytest src\\test_judge_transport.py -q

Инвариант: если Ollama недоступна во время проверки ответа (судья),
коррекции или повторной проверки, пользователь получает жёлтый
служебный код, а не непроверенный текст и не текст, который судья
уже признал нарушением. Нетранспортный сбой судьи (LLMContentError)
сознательно не меняется (решение Оператора, см. последний тест).

Фикстуры повторяют test_stagnation_guard.py (изолированный Registry
на tmp_path, логи перехватываются на governor.logger).
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


def ok(text):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"response": text})
    return resp


def bad(tag=""):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={f"error{tag}": "model not loaded"})
    return resp


def http_5xx(status=503):
    import httpx
    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
        "server error", request=MagicMock(), response=MagicMock(status_code=status)))
    return resp


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def red(self):
        return [r for r in self.records if r.levelno >= logging.ERROR]


@pytest.fixture
def gc(tmp_path):
    import httpx
    from src.registry import Registry
    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    gov = governor.AgentGovernor(http_client=client)
    yield gov, client
    Registry._instance = None


@pytest.fixture
def cap():
    import governor
    handler = _Capture()
    lg = governor.logger
    old_level = lg.level
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    yield handler
    lg.removeHandler(handler)
    lg.setLevel(old_level)


def run(coro):
    return asyncio.run(coro)


def state(gov, key):
    return gov.registry.get_state(key)


class TestJudgeTransportFailClosed:
    def test_judge_connect_error_blocks_unchecked_response(self, gc, cap):
        import httpx
        gov, client = gc
        client.post.side_effect = [ok("основной ответ"), httpx.ConnectError("down")]

        result = run(gov.execute_request("вопрос"))

        assert result == "[ERR: LLM_UNAVAILABLE]"
        assert client.post.call_count == 2
        assert state(gov, "last_compliance_status") == "UNCHECKED"
        assert state(gov, "compliance_check_failures") == 1
        assert state(gov, "transport_failures") == 1
        assert gov.monitor.get_unchecked_rate() == 1.0
        assert cap.red() == [], [r.getMessage() for r in cap.red()]

    def test_judge_timeout_blocks_unchecked_response(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [ok("основной ответ"), asyncio.TimeoutError()]
        assert run(gov.execute_request("вопрос")) == "[ERR: LLM_TIMEOUT]"
        assert state(gov, "transport_failures") == 1
        assert cap.red() == []

    def test_judge_httpx_read_timeout_blocks(self, gc, cap):
        import httpx
        gov, client = gc
        client.post.side_effect = [ok("основной ответ"), httpx.ReadTimeout("slow")]
        assert run(gov.execute_request("вопрос")) == "[ERR: LLM_TIMEOUT]"
        assert cap.red() == []

    def test_judge_5xx_blocks(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [ok("основной ответ"), http_5xx(503)]
        assert run(gov.execute_request("вопрос")) == "[ERR: LLM_UNAVAILABLE]"
        assert cap.red() == []

    def test_self_correct_transport_after_fail_does_not_leak_violation(self, gc, cap):
        """Судья вынес FAIL, коррекция упала транспортом: текст с известным
        нарушением пользователю не уходит."""
        import httpx
        gov, client = gc
        client.post.side_effect = [
            ok("нарушающий ответ"), ok("FAIL:POL_001"), httpx.ConnectError("down"),
        ]

        result = run(gov.execute_request("вопрос"))

        assert result == "[ERR: LLM_UNAVAILABLE]"
        assert client.post.call_count == 3
        assert state(gov, "compliance_violations") == 1
        assert state(gov, "transport_failures") == 1
        assert cap.red() == [], [r.getMessage() for r in cap.red()]

    def test_reverify_transport_does_not_leak_unchecked_correction(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [
            ok("нарушающий ответ"), ok("FAIL:POL_001"),
            ok("исправленный ответ"), asyncio.TimeoutError(),
        ]

        result = run(gov.execute_request("вопрос"))

        assert result == "[ERR: LLM_TIMEOUT]"
        assert client.post.call_count == 4
        assert state(gov, "last_compliance_status") == "UNCHECKED_AFTER_CORRECTION"
        assert cap.red() == []

    def test_gpu_queue_starvation_cannot_bypass_judge(self, gc, cap, monkeypatch):
        """Атака нагрузкой: при GPU_CONCURRENCY=1 судья запроса A ждёт семафор,
        пока генерирует запрос B, и истекает JUDGE_TIMEOUT. Ответ A не должен
        уйти непроверенным."""
        import config
        gov, client = gc
        assert config.GPU_CONCURRENCY == 1
        monkeypatch.setattr(config, "JUDGE_TIMEOUT", 0.05)

        async def fake_post(endpoint, json=None, **kwargs):
            prompt = json["prompt"]
            if "TEXT TO CHECK" in prompt:          # вызов судьи
                return ok("PASS")
            await asyncio.sleep(0.3)                # основная генерация
            return ok("ответ-A" if "запрос A" in prompt else "ответ-B")

        client.post.side_effect = fake_post

        async def both():
            return await asyncio.gather(
                gov.execute_request("запрос A"), gov.execute_request("запрос B"),
            )

        results = run(both())

        assert "ответ-A" not in results, results
        assert results[0] == "[ERR: LLM_TIMEOUT]"
        assert results[1] == "ответ-B"


class TestJudgeNonTransportUnchanged:
    def test_judge_content_error_still_returns_original_unchecked(self, gc, cap):
        """Решение Оператора 1 (статус-кво): нетранспортный сбой судьи
        (ответ без поля response) по-прежнему отдаёт ответ как UNCHECKED.
        Если решение изменится, меняется этот тест."""
        gov, client = gc
        client.post.side_effect = [ok("основной ответ"), bad("_judge")]
        assert run(gov.execute_request("вопрос")) == "основной ответ"
        assert state(gov, "last_compliance_status") == "UNCHECKED"
        assert state(gov, "transport_failures") == 0
