#!/usr/bin/env python3
"""apply_breaker_wiring.py -- CIRCUIT_BREAKER_WIRED для src/governor.py.

Заменяет самодельную probe-логику SYSTEM_DEGRADED (_maybe_allow_probe,
_degraded_since) на src/circuit_breaker.py::CircuitBreaker:
  * OPEN      -> [ERR: SYSTEM_DEGRADED] без вызова LLM (WARNING, жёлтый свет);
  * HALF_OPEN -> ровно одна проба (флаг _probe_in_flight под _probe_lock),
                 исход пишется в record_probe_result;
  * свойство governor.breaker_state (строка) -> блок CIRCUIT BREAKER в /status.
Порог 0.5 / min_sample 10 / cooldown PROBE_COOLDOWN_S сохраняют прежние границы.

Dry-run по умолчанию. --apply пишет файл, бэкап: governor.py.bak_breaker
Использование:  python apply_breaker_wiring.py [--src C:\\oko\\src] [--apply]
"""
from __future__ import annotations

import argparse
import difflib
import pathlib
import re
import shutil
import sys

MARKER = "CIRCUIT_BREAKER_WIRED"

IMPORT_OLD = "from src.registry import Registry\n"
IMPORT_NEW = IMPORT_OLD + "from src.circuit_breaker import BreakerState, CircuitBreaker\n"

INIT_OLD = (
    "        self._degraded_since: Optional[float] = None\n"
    "        self._probe_lock = asyncio.Lock()\n"
)
INIT_NEW = '''        # CIRCUIT_BREAKER_WIRED: probe-механизм SYSTEM_DEGRADED теперь живёт в
        # CircuitBreaker (комментарий выше описывает прежнюю реализацию через
        # _degraded_since, которой больше нет). Порог 0.5 сохраняет прежнюю
        # границу деградации; _probe_lock защищает флаг единственной пробы.
        self._breaker = CircuitBreaker(
            self.monitor, error_threshold=0.5, min_sample=10, cooldown_s=PROBE_COOLDOWN_S,
        )
        self._probe_in_flight = False
        self._probe_lock = asyncio.Lock()
'''

PROP_ANCHOR = "    @staticmethod\n    def _load_identity() -> Optional[str]:\n"
PROP_NEW = '''    @property
    def breaker_state(self) -> str:
        """CIRCUIT_BREAKER_WIRED: состояние предохранителя для /status
        (bot.py читает его через getattr). Только наблюдение, не для решений."""
        return self._breaker.state_hint.value

''' + PROP_ANCHOR

GATE_RE = re.compile(
    r"        if self\.monitor\.get_sample_size\(\) >= 10 and self\.monitor\.get_error_rate\(\) > 0\.5:\n"
    r".*?"
    r"            self\._degraded_since = None\n",
    re.S,
)
GATE_NEW = '''        # CIRCUIT_BREAKER_WIRED: CLOSED/OPEN/HALF_OPEN ведёт CircuitBreaker поверх
        # того же ComplianceMonitor. OPEN -- отказ без вызова LLM (жёлтый
        # SYSTEM_DEGRADED); HALF_OPEN -- ровно одна проба, исход фиксируется.
        breaker_state = await self._breaker.guard()
        if breaker_state == BreakerState.OPEN:
            return log_service_code(
                "SYSTEM_DEGRADED",
                f"Circuit breaker OPEN (violation rate {self.monitor.get_error_rate() * 100:.0f}%), "
                "refusing request",
            )
        if breaker_state == BreakerState.HALF_OPEN:
            async with self._probe_lock:
                if self._probe_in_flight:
                    return log_service_code(
                        "SYSTEM_DEGRADED",
                        "Circuit breaker HALF_OPEN: probe already in flight, refusing request",
                    )
                self._probe_in_flight = True
            logger.info(
                "SYSTEM_DEGRADED probe: allowing one request through to test recovery"
            )
            final = ""
            try:
                final = await self._run_request(user_input, context)
            finally:
                self._breaker.record_probe_result(self._probe_succeeded(final))
                self._probe_in_flight = False
            return final
        return await self._run_request(user_input, context)

    def _probe_succeeded(self, final: str) -> bool:
        """Проба успешна, только если ответ не пуст, не маркер ошибки и судья
        вынес PASS."""
        return (
            bool(final)
            and not final.startswith("[ERR:")
            and self.registry.get_state("last_compliance_status") == "PASS"
        )

    async def _run_request(self, user_input: str, context: Optional[str]) -> str:
        """Основной конвейер execute_request после проверки предохранителя."""
'''

PROBE_RE = re.compile(
    r"    async def _maybe_allow_probe\(self\) -> bool:\n.*?(?=    async def execute_discuss_request\()",
    re.S,
)


def apply_edits(text: str) -> str:
    if MARKER in text:
        raise SystemExit("[SKIP] маркер CIRCUIT_BREAKER_WIRED уже есть -- патч применён")

    def exact(name, old, new):
        nonlocal text
        n = text.count(old)
        if n != 1:
            raise SystemExit(f"[FAIL] якорь '{name}': найдено {n}, ожидалось 1")
        text = text.replace(old, new)
        print(f"[OK] {name}")

    def regex(name, rx, new):
        nonlocal text
        text, n = rx.subn(lambda m: new, text)
        if n != 1:
            raise SystemExit(f"[FAIL] якорь '{name}': найдено {n}, ожидалось 1")
        print(f"[OK] {name}")

    exact("import CircuitBreaker", IMPORT_OLD, IMPORT_NEW)
    exact("__init__: предохранитель вместо _degraded_since", INIT_OLD, INIT_NEW)
    exact("свойство breaker_state", PROP_ANCHOR, PROP_NEW)
    regex("execute_request: ворота на guard()", GATE_RE, GATE_NEW)
    regex("удаление _maybe_allow_probe", PROBE_RE, "")
    exact(
        "устаревший комментарий",
        "Теперь _maybe_allow_probe() раз в",
        "Теперь probe (CircuitBreaker.HALF_OPEN, ранее _maybe_allow_probe()) раз в",
    )
    if "self._degraded_since" in text or "self._maybe_allow_probe" in text:
        raise SystemExit("[FAIL] в файле остались обращения к старой probe-логике")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\oko\src")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    path = pathlib.Path(args.src) / "governor.py"
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    crlf = b"\r\n" in raw
    original = raw.decode("utf-8").replace("\r\n", "\n")

    patched = apply_edits(original)
    compile(patched, str(path), "exec")

    diff = list(difflib.unified_diff(
        original.splitlines(), patched.splitlines(), "a/governor.py", "b/governor.py", lineterm="",
    ))
    plus = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    minus = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    print(f"diff: +{plus} -{minus} | CRLF={crlf} BOM={bom}")

    if not args.apply:
        print("dry-run: файл не изменён. Для записи: --apply")
        return 0

    backup = path.with_name(path.name + ".bak_breaker")
    shutil.copy2(path, backup)
    out = patched.replace("\n", "\r\n") if crlf else patched
    data = out.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    print(f"written: {path} | backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
