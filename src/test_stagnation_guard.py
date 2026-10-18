"""Тесты Патча v2: защита от застоя, «светофор» логов, observability.

Размещение: C:\\oko\\src\\test_stagnation_guard.py (рядом с test_governor.py).
Запуск:     python -m pytest src\\test_stagnation_guard.py -q

Изоляция: реестр создаётся на tmp_path ДО AgentGovernor (синглтон), поэтому
живой registry_state.json не читается и не удаляется. Логи ядра перехватываются
обработчиком, повешенным прямо на governor.logger (не через caplog): так тест
не зависит от того, делает ли logger.py propagate.

Что фиксируют тесты:
  1. 10 служебных кодов пишутся в лог дословно, ровно одной строкой, с уровнем
     по таблице; код в ядре без строки лога -> тест падает.
  2. Светофор: жёлтые коды = WARNING (не считаются watchdog'ом), красные =
     ERROR/CRITICAL (считаются); 12 запросов при недоступной Ollama = 0 красных.
  3. Транспортный сбой между основным вызовом и _meta_reflect = жёлтый
     LLM_UNAVAILABLE/LLM_TIMEOUT, reflection_depth = 0.
  4. Observability: счётчики и last_error в Registry, блок в /status.
  5. Инварианты состояния и FATAL при 1 <= MAX_STAGNATION_CYCLES < MAX_REFLECTION_DEPTH.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

SRC_DIR = Path(__file__).resolve().parent

# JUDGE_FAIL_CLOSED: +JUDGE_UNPARSEABLE (жёлтый), +COMPLIANCE_UNRESOLVED (красный).
YELLOW = {"LLM_TIMEOUT", "LLM_UNAVAILABLE", "STAGNATION_BREAK", "SYSTEM_DEGRADED",
          "DISCUSS_TIMEOUT", "JUDGE_UNPARSEABLE"}
RED = {"RETRY_LIMIT_EXCEEDED", "RECOVERY_FAILED", "KERNEL_FAULT", "HALT_CONDITION",
       "COMPLIANCE_UNRESOLVED"}
ALL_CODES = YELLOW | RED | {"DISCUSS_FAULT"}

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


# --------------------------------------------------------------------------
# helpers / fixtures
# --------------------------------------------------------------------------

def ok(text):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"response": text})
    return resp


def bad(tag=""):
    """Ответ без "response" -> LLMContentError. tag меняет ключ, а значит и
    сигнатуру ошибки ("Unexpected response schema: ['error<tag>']")."""
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
        self.setFormatter(logging.Formatter(LOG_FORMAT))

    def emit(self, record):
        self.records.append(record)

    def lines(self):
        return [self.format(r) for r in self.records]

    def red(self):
        return [r for r in self.records if r.levelno >= logging.ERROR]

    def with_marker(self, code):
        return [r for r in self.records if f"[ERR: {code}]" in r.getMessage()]


def _make_governor(tmp_path):
    import httpx
    from src.registry import Registry

    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))  # изолированный синглтон
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    return governor.AgentGovernor(http_client=client), client


@pytest.fixture
def gc(tmp_path):
    from src.registry import Registry
    gov, client = _make_governor(tmp_path)
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


# --------------------------------------------------------------------------
# 1. десять служебных кодов: таблица, исходник, поведение
# --------------------------------------------------------------------------

class TestServiceCodesTable:
    def test_exactly_twelve_codes_with_expected_colors(self):
        import governor
        assert set(governor.SERVICE_CODES) == ALL_CODES
        assert len(governor.SERVICE_CODES) == 12
        assert set(governor.YELLOW_CODES) == YELLOW
        assert set(governor.RED_CODES) == RED
        assert set(governor.SPLIT_CODES) == {"DISCUSS_FAULT"}

    def test_unknown_code_is_rejected(self, cap):
        import governor
        with pytest.raises(ValueError):
            governor.log_service_code("NOT_A_CODE", "x")

    def test_yellow_code_cannot_be_painted_red(self, cap):
        """Уровень выводится только из таблицы: даже critical=True не делает
        жёлтый код красным."""
        import governor
        governor.log_service_code("LLM_UNAVAILABLE", "x", critical=True, exc_info=False)
        assert [r.levelno for r in cap.records] == [logging.WARNING]


class TestNoCodeWithoutLogLine:
    """Ядро = governor.py. Любой «[ERR: CODE]», вернутый мимо
    log_service_code(), -- возврат без строки в логе. Такой код ломает тест."""

    @staticmethod
    def _source():
        import governor
        return Path(governor.__file__).read_text(encoding="utf-8")

    def test_no_bare_marker_literals_in_core(self):
        bare = re.findall(r"\[ERR: [A-Z_]+\]", self._source())
        assert bare == [], (
            f"маркеры возвращаются мимо log_service_code(): {bare}. "
            "Каждый код обязан идти через log_service_code() -- иначе нет строки в логе."
        )

    def test_every_call_site_uses_declared_code_and_every_code_has_a_call_site(self):
        import governor
        used = set(re.findall(r'log_service_code\(\s*"([A-Z_]+)"', self._source()))
        assert used <= set(governor.SERVICE_CODES), used - set(governor.SERVICE_CODES)
        assert used == set(governor.SERVICE_CODES), set(governor.SERVICE_CODES) - used


def _sc_stagnation(gov, c):
    c.post.side_effect = [bad(), ok("correction"), bad()]
    return run(gov.execute_request("q"))


def _sc_timeout(gov, c):
    c.post.side_effect = asyncio.TimeoutError()
    return run(gov.execute_request("q"))


def _sc_unavailable(gov, c):
    import httpx
    c.post.side_effect = httpx.ConnectError("down")
    return run(gov.execute_request("q"))


def _sc_degraded(gov, c):
    from src.monitor import ComplianceEvent
    for _ in range(10):
        gov.monitor.record_event(ComplianceEvent.VIOLATION)
    return run(gov.execute_request("q"))


def _sc_discuss_timeout(gov, c):
    c.post.side_effect = asyncio.TimeoutError()
    return run(gov.execute_discuss_request("q", "document text"))


def _sc_discuss_fault_transport(gov, c):
    import httpx
    c.post.side_effect = httpx.ConnectError("down")
    return run(gov.execute_discuss_request("q", "document text"))


def _sc_discuss_fault_other(gov, c):
    c.post.side_effect = [bad()]
    return run(gov.execute_discuss_request("q", "document text"))


def _sc_retry_limit(gov, c):
    import config
    seq = []
    for i in range(config.MAX_REFLECTION_DEPTH):
        seq += [bad(i), ok(f"correction{i}")]
    seq.append(bad(config.MAX_REFLECTION_DEPTH))
    c.post.side_effect = seq
    return run(gov.execute_request("q"))


def _sc_recovery_failed(gov, c):
    c.post.side_effect = [bad(), bad("_meta")]
    return run(gov.execute_request("q"))


def _sc_kernel_fault(gov, c):
    async def boom(*a, **k):
        raise RuntimeError("boom")
    gov._process = boom
    return run(gov.execute_request("q"))


def _sc_halt(gov, c):
    c.post.side_effect = [ok("противоречивый ответ"), ok("HALT:POL_002")]
    return run(gov.execute_request("q"))


def _sc_judge_unparseable(gov, c):
    c.post.side_effect = [ok("ответ"), ok("Не могу оценить")]
    return run(gov.execute_request("q"))


def _sc_compliance_unresolved(gov, c):
    c.post.side_effect = [ok("нарушающий"), ok("FAIL:POL_001"),
                          ok("всё ещё нарушающий"), ok("FAIL:POL_001")]
    return run(gov.execute_request("q"))


# (code, scenario, allowed levels)
_WARN = {logging.WARNING}
_RED = {logging.ERROR, logging.CRITICAL}
SCENARIOS = [
    ("STAGNATION_BREAK", _sc_stagnation, _WARN),
    ("LLM_TIMEOUT", _sc_timeout, _WARN),
    ("LLM_UNAVAILABLE", _sc_unavailable, _WARN),
    ("SYSTEM_DEGRADED", _sc_degraded, _WARN),
    ("DISCUSS_TIMEOUT", _sc_discuss_timeout, _WARN),
    ("DISCUSS_FAULT", _sc_discuss_fault_transport, _WARN),   # транспортный вид -- жёлтый
    ("DISCUSS_FAULT", _sc_discuss_fault_other, _RED),        # остальные -- красные
    ("RETRY_LIMIT_EXCEEDED", _sc_retry_limit, _RED),
    ("RECOVERY_FAILED", _sc_recovery_failed, _RED),
    ("KERNEL_FAULT", _sc_kernel_fault, _RED),
    ("HALT_CONDITION", _sc_halt, _RED),
    ("JUDGE_UNPARSEABLE", _sc_judge_unparseable, _WARN),
    ("COMPLIANCE_UNRESOLVED", _sc_compliance_unresolved, _RED),
]


class TestEveryCodeIsLoggedVerbatim:
    @pytest.mark.parametrize("code,scenario,levels", SCENARIOS,
                             ids=[f"{c}-{s.__name__[4:]}" for c, s, _ in SCENARIOS])
    def test_code_returned_and_logged_once_at_right_level(self, gc, cap, code, scenario, levels):
        gov, client = gc
        result = scenario(gov, client)

        assert result == f"[ERR: {code}]"
        hits = cap.with_marker(code)
        assert len(hits) == 1, f"ожидалась ровно 1 строка лога с маркером, найдено {len(hits)}"
        assert hits[0].levelno in levels, (
            f"{code}: уровень {logging.getLevelName(hits[0].levelno)}"
        )
        assert hits[0].name == "kernel.governor"

    def test_all_codes_covered_by_scenarios(self):
        assert {c for c, _, _ in SCENARIOS} == ALL_CODES


# --------------------------------------------------------------------------
# 2. светофор: полный отказ Ollama, watchdog
# --------------------------------------------------------------------------

class TestTrafficLight:
    def test_12_requests_with_ollama_down_produce_zero_red_lines(self, gc, cap):
        import httpx
        gov, client = gc
        client.post.side_effect = httpx.ConnectError("Ollama down")

        results = [run(gov.execute_request(f"запрос {i}")) for i in range(12)]

        assert results == ["[ERR: LLM_UNAVAILABLE]"] * 12
        assert client.post.call_count == 12          # F1: ровно один вызов на запрос
        assert cap.red() == [], [r.getMessage() for r in cap.red()]
        assert len(cap.with_marker("LLM_UNAVAILABLE")) == 12
        assert state(gov, "transport_failures") == 12
        assert state(gov, "status") == "ERROR"
        assert state(gov, "reflection_depth") == 0
        assert str(state(gov, "last_error")).startswith("LLM_UNAVAILABLE:")

    def test_bot_keeps_accepting_requests_after_outage(self, gc, cap):
        import httpx
        gov, client = gc
        client.post.side_effect = httpx.ConnectError("Ollama down")
        run(gov.execute_request("q1"))
        assert state(gov, "status") == "ERROR"

        client.post.side_effect = [ok("ответ"), ok("PASS")]
        assert run(gov.execute_request("q2")) == "ответ"
        assert state(gov, "status") == "READY"

    def test_5xx_is_logged_as_warning_not_error(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [http_5xx(503)]
        assert run(gov.execute_request("q")) == "[ERR: LLM_UNAVAILABLE]"
        assert cap.red() == []
        assert any("LLM HTTP error" in r.getMessage() and r.levelno == logging.WARNING
                   for r in cap.records)

    def test_4xx_content_error_is_still_red(self, gc, cap):
        """4xx -- не транспорт: строка в _route_to_llm остаётся ERROR."""
        gov, client = gc
        client.post.side_effect = [http_5xx(400), ok("correction"), ok("итог"), ok("PASS")]
        assert run(gov.execute_request("q")) == "итог"
        assert any("LLM HTTP error" in r.getMessage() and r.levelno == logging.ERROR
                   for r in cap.records)

    def test_connection_error_line_is_warning(self, gc, cap):
        import httpx
        gov, client = gc
        client.post.side_effect = httpx.ConnectError("down")
        run(gov.execute_request("q"))
        assert any("LLM connection error" in r.getMessage() and r.levelno == logging.WARNING
                   for r in cap.records)


class TestWatchdogCountsOnlyRed:
    @staticmethod
    def _wd_config(log_path, limit=5):
        pw = pytest.importorskip("process_watchdog")
        return pw, pw.WatchdogConfig(
            target_pid_file=log_path.with_name("pid"),
            heartbeat_file=log_path.with_name("hb"),
            target_log_file=log_path,
            memory_limit_mb=512,
            error_rate_limit=limit,
            error_window_sec=60.0,
            heartbeat_timeout_sec=120.0,
            check_interval_sec=10.0,
        )

    def test_outage_log_never_trips_watchdog(self, gc, cap, tmp_path):
        import httpx
        gov, client = gc
        client.post.side_effect = httpx.ConnectError("down")
        for i in range(12):
            run(gov.execute_request(f"q{i}"))
        log = tmp_path / "bot.log"
        log.write_text("\n".join(cap.lines()) + "\n", encoding="utf-8")

        pw, cfg = self._wd_config(log)
        pw.check_error_rate(cfg)          # не должен поднять DegradationDetected

    def test_sixth_red_line_in_window_triggers_sigterm_path(self, tmp_path, cap):
        import governor
        for _ in range(5):
            governor.log_service_code("RETRY_LIMIT_EXCEEDED", "x")
        log = tmp_path / "bot.log"
        log.write_text("\n".join(cap.lines()) + "\n", encoding="utf-8")
        pw, cfg = self._wd_config(log)
        pw.check_error_rate(cfg)          # 5 красных -- ещё не порог

        governor.log_service_code("RECOVERY_FAILED", "x")
        log.write_text("\n".join(cap.lines()) + "\n", encoding="utf-8")
        with pytest.raises(pw.DegradationDetected):
            pw.check_error_rate(cfg)      # 6-я -- срабатывает

    def test_warning_line_that_contains_word_error_is_not_counted(self, tmp_path):
        now = time.strftime("%Y-%m-%d %H:%M:%S") + ",000"
        lines = [f"{now} [WARNING] kernel.governor: boom ERROR CRITICAL inside text"] * 10
        log = tmp_path / "bot.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        pw, cfg = self._wd_config(log)
        pw.check_error_rate(cfg)

    def test_line_without_level_field_falls_back_to_substring(self, tmp_path):
        now = time.strftime("%Y-%m-%d %H:%M:%S") + ",000"
        lines = [f"{now} legacy-format ERROR something"] * 6
        log = tmp_path / "bot.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        pw, cfg = self._wd_config(log)
        with pytest.raises(pw.DegradationDetected):
            pw.check_error_rate(cfg)

    def test_config_default_matches_six_per_window(self):
        import config
        assert config.WATCHDOG_ERROR_RATE_LIMIT == 5   # count > 5  =>  SIGTERM на 6-й
        assert config.WATCHDOG_ERROR_WINDOW_SEC == 60.0


# --------------------------------------------------------------------------
# 3. транспортный сбой между основным вызовом и _meta_reflect
# --------------------------------------------------------------------------

class TestTransportBetweenCallAndReflect:
    def test_connect_error_in_meta_reflect_is_yellow_unavailable(self, gc, cap):
        import httpx
        gov, client = gc
        client.post.side_effect = [bad(), httpx.ConnectError("down")]

        result = run(gov.execute_request("q"))

        assert result == "[ERR: LLM_UNAVAILABLE]"
        assert cap.with_marker("RECOVERY_FAILED") == []
        assert cap.red() == []
        assert client.post.call_count == 2
        assert state(gov, "reflection_depth") == 0
        assert state(gov, "status") == "ERROR"
        assert state(gov, "transport_failures") == 1

    def test_timeout_in_meta_reflect_is_yellow_timeout(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [bad(), asyncio.TimeoutError()]
        assert run(gov.execute_request("q")) == "[ERR: LLM_TIMEOUT]"
        assert cap.red() == []
        assert state(gov, "reflection_depth") == 0

    def test_httpx_read_timeout_maps_to_llm_timeout(self, gc, cap):
        import httpx
        gov, client = gc
        client.post.side_effect = httpx.ReadTimeout("slow")
        assert run(gov.execute_request("q")) == "[ERR: LLM_TIMEOUT]"
        assert cap.red() == []

    def test_transport_failure_on_retry_after_reflection(self, gc, cap):
        """Основной сбой -> рефлексия OK -> повтор падает транспортом."""
        import httpx
        gov, client = gc
        client.post.side_effect = [bad(), ok("correction"), httpx.ConnectError("down")]
        assert run(gov.execute_request("q")) == "[ERR: LLM_UNAVAILABLE]"
        assert state(gov, "reflection_depth") == 0
        assert cap.red() == []

    def test_non_transport_failure_in_meta_reflect_stays_red(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [bad(), bad("_meta")]
        assert run(gov.execute_request("q")) == "[ERR: RECOVERY_FAILED]"
        assert len(cap.with_marker("RECOVERY_FAILED")) == 1


# --------------------------------------------------------------------------
# 4. застой: поведение и инварианты состояния
# --------------------------------------------------------------------------

class TestStagnationGuard:
    def test_identical_failures_break_after_one_reflection(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [bad(), ok("correction"), bad()]

        result = run(gov.execute_request("q"))

        assert result == "[ERR: STAGNATION_BREAK]"
        assert client.post.call_count == 3        # основной, рефлексия, повтор
        # инварианты состояния: немедленный сброс
        assert state(gov, "reflection_depth") == 0
        assert state(gov, "status") == "READY"
        # observability
        assert state(gov, "stagnation_breaks") == 1
        assert "identical failure repeated 2x" in state(gov, "last_stagnation_reason")
        assert "Unexpected response schema" in state(gov, "last_stagnation_reason")
        assert str(state(gov, "last_error")).startswith("STAGNATION_BREAK:")
        assert cap.red() == []                    # жёлтый уровень

    def test_limit_one_breaks_without_any_reflection(self, gc, cap, monkeypatch):
        import config
        monkeypatch.setattr(config, "MAX_STAGNATION_CYCLES", 1)
        gov, client = gc
        client.post.side_effect = [bad()]

        assert run(gov.execute_request("q")) == "[ERR: STAGNATION_BREAK]"
        assert client.post.call_count == 1
        assert state(gov, "reflection_depth") == 0
        assert state(gov, "status") == "READY"

    def test_distinct_failures_reach_retry_limit_not_stagnation(self, gc, cap):
        import config
        gov, client = gc
        seq = []
        for i in range(config.MAX_REFLECTION_DEPTH):
            seq += [bad(i), ok(f"c{i}")]
        seq.append(bad(config.MAX_REFLECTION_DEPTH))
        client.post.side_effect = seq

        assert run(gov.execute_request("q")) == "[ERR: RETRY_LIMIT_EXCEEDED]"
        assert state(gov, "stagnation_breaks") == 0
        assert state(gov, "status") == "ERROR"
        assert client.post.call_count == len(seq)

    def test_alternating_failures_are_not_stagnation(self, gc, cap):
        """A,B,A,B: повторы НЕ подряд -- застоем не считаются."""
        import config
        gov, client = gc
        seq = []
        for i in range(config.MAX_REFLECTION_DEPTH):
            seq += [bad("A" if i % 2 == 0 else "B"), ok(f"c{i}")]
        seq.append(bad("A" if config.MAX_REFLECTION_DEPTH % 2 == 0 else "B"))
        client.post.side_effect = seq

        assert run(gov.execute_request("q")) == "[ERR: RETRY_LIMIT_EXCEEDED]"
        assert state(gov, "stagnation_breaks") == 0

    def test_bot_accepts_requests_after_break_and_counters_accumulate(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [bad(), ok("c"), bad()]
        assert run(gov.execute_request("q1")) == "[ERR: STAGNATION_BREAK]"

        client.post.side_effect = [ok("нормальный ответ"), ok("PASS")]
        assert run(gov.execute_request("q2")) == "нормальный ответ"
        assert state(gov, "status") == "READY"

        client.post.side_effect = [bad("x"), ok("c"), bad("x")]
        assert run(gov.execute_request("q3")) == "[ERR: STAGNATION_BREAK]"
        assert state(gov, "stagnation_breaks") == 2

    def test_stagnation_does_not_touch_transport_counter(self, gc, cap):
        gov, client = gc
        client.post.side_effect = [bad(), ok("c"), bad()]
        run(gov.execute_request("q"))
        assert state(gov, "transport_failures") == 0


class TestStagnationInvariantFatal:
    @pytest.mark.parametrize("stag,depth", [(0, 3), (3, 3), (4, 3), (-1, 3), (1, 1), (2, 2)])
    def test_violation_is_fatal(self, stag, depth):
        import config
        with pytest.raises(SystemExit) as ei:
            config.check_stagnation_invariant(stag, depth)
        assert "MAX_STAGNATION_CYCLES" in str(ei.value)

    @pytest.mark.parametrize("stag,depth", [(1, 2), (1, 3), (2, 3), (4, 5)])
    def test_valid_pairs_pass(self, stag, depth):
        import config
        config.check_stagnation_invariant(stag, depth)

    def test_default_config_is_valid(self):
        import config
        assert 1 <= config.MAX_STAGNATION_CYCLES < config.MAX_REFLECTION_DEPTH

    @pytest.mark.parametrize("bad_value", [0, 3])
    def test_governor_refuses_to_start_when_invariant_broken(self, tmp_path, monkeypatch, bad_value):
        import config
        from src.registry import Registry
        monkeypatch.setattr(config, "MAX_STAGNATION_CYCLES", bad_value)
        try:
            with pytest.raises(SystemExit):
                _make_governor(tmp_path)
        finally:
            Registry._instance = None


# --------------------------------------------------------------------------
# 5. observability: Registry и /status
# --------------------------------------------------------------------------

class TestRegistryObservability:
    def test_new_counters_exist_with_defaults(self, registry):
        assert registry.get_state("transport_failures") == 0
        assert registry.get_state("stagnation_breaks") == 0
        assert registry.get_state("last_stagnation_reason") is None

    def test_schema_accepts_and_rejects(self, registry):
        assert registry.update_state("transport_failures", 3) is True
        assert registry.update_state("transport_failures", "x") is False
        assert registry.update_state("stagnation_breaks", 1) is True
        assert registry.update_state("last_stagnation_reason", "why") is True
        assert registry.update_state("last_stagnation_reason", None) is True
        assert registry.update_state("last_stagnation_reason", 5) is False

    def test_increment_is_atomic_counter(self, registry):
        assert registry.increment_state("transport_failures") == 1
        assert registry.increment_state("transport_failures") == 2
        assert registry.get_state("transport_failures") == 2

    def test_counters_survive_restart(self, tmp_path):
        from src.registry import Registry
        Registry._instance = None
        path = str(tmp_path / "persist.json")
        try:
            r = Registry(filepath=path)
            r.increment_state("stagnation_breaks")
            r.update_state("last_stagnation_reason", "persisted")
            Registry._instance = None
            r2 = Registry(filepath=path)
            assert r2.get_state("stagnation_breaks") == 1
            assert r2.get_state("last_stagnation_reason") == "persisted"
        finally:
            Registry._instance = None

    def test_last_error_keeps_code_and_detail_for_timeouts(self, gc, cap):
        gov, client = gc
        client.post.side_effect = asyncio.TimeoutError()
        run(gov.execute_request("q"))
        assert state(gov, "last_error") == "LLM_TIMEOUT: LLM call exceeded timeout"

    def test_discuss_transport_failure_is_counted(self, gc, cap):
        import httpx
        gov, client = gc
        client.post.side_effect = httpx.ConnectError("down")
        run(gov.execute_discuss_request("q", "doc"))
        assert state(gov, "transport_failures") == 1
        assert str(state(gov, "last_error")).startswith("DISCUSS_FAULT:")


class TestBotSourceContract:
    """Контракт bot.py проверяется по исходнику (импорт bot.py тянет Telegram)."""

    @staticmethod
    def _bot():
        p = SRC_DIR / "bot.py"
        if not p.exists():
            pytest.skip("bot.py not found next to the tests")
        return p.read_text(encoding="utf-8-sig")

    def test_status_has_degradation_markers_block(self):
        src = self._bot()
        assert "DEGRADATION MARKERS" in src
        for field in ("transport_failures", "stagnation_breaks", "last_stagnation_reason"):
            assert field in src

    def test_skipped_history_write_is_warning_with_marker(self):
        src = self._bot()
        lines = [ln for ln in src.splitlines() if "exchange not persisted" in ln]
        if not lines:
            pytest.skip("this bot.py has no skipped-history warning (nothing to mark)")
        for ln in lines:
            assert "logger.warning" in ln or "[HISTORY_SKIPPED]" in ln
            assert "[HISTORY_SKIPPED]" in ln
