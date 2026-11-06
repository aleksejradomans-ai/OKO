#!/usr/bin/env python3
"""apply_stagnation_patch.py -- Patch v2: stagnation guard, 3-level log
"traffic light", observability.

Place in C:\\oko (or anywhere); --src points to the directory with the
sources (default: <script dir>\\src if it exists, else the script dir).

    python apply_stagnation_patch.py                 # DRY-RUN (default): writes nothing
    python apply_stagnation_patch.py --diff-out stagnation_patch.diff
    python apply_stagnation_patch.py --apply         # backup *.bak_stagnation_v2 + write

Guarantees:
  * all-or-nothing for the required files: every anchor must be found
    EXACTLY once, otherwise nothing is written;
  * bot.py is optional (anchors there are less certain): if its anchors do
    not match, bot.py is skipped with a warning, the rest is applied;
  * BOM and CRLF/LF of every file are preserved;
  * every patched file is compile()-checked in memory before writing;
  * idempotent: an already patched file is skipped;
  * a *.bak_stagnation_v2 backup is created before overwriting
    (never overwritten: a second backup gets a numeric suffix).

After --apply (Windows): run update_manifest.ps1 / verify_manifest.ps1,
then:  python -m pytest src\\test_stagnation_guard.py src\\test_governor.py -q
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

BAK_SUFFIX = ".bak_stagnation_v2"


class PatchError(Exception):
    pass


# --------------------------------------------------------------------------
# Edit primitives (operate on LF-normalised text)
# --------------------------------------------------------------------------

@dataclass
class Exact:
    label: str
    old: str
    new: str

    def apply(self, text: str) -> str:
        n = text.count(self.old)
        if n != 1:
            raise PatchError(f"[{self.label}] anchor found {n}x (need exactly 1)")
        return text.replace(self.old, self.new, 1)


@dataclass
class Range:
    """Replace [start .. end] inclusive. start must be unique; end is the
    first occurrence after start."""
    label: str
    start: str
    end: str
    new: str
    max_span: int = 6000

    def apply(self, text: str) -> str:
        n = text.count(self.start)
        if n != 1:
            raise PatchError(f"[{self.label}] start anchor found {n}x (need exactly 1)")
        i = text.index(self.start)
        j = text.find(self.end, i + len(self.start))
        if j < 0:
            raise PatchError(f"[{self.label}] end anchor not found after start")
        j += len(self.end)
        if j - i > self.max_span:
            raise PatchError(f"[{self.label}] span {j - i} > {self.max_span}: wrong end anchor?")
        return text[:i] + self.new + text[j:]


@dataclass
class Func:
    label: str
    fn: Callable[[str], str]

    def apply(self, text: str) -> str:
        return self.fn(text)


# --------------------------------------------------------------------------
# governor.py
# --------------------------------------------------------------------------

GOV_IMPORTS = Exact(
    "gov.imports",
    "import asyncio\nimport logger as oko_logger\nimport re\n",
    "import asyncio\nimport logger as oko_logger\nimport logging\nimport re\n",
)

GOV_CLASSES_AND_CODES = Exact(
    "gov.classes+codes",
    r'''class LLMContentError(RuntimeError):
    """Ответ получен, но непригоден: 4xx, не-JSON, неверная схема."""

logger = oko_logger.get_logger("kernel.governor")
''',
    r'''class LLMTimeoutError(LLMTransportError):
    """Таймаут транспорта (httpx.TimeoutException): подвид транспортного сбоя,
    отдельный код LLM_TIMEOUT."""


class LLMContentError(RuntimeError):
    """Ответ получен, но непригоден: 4xx, не-JSON, неверная схема."""

logger = oko_logger.get_logger("kernel.governor")

# --- PATCH_V2_STAGNATION: трёхуровневый «светофор» логов -------------------
# Единственный источник правды о служебных кодах и их уровне лога.
#   YELLOW -> logger.WARNING: НЕ фатально для process_watchdog (он считает
#             только строки уровня ERROR/CRITICAL).
#   RED    -> ERROR/CRITICAL: считаются watchdog'ом (порог/окно — config).
#   SPLIT  -> DISCUSS_FAULT: транспортный вид жёлтый, остальные красные.
# Каждый возврат маркера "[ERR: <CODE>]" из ядра ОБЯЗАН идти через
# log_service_code(): маркер пишется в лог дословно и ровно одной строкой
# (две строки на одно событие удвоили бы счёт watchdog'а). Тест
# test_stagnation_guard.py падает, если в ядре появился код без такого вызова.
YELLOW_CODES = frozenset({
    "LLM_TIMEOUT", "LLM_UNAVAILABLE", "STAGNATION_BREAK",
    "SYSTEM_DEGRADED", "DISCUSS_TIMEOUT",
})
RED_CODES = frozenset({
    "RETRY_LIMIT_EXCEEDED", "RECOVERY_FAILED", "KERNEL_FAULT", "HALT_CONDITION",
})
SPLIT_CODES = frozenset({"DISCUSS_FAULT"})
SERVICE_CODES = YELLOW_CODES | RED_CODES | SPLIT_CODES


def log_service_code(
    code: str,
    detail: str = "",
    *,
    transport: bool = False,
    critical: bool = False,
    exc_info: bool = False,
) -> str:
    """Пишет служебный код в лог дословно ("[ERR: <CODE>] detail") и возвращает
    маркер. Уровень выводится ТОЛЬКО из таблицы выше — вызывающий код не
    может «покрасить» жёлтый код в красный."""
    if code not in SERVICE_CODES:
        raise ValueError(f"unknown service code: {code!r}")
    if code in YELLOW_CODES or (code in SPLIT_CODES and transport):
        level = logging.WARNING
    else:
        level = logging.CRITICAL if critical else logging.ERROR
    marker = f"[ERR: {code}]"
    if detail:
        logger.log(level, "%s %s", marker, detail, exc_info=exc_info)
    else:
        logger.log(level, "%s", marker, exc_info=exc_info)
    return marker

''',
)

GOV_INIT = Exact(
    "gov.__init__",
    "        self.registry = Registry()\n"
    "        self._http: Optional[httpx.AsyncClient] = http_client  # для unit-тестов с моком",
    "        # PATCH_V2_STAGNATION: FATAL-стоп, если нарушено 1 <= MAX_STAGNATION_CYCLES\n"
    "        # < MAX_REFLECTION_DEPTH (проверка живая, читает текущие значения config).\n"
    "        config.check_stagnation_invariant()\n"
    "        self.registry = Registry()\n"
    "        self._http: Optional[httpx.AsyncClient] = http_client  # для unit-тестов с моком",
)

GOV_DEGRADED = Exact(
    "gov.system_degraded",
    r'''                logger.warning(
                    "High violation rate (%.0f%%), refusing request",
                    self.monitor.get_error_rate() * 100,
                )
                return "[ERR: SYSTEM_DEGRADED]"''',
    r'''                return log_service_code(
                    "SYSTEM_DEGRADED",
                    f"High violation rate ({self.monitor.get_error_rate() * 100:.0f}%), "
                    "refusing request",
                )''',
)

GOV_KERNEL_FAULT = Exact(
    "gov.kernel_fault",
    r'''            logger.critical("Unhandled fault in execute_request: %s", e, exc_info=True)
            self.registry.update_state("status", "ERROR")
            self.registry.update_state("last_error", str(e))
            return "[ERR: KERNEL_FAULT]"''',
    r'''            marker = log_service_code(
                "KERNEL_FAULT", f"Unhandled fault in execute_request: {e}",
                critical=True, exc_info=True,
            )
            self.registry.update_state("status", "ERROR")
            self.registry.update_state("last_error", f"KERNEL_FAULT: {e}")
            return marker''',
)

GOV_DISCUSS = Exact(
    "gov.discuss",
    r'''        except asyncio.TimeoutError:
            logger.error("Discuss request timed out for user=%s", user_id)
            return "[ERR: DISCUSS_TIMEOUT]"
        except Exception as e:
            logger.error("Discuss request failed for user=%s: %s", user_id, e)
            return "[ERR: DISCUSS_FAULT]"''',
    r'''        except (asyncio.TimeoutError, LLMTimeoutError):
            self.registry.increment_state("transport_failures")
            self.registry.update_state("last_error", "DISCUSS_TIMEOUT: LLM call exceeded timeout")
            return log_service_code(
                "DISCUSS_TIMEOUT", f"Discuss request timed out for user={user_id}"
            )
        except LLMTransportError as e:
            self.registry.increment_state("transport_failures")
            self.registry.update_state("last_error", f"DISCUSS_FAULT: {e}")
            return log_service_code(
                "DISCUSS_FAULT",
                f"Discuss transport failure for user={user_id}: {e}",
                transport=True,
            )
        except Exception as e:
            self.registry.update_state("last_error", f"DISCUSS_FAULT: {e}")
            return log_service_code(
                "DISCUSS_FAULT", f"Discuss request failed for user={user_id}: {e}"
            )''',
)

GOV_PROCESS_BLOCK = Range(
    "gov._process/_transport_failure/_handle_failure",
    "    async def _process(self, prompt: str, depth: int) -> str:",
    '            f"ORIGINAL: {prompt} | CORRECTION: {correction}", depth + 1\n        )',
    r'''    async def _process(self, prompt: str, depth: int, trail: tuple = ()) -> str:
        """trail -- сигнатуры последовательных сбоев текущей цепочки рефлексии
        (PATCH_V2_STAGNATION): по ним _handle_failure отличает «ещё пробуем»
        от «застоя» (одна и та же ошибка повторяется, рефлексия не помогает)."""
        self.registry.update_state("status", "BUSY")
        try:
            result = await self._call_llm_with_timeout(prompt, system_prompt=self.identity_text)
            self.registry.update_state("status", "READY")
            return result

        except (asyncio.TimeoutError, LLMTimeoutError, LLMTransportError) as e:
            return self._transport_failure(e)
        except Exception as e:
            return await self._handle_failure(e, prompt, depth, trail)

    def _transport_failure(self, error: BaseException) -> str:
        """F1 + светофор: транспортный сбой не запускает _meta_reflect. Раньше
        таймаут GPU порождал ещё один LLM-вызов и рекурсию с растущим
        промптом -- положительная обратная связь на перегруженном узле.

        Жёлтый уровень (WARNING): LLM_TIMEOUT / LLM_UNAVAILABLE. Счётчик
        transport_failures растёт, reflection_depth сбрасывается, status=ERROR
        (бот при этом продолжает принимать запросы: следующий успешный вызов
        вернёт status в READY), last_error = код + деталь."""
        is_timeout = isinstance(error, (asyncio.TimeoutError, LLMTimeoutError))
        if isinstance(error, asyncio.TimeoutError):
            reason = "LLM call exceeded timeout"
        else:
            reason = str(error) or type(error).__name__
        detail = f"Transport fault, reflection bypassed: {reason}"
        if is_timeout:
            code, marker = "LLM_TIMEOUT", log_service_code("LLM_TIMEOUT", detail)
        else:
            code, marker = "LLM_UNAVAILABLE", log_service_code("LLM_UNAVAILABLE", detail)
        self.registry.update_state("reflection_depth", 0)
        self.registry.increment_state("transport_failures")
        self.registry.update_state("status", "ERROR")
        self.registry.update_state("last_error", f"{code}: {reason}")
        return marker

    @staticmethod
    def _failure_signature(error: BaseException) -> str:
        return f"{type(error).__name__}:{str(error)[:200]}"

    def _stagnation_break(self, signature: str, streak: int) -> str:
        """Застой: одна и та же ошибка повторилась MAX_STAGNATION_CYCLES раз
        подряд -- очередной круг рефлексии заведомо не поможет. Жёлтый код.
        Инвариант состояния: СРАЗУ reflection_depth=0 и status=READY (бот
        готов к следующему запросу), затем счётчики и причина."""
        reason = f"identical failure repeated {streak}x: {signature}"
        self.registry.update_state("reflection_depth", 0)
        self.registry.update_state("status", "READY")
        marker = log_service_code("STAGNATION_BREAK", reason)
        self.registry.increment_state("stagnation_breaks")
        self.registry.update_state("last_stagnation_reason", reason)
        self.registry.update_state("last_error", f"STAGNATION_BREAK: {reason}")
        return marker

    async def _handle_failure(
        self, error: Exception, prompt: str, depth: int, trail: tuple = ()
    ) -> str:
        signature = self._failure_signature(error)
        trail = trail + (signature,)
        streak = 0
        for past in reversed(trail):
            if past != signature:
                break
            streak += 1
        if streak >= config.MAX_STAGNATION_CYCLES:
            return self._stagnation_break(signature, streak)

        if depth >= config.MAX_REFLECTION_DEPTH:
            marker = log_service_code(
                "RETRY_LIMIT_EXCEEDED", f"Reflection depth limit reached: {error}",
                critical=True,
            )
            self.registry.update_state("status", "ERROR")
            self.registry.update_state("last_error", f"RETRY_LIMIT_EXCEEDED: {error}")
            return marker

        self.registry.update_state("reflection_depth", depth + 1)
        logger.warning("Recovery attempt %d after error: %s", depth + 1, error)

        try:
            correction = await self._meta_reflect(error, prompt)
        except (asyncio.TimeoutError, LLMTimeoutError, LLMTransportError) as reflect_err:
            # Транспортный сбой МЕЖДУ основным вызовом и _meta_reflect --
            # это не провал восстановления, а недоступный узел: жёлтый
            # LLM_UNAVAILABLE/LLM_TIMEOUT, reflection_depth -> 0.
            return self._transport_failure(reflect_err)
        except Exception as reflect_err:
            marker = log_service_code(
                "RECOVERY_FAILED", f"Recovery step failed: {reflect_err}",
                critical=True, exc_info=True,
            )
            self.registry.update_state("status", "ERROR")
            self.registry.update_state("last_error", f"RECOVERY_FAILED: {reflect_err}")
            return marker

        return await self._process(
            f"ORIGINAL: {prompt} | CORRECTION: {correction}", depth + 1, trail
        )''',
)

GOV_HALT = Exact(
    "gov.halt",
    r'''        logger.critical(
            "HALT_CONDITION triggered (rule=%s) — blocking response, no correction attempted.",
            violation_id,
        )
        self.registry.update_state("last_compliance_status", "HALT")
        self.registry.increment_state("compliance_violations")
        self.monitor.record_event(ComplianceEvent.VIOLATION)
        return "[ERR: HALT_CONDITION]"''',
    r'''        marker = log_service_code(
            "HALT_CONDITION",
            f"triggered (rule={violation_id}) — blocking response, no correction attempted.",
            critical=True,
        )
        self.registry.update_state("last_compliance_status", "HALT")
        self.registry.increment_state("compliance_violations")
        self.monitor.record_event(ComplianceEvent.VIOLATION)
        return marker''',
)

GOV_ROUTE = Exact(
    "gov._route_to_llm",
    r'''            except httpx.HTTPStatusError as e:
                logger.error("LLM HTTP error: %s", e)
                status = getattr(e.response, "status_code", None)
                if isinstance(status, int) and status >= 500:
                    raise LLMTransportError("LLM provider failed (5xx)") from e
                raise LLMContentError("LLM provider rejected request") from e
            except httpx.RequestError as e:
                logger.error("LLM connection error: %s", e)
                raise LLMTransportError("LLM provider unreachable") from e''',
    r'''            except httpx.HTTPStatusError as e:
                status = getattr(e.response, "status_code", None)
                if isinstance(status, int) and status >= 500:
                    # транспорт -> жёлтый (WARNING), не считается watchdog'ом
                    logger.warning("LLM HTTP error (transport, status=%s): %s", status, e)
                    raise LLMTransportError("LLM provider failed (5xx)") from e
                logger.error("LLM HTTP error: %s", e)
                raise LLMContentError("LLM provider rejected request") from e
            except httpx.TimeoutException as e:
                logger.warning("LLM timeout: %s", e)
                raise LLMTimeoutError("LLM provider timed out") from e
            except httpx.RequestError as e:
                logger.warning("LLM connection error: %s", e)
                raise LLMTransportError("LLM provider unreachable") from e''',
)

GOVERNOR_EDITS = [
    GOV_IMPORTS, GOV_CLASSES_AND_CODES, GOV_INIT, GOV_DEGRADED, GOV_KERNEL_FAULT,
    GOV_DISCUSS, GOV_PROCESS_BLOCK, GOV_HALT, GOV_ROUTE,
]

# --------------------------------------------------------------------------
# registry.py / registry_schema.py
# --------------------------------------------------------------------------

REGISTRY_EDITS = [
    Exact(
        "registry.allowed_keys",
        '    "pending_tasks",\n})',
        '    "pending_tasks",\n'
        '    # PATCH_V2_STAGNATION: маркеры деградации (observability)\n'
        '    "transport_failures",\n'
        '    "stagnation_breaks",\n'
        '    "last_stagnation_reason",\n'
        '})',
    ),
    Exact(
        "registry.defaults",
        '            "pending_tasks": {},\n        }',
        '            "pending_tasks": {},\n'
        '            "transport_failures": 0,\n'
        '            "stagnation_breaks": 0,\n'
        '            "last_stagnation_reason": None,\n'
        '        }',
    ),
]

SCHEMA_EDITS = [
    Exact(
        "schema.entries",
        '    "pending_tasks": _validate_pending_tasks,\n}',
        '    "pending_tasks": _validate_pending_tasks,\n'
        '    # PATCH_V2_STAGNATION\n'
        '    "transport_failures": (int,),\n'
        '    "stagnation_breaks": (int,),\n'
        '    "last_stagnation_reason": (str, type(None)),\n'
        '}',
    ),
]

# --------------------------------------------------------------------------
# config.py
# --------------------------------------------------------------------------

CONFIG_EDITS = [
    Exact(
        "config.stagnation",
        'MAX_REFLECTION_DEPTH: int = int(os.getenv("MAX_REFLECTION_DEPTH", "3"))\n',
        'MAX_REFLECTION_DEPTH: int = int(os.getenv("MAX_REFLECTION_DEPTH", "3"))\n'
        '\n'
        '# PATCH_V2_STAGNATION: сколько ОДИНАКОВЫХ сбоев подряд (одна и та же\n'
        '# сигнатура ошибки) считается застоем рефлексии. Обязан быть меньше\n'
        '# MAX_REFLECTION_DEPTH, иначе защита от застоя недостижима (сработает\n'
        '# жёсткий лимит глубины раньше) -- проверяется инвариантом ниже.\n'
        'MAX_STAGNATION_CYCLES: int = int(os.getenv("MAX_STAGNATION_CYCLES", "2"))\n'
        '\n'
        '\n'
        'def check_stagnation_invariant(stagnation=None, depth=None) -> None:\n'
        '    """FATAL-стоп (как _require_env), если нарушено\n'
        '    1 <= MAX_STAGNATION_CYCLES < MAX_REFLECTION_DEPTH. Без аргументов\n'
        '    читает ТЕКУЩИЕ значения модуля (их можно подменить в тесте)."""\n'
        '    s = MAX_STAGNATION_CYCLES if stagnation is None else stagnation\n'
        '    d = MAX_REFLECTION_DEPTH if depth is None else depth\n'
        '    if not (1 <= s < d):\n'
        '        sys.exit(\n'
        '            f"FATAL: нарушен инвариант 1 <= MAX_STAGNATION_CYCLES ({s}) < "\n'
        '            f"MAX_REFLECTION_DEPTH ({d}). Проверьте .env."\n'
        '        )\n'
        '\n'
        '\n'
        'check_stagnation_invariant()\n',
    ),
    Exact(
        "config.watchdog_limit",
        'WATCHDOG_ERROR_RATE_LIMIT: int = int(os.getenv("WATCHDOG_ERROR_RATE_LIMIT", "2"))',
        '# PATCH_V2_STAGNATION: порог светофора -- SIGTERM на 6-й красной строке за окно\n'
        '# (проверка в watchdog: count > limit, limit=5 => срабатывает на 6-й).\n'
        'WATCHDOG_ERROR_RATE_LIMIT: int = int(os.getenv("WATCHDOG_ERROR_RATE_LIMIT", "5"))',
    ),
]

