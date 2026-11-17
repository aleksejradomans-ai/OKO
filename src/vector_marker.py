"""vector_marker.py — метрика S (векторный маркер), режим PASSIVE_OBSERVATION.
ТЗ: claude/TZ_vector_marker_S_2026-10-21.md.

    s_identity = cos(emb(ответ), emb(identity.md))      — близость к роли системы
    s_manifest = cos(emb(ответ), emb(правила манифеста)) — близость к ограничениям

Выход — одна INFO-строка на ответ:
    [VECTOR_S] point=B s_identity=0.731 s_manifest=0.612 id=<hash8> man=<hash8> trunc=0 ms=140
Сбой или таймаут — одна WARNING-строка "[VECTOR_S] point=B fail=<Тип> ...", ответ не меняется.

X3: метрика ничего не решает. TransportGate, CircuitBreaker и судья её не читают;
сбой эмбеддинга не считается сбоем транспорта (F1: метрика — не основной путь).
Порогов нет до калибровки.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
from typing import Awaitable, Callable, Optional, Sequence

import config

# Бюджет эмбеддинга ответа. Был 3.0 (решение Оператора, 21.10); замер 26.10
# показал холодную загрузку bge-m3 4601 мс -> 29 fail=TimeoutError из 59 замеров
# за неделю (тёплая 213-225 мс). Значение из .env (OKO_EMBED_TIMEOUT_S), откат
# без правки кода. Читается на верхнем уровне: monkeypatch в тестах подменяет
# атрибут модуля, observe() берёт его по имени в момент вызова.
EMBED_TIMEOUT_S = float(getattr(config, "OKO_EMBED_TIMEOUT_S", 6.0))
CORE_TIMEOUT_S = 20.0    # опорные векторы: первый расчёт может застать холодную загрузку bge-m3
QUEUE_WAIT_S = 30.0      # ожидание общего GPU-семафора; дольше — замер пропускается
MAX_CHARS = 2000         # Ollama-батч 2048 токенов (ошибка 500 на длинном вводе); 2000 символов — с запасом

Embedder = Callable[[str], Awaitable[Sequence[float]]]


class QueueBusy(RuntimeError):
    """GPU-семафор занят дольше QUEUE_WAIT_S — замер пропущен, генерация важнее."""


def manifest_text(manifesto) -> str:
    """Детерминированная склейка манифеста: правила POL_* по id + PRIMARY_OBJECTIVE.
    Источник — только src/manifesto.py (копии текста в отдельном файле нет)."""
    rules = sorted(manifesto.rules_by_id().values(), key=lambda r: r.id)
    lines = [f"{r.id}: {r.text}" for r in rules]
    lines.append(f"OBJECTIVE: {manifesto.PRIMARY_OBJECTIVE}")
    return "\n".join(lines)


def hash8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or len(a) != len(b):
        raise ValueError(f"dim mismatch {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        raise ValueError("zero vector")
    return dot / (na * nb)


def truncate(text: str, limit: int = MAX_CHARS) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


class VectorMarker:
    """Опорные векторы считаются лениво при первом замере и кэшируются по SHA256
    источника. Смена источника -> пересчёт + INFO "[VECTOR_S] core rebuilt".
    Хэши пишутся в каждую строку замера: ряды до и после смены несопоставимы."""

    def __init__(
        self,
        identity_text: Optional[str],
        manifesto,
        embed: Embedder,
        logger,
        gpu: Optional[asyncio.Semaphore] = None,
        model: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._embed_fn = embed
        self._log = logger
        self._gpu = gpu
        self._model = model
        self._clock = clock
        self.identity_text = identity_text or None
        self.manifest = manifest_text(manifesto)
        self._core: dict[str, tuple[str, Sequence[float]]] = {}   # "id"/"man" -> (hash8, вектор)
        self._core_lock: Optional[asyncio.Lock] = None

    async def _embed(self, text: str, timeout: float) -> Sequence[float]:
        if self._gpu is None:
            return await asyncio.wait_for(self._embed_fn(text), timeout)
        try:
            await asyncio.wait_for(self._gpu.acquire(), QUEUE_WAIT_S)
        except asyncio.TimeoutError:
            raise QueueBusy(f"gpu queue > {QUEUE_WAIT_S:.0f}s") from None
        try:
            return await asyncio.wait_for(self._embed_fn(text), timeout)
        finally:
            self._gpu.release()

    def core_hashes(self) -> tuple[str, str]:
        id_h = hash8(self.identity_text) if self.identity_text else "none"
        return id_h, hash8(self.manifest)

    async def _ensure_core(self) -> None:
        if self._core_lock is None:
            self._core_lock = asyncio.Lock()
        async with self._core_lock:
            rebuilt = False
            for key, src in (("id", self.identity_text), ("man", self.manifest)):
                if src is None:
                    self._core.pop(key, None)
                    continue
                h = hash8(src)
                cur = self._core.get(key)
                if cur is not None and cur[0] == h:
                    continue
                body, _ = truncate(src)
                self._core[key] = (h, await self._embed(body, CORE_TIMEOUT_S))
                rebuilt = True
            if rebuilt:
                id_h, man_h = self.core_hashes()
                self._log.info("[VECTOR_S] core rebuilt id=%s man=%s model=%s",
                               id_h, man_h, self._model or "?")

    async def observe(self, text: str, point: str) -> Optional[dict]:
        """Никогда не бросает (кроме отмены задачи при остановке бота)."""
        t0 = self._clock()
        try:
            await self._ensure_core()
            body, trunc = truncate(text)
            vec = await self._embed(body, EMBED_TIMEOUT_S)
            ident = self._core.get("id")
            s_id = cosine(vec, ident[1]) if ident else None
            s_man = cosine(vec, self._core["man"][1])
            id_h, man_h = self.core_hashes()
            ms = int((self._clock() - t0) * 1000)
            self._log.info(
                "[VECTOR_S] point=%s s_identity=%s s_manifest=%.3f id=%s man=%s trunc=%d ms=%d",
                point, "na" if s_id is None else f"{s_id:.3f}", s_man, id_h, man_h, int(trunc), ms,
            )
            return {"s_identity": s_id, "s_manifest": s_man, "trunc": trunc, "ms": ms}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.warning("[VECTOR_S] point=%s fail=%s %s", point, type(e).__name__, str(e)[:160])
            return None
