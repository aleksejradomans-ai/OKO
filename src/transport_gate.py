"""transport_gate.py -- TRANSPORT_GATE (M5a, 19.10): предохранитель СВЯЗИ с Ollama.

Отдельно от CircuitBreaker (тот -- про качество ответов по вердиктам судьи,
X3). Этот -- только про транспорт: таймаут, обрыв, 5xx.

CLOSED    обычная работа; считаются транспортные сбои ПОДРЯД (любой ответ
          Ollama, даже 4xx/битый JSON, обнуляет счёт -- узел жив).
OPEN      после THRESHOLD сбоев подряд: запросы в Ollama не отправляются
          (вызывающий код сразу получает отказ -> жёлтый LLM_UNAVAILABLE,
          без ожидания LLM_TIMEOUT). Длительность -- экспонента
          BASE_S * 2**step, потолок MAX_S, разброс +-JITTER.
HALF_OPEN по истечении паузы ОДИН запрос (под lock) делает дешёвую пробу
          (GET /api/version у вызывающего, не генерацию). Успех -> CLOSED,
          сброс паузы. Неудача -> OPEN со следующей ступенью паузы.

Сбой считается, только если запрос реально ушёл в Ollama: таймаут ожидания
очереди GPU-семафора -- не сбой узла (вызывающий код это различает).
Переходы состояний -- одна строка лога (WARNING/INFO), никогда ERROR.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, Optional

THRESHOLD = 3
BASE_S = 5.0
MAX_S = 300.0
JITTER = 0.2
PROBE_TIMEOUT_S = 3.0

CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"


def _now() -> float:
    """Единственная точка чтения времени (тесты подменяют ручными часами)."""
    return time.monotonic()


def _jitter_factor(j: float) -> float:
    """Множитель разброса паузы (тесты подменяют на 1.0)."""
    return 1.0 + random.uniform(-j, j)


class TransportGate:
    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        threshold: int = THRESHOLD,
        base_s: float = BASE_S,
        max_s: float = MAX_S,
        jitter: float = JITTER,
    ) -> None:
        self._log = logger or logging.getLogger("kernel.transport_gate")
        self._threshold = max(1, threshold)
        self._base_s = base_s
        self._max_s = max_s
        self._jitter = jitter
        self._fails = 0
        self._step = 0
        self._open_until: Optional[float] = None
        self._lock = asyncio.Lock()

    # --- наблюдение ---------------------------------------------------
    @property
    def state(self) -> str:
        if self._open_until is None:
            return CLOSED
        return OPEN if _now() < self._open_until else HALF_OPEN

    def retry_in(self) -> float:
        """Секунд до следующей пробы; 0 -- предохранитель закрыт или проба уже разрешена."""
        if self._open_until is None:
            return 0.0
        return max(0.0, self._open_until - _now())

    # --- переходы -----------------------------------------------------
    def _pause(self) -> float:
        return min(self._max_s, self._base_s * (2 ** self._step)) * _jitter_factor(self._jitter)

    def _open(self, reason: str) -> None:
        pause = self._pause()
        self._open_until = _now() + pause
        self._log.warning(
            "[TRANSPORT_GATE] -> OPEN (step=%d, pause=%.0fs): %s",
            self._step, pause, reason,
        )

    def record_success(self) -> None:
        """Ollama ответила (любым HTTP-ответом) -- узел жив."""
        if self._open_until is not None:
            self._log.info("[TRANSPORT_GATE] -> CLOSED (Ollama responds again)")
        self._fails = 0
        self._step = 0
        self._open_until = None

    def record_failure(self, reason: str) -> None:
        """Транспортный сбой запроса, реально ушедшего в Ollama."""
        if self._open_until is not None:
            return  # уже открыт; запросы сюда не доходят, кроме гонок -- игнор
        self._fails += 1
        if self._fails >= self._threshold:
            self._step = 0
            self._open(f"{self._fails} transport failures in a row, last: {reason}")

    async def allow(self, probe: Callable[[], Awaitable[bool]]) -> bool:
        """True -- можно отправлять запрос в Ollama. В OPEN после паузы ровно
        один вызывающий выполняет probe(); остальные ждут lock и видят итог."""
        if self._open_until is None:
            return True
        async with self._lock:
            if self._open_until is None:
                return True
            if _now() < self._open_until:
                return False
            try:
                ok = bool(await probe())
            except Exception as e:  # проба не должна ронять запрос
                ok = False
                self._log.warning("[TRANSPORT_GATE] probe raised %s: %s", type(e).__name__, e)
            if ok:
                self.record_success()
                return True
            self._step += 1
            self._open("probe failed")
            return False