# --------------------------------------------------------------------------
# process_watchdog.py
# --------------------------------------------------------------------------

WATCHDOG_EDITS = [
    Exact("wd.import_re", "import os\nimport signal\n", "import os\nimport re\nimport signal\n"),
    Exact(
        "wd.helper",
        "def _parse_log_timestamp(line: str) -> Optional[float]:",
        '''# PATCH_V2_STAGNATION: светофор. Красными считаются ТОЛЬКО строки уровня
# ERROR/CRITICAL, определённого по полю "[LEVEL]" формата лога, а не по
# подстроке: жёлтая WARNING-строка, в тексте которой встретилось слово ERROR,
# раньше засчитывалась бы как красная. Строка без распознаваемого "[LEVEL]"
# (чужой формат) трактуется по-старому, по подстроке -- fail-safe.
RED_LEVELS = frozenset({"ERROR", "CRITICAL"})
_LEVEL_RE = re.compile(r"^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2},\\d{3} \\[([A-Z]+)\\]")


def _is_red_line(line: str) -> bool:
    m = _LEVEL_RE.match(line)
    if m:
        return m.group(1) in RED_LEVELS
    return "ERROR" in line or "CRITICAL" in line


def _parse_log_timestamp(line: str) -> Optional[float]:''',
    ),
    Exact(
        "wd.count",
        '                if "ERROR" not in line and "CRITICAL" not in line:\n'
        '                    continue\n',
        '                if not _is_red_line(line):\n'
        '                    continue\n',
    ),
]

# --------------------------------------------------------------------------
# test_governor.py (4 tests whose contract changed by design)
# --------------------------------------------------------------------------

TEST_GOVERNOR_EDITS = [
    Exact(
        "tg.bad_schema_tag",
        r'''def _mock_bad_schema_response():
    """Ответ без поля "response" -> LLMContentError (содержательный сбой)."""
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"error": "model not loaded"})
    return resp''',
        r'''def _mock_bad_schema_response(tag=""):
    """Ответ без поля "response" -> LLMContentError (содержательный сбой).

    PATCH_V2_STAGNATION: tag меняет ключ ответа, а значит и сигнатуру ошибки
    ("Unexpected response schema: [...]"). Одинаковые ошибки подряд теперь
    считаются застоем (STAGNATION_BREAK); чтобы дойти до лимита глубины
    (RETRY_LIMIT_EXCEEDED), ошибки в цепочке должны различаться."""
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={f"error{tag}": "model not loaded"})
    return resp''',
    ),
    Exact(
        "tg.retry_limit_distinct_errors",
        r'''        responses = []
        for i in range(config.MAX_REFLECTION_DEPTH):
            responses.append(_mock_bad_schema_response())
            responses.append(_mock_llm_response(f"correction{i}"))
        responses.append(_mock_bad_schema_response())''',
        r'''        responses = []
        for i in range(config.MAX_REFLECTION_DEPTH):
            responses.append(_mock_bad_schema_response(i))
            responses.append(_mock_llm_response(f"correction{i}"))
        responses.append(_mock_bad_schema_response(config.MAX_REFLECTION_DEPTH))''',
    ),
    Range(
        "tg.recovery_failed_content",
        "    def test_recovery_failed_when_meta_reflect_itself_fails(self, governor_and_client):",
        "        assert mock_client.post.call_count == 2",
        r'''    def test_recovery_failed_when_meta_reflect_itself_fails(self, governor_and_client):
        """Основной вызов: содержательный сбой. Сам meta_reflect тоже
        содержательно сбоит (не транспорт) -> RECOVERY_FAILED сразу, без
        дальнейшей рекурсии. Транспортный сбой в meta_reflect теперь НЕ
        RECOVERY_FAILED, а жёлтый LLM_UNAVAILABLE (см. test_stagnation_guard)."""
        import asyncio
        gov, mock_client = governor_and_client
        mock_client.post.side_effect = [
            _mock_bad_schema_response(),
            _mock_bad_schema_response("_meta"),
        ]

        result = asyncio.run(gov.execute_request("вопрос"))

        assert result == "[ERR: RECOVERY_FAILED]"
        assert gov.registry.get_state("status") == "ERROR"
        assert mock_client.post.call_count == 2''',
    ),
    Range(
        "tg.timeout_code",
        "    def test_timeout_skips_reflection_and_judge(self, governor_and_client):",
        "        assert mock_client.post.call_count == 1",
        r'''    def test_timeout_skips_reflection_and_judge(self, governor_and_client):
        import asyncio
        gov, mock_client = governor_and_client
        mock_client.post.side_effect = asyncio.TimeoutError()

        result = asyncio.run(gov.execute_request("вопрос"))

        # PATCH_V2_STAGNATION: таймаут -- отдельный жёлтый код LLM_TIMEOUT
        assert result == "[ERR: LLM_TIMEOUT]"
        assert mock_client.post.call_count == 1''',
    ),
]

# --------------------------------------------------------------------------
# bot.py (optional)
# --------------------------------------------------------------------------

_HISTORY_RE = re.compile(
    r'(logger\.warning\(\s*f?")((?:(?!\[HISTORY_SKIPPED\])[^"\n])*exchange not persisted[^"\n]*")'
)


def _bot_history_marker(text: str) -> str:
    new, n = _HISTORY_RE.subn(lambda m: m.group(1) + "[HISTORY_SKIPPED] " + m.group(2), text)
    if n == 0 and "[HISTORY_SKIPPED]" not in text:
        print("  note: bot.py has no 'exchange not persisted' warning -- history-skip marker "
              "NOT added (add '[HISTORY_SKIPPED] ' to that logger.warning by hand).")
    return new


BOT_EDITS = [
    Exact(
        "bot.status_block",
        '        f"INPUT FILTERING (sliding window, last {input_monitor.window_size} checked)\\n"',
        '        "DEGRADATION MARKERS\\n"\n'
        "        f\"transport_failures: {state.get('transport_failures', 0)}\\n\"\n"
        "        f\"stagnation_breaks: {state.get('stagnation_breaks', 0)}\\n\"\n"
        "        f\"last_stagnation_reason: {state.get('last_stagnation_reason')}\\n\"\n"
        '        "\\n"\n'
        '        f"INPUT FILTERING (sliding window, last {input_monitor.window_size} checked)\\n"',
    ),
    Func("bot.history_marker", _bot_history_marker),
]

# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

@dataclass
class FileSpec:
    name: str
    edits: list
    marker: str
    required: bool = True


PLAN = [
    FileSpec("governor.py", GOVERNOR_EDITS, "PATCH_V2_STAGNATION"),
    FileSpec("registry.py", REGISTRY_EDITS, "PATCH_V2_STAGNATION"),
    FileSpec("registry_schema.py", SCHEMA_EDITS, "PATCH_V2_STAGNATION"),
    FileSpec("config.py", CONFIG_EDITS, "PATCH_V2_STAGNATION"),
    FileSpec("process_watchdog.py", WATCHDOG_EDITS, "PATCH_V2_STAGNATION"),
    FileSpec("test_governor.py", TEST_GOVERNOR_EDITS, "PATCH_V2_STAGNATION"),
    FileSpec("bot.py", BOT_EDITS, "DEGRADATION MARKERS", required=False),
]


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def read_text(path: Path):
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    crlf = "\r\n" in text
    lone_lf = text.replace("\r\n", "").count("\n")
    if crlf and lone_lf:
        print(f"  warning: {path.name} has MIXED line endings; they will be normalised to CRLF")
    return text.replace("\r\n", "\n"), bom, crlf


def encode_text(text: str, bom: bool, crlf: bool) -> bytes:
    if crlf:
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def next_backup(path: Path) -> Path:
    cand = path.with_name(path.name + BAK_SUFFIX)
    i = 1
    while cand.exists():
        i += 1
        cand = path.with_name(f"{path.name}{BAK_SUFFIX}{i}")
    return cand


def default_src() -> Path:
    here = Path(__file__).resolve().parent
    return here / "src" if (here / "src").is_dir() else here


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Patch v2: stagnation guard / log traffic light")
    ap.add_argument("--src", type=Path, default=None, help="directory with the .py sources")
    ap.add_argument("--apply", action="store_true", help="write files (default: dry-run)")
    ap.add_argument("--diff-out", type=Path, default=None, help="write unified diff here")
    args = ap.parse_args(argv)

    src = (args.src or default_src()).resolve()
    print(f"source dir : {src}")
    print(f"mode       : {'APPLY' if args.apply else 'DRY-RUN (nothing is written)'}")

    results = []   # (spec, path, old_text, new_text, bom, crlf, status)
    failed_required = False

    for spec in PLAN:
        path = src / spec.name
        if not path.is_file():
            msg = "MISSING"
            print(f"[{msg}] {spec.name}")
            if spec.required:
                failed_required = True
            continue
        old, bom, crlf = read_text(path)
        if spec.marker in old:
            print(f"[SKIP ] {spec.name}: already patched")
            continue
        text = old
        try:
            for ed in spec.edits:
                text = ed.apply(text)
            compile(text, spec.name, "exec")
        except (PatchError, SyntaxError) as e:
            print(f"[FAIL ] {spec.name}: {e}")
            if spec.required:
                failed_required = True
            else:
                print(f"        optional file skipped")
            continue
        added = sum(1 for _ in difflib.unified_diff(old.splitlines(), text.splitlines(), n=0))
        print(f"[OK   ] {spec.name}: {len(spec.edits)} edits, diff lines={added}")
        results.append((spec, path, old, text, bom, crlf))

    if failed_required:
        print("\nABORT: a required file failed its anchors/syntax check. NOTHING was written.")
        print("Send the failing file(s) so the patch can be rebased on your actual code.")
        return 2

    if args.diff_out:
        chunks = []
        for spec, path, old, text, _bom, _crlf in results:
            chunks.extend(difflib.unified_diff(
                old.splitlines(keepends=True), text.splitlines(keepends=True),
                fromfile=f"a/{spec.name}", tofile=f"b/{spec.name}",
            ))
        args.diff_out.write_text("".join(chunks), encoding="utf-8")
        print(f"diff written: {args.diff_out}")

    if not results:
        print("nothing to do.")
        return 0

    if not args.apply:
        print("\nDRY-RUN complete. Re-run with --apply to write (backups are created).")
        return 0

    for spec, path, old, text, bom, crlf in results:
        bak = next_backup(path)
        bak.write_bytes(path.read_bytes())
        tmp = path.with_name(path.name + ".tmp_patch")
        tmp.write_bytes(encode_text(text, bom, crlf))
        os.replace(tmp, path)
        print(f"[WRITE] {path.name}  (backup: {bak.name})")

    print("\nDONE. Next: update_manifest.ps1, then")
    print("  python -m pytest src\\test_stagnation_guard.py src\\test_governor.py -q")
    return 0


if __name__ == "__main__":
    sys.exit(main())
