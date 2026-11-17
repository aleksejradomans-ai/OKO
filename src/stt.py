"""stt.py — голос -> текст локально (faster-whisper), без облака (28.10.2026).

Голосовое из Telegram (OGG/Opus) -> текст по-русски. Модель грузится один раз, при первом голосовом
(первое распознавание дольше — загрузка), дальше держится в памяти процесса бота.
Устройство: OKO_STT_DEVICE = auto | cuda | cpu (по умолчанию auto). auto пробует видеокарту и ПРОВЕРЯЕТ её
пробным распознаванием (на Windows ctranslate2 без библиотек cuBLAS/cuDNN грузит модель, но падает на первом
распознавании), при сбое — процессор int8. Модель: OKO_STT_MODEL (по умолчанию small).
Подсказка (initial_prompt) смещает распознавание к словам команд: короткая фраза «вниз три» без неё
легко становится «вниз, тре».
Текст распознавания в журнал не пишется (только длина) — тот же принцип, что для сообщений.
"""
from __future__ import annotations

import av_compat
av_compat.apply()  # faster-whisper 1.2.1 + PyAV 19: metadata_errors (см. av_compat.py)


import logging
import os
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger("oko.stt")

PROMPT = ("Команды телевизору: вверх, вниз, влево, вправо, ок, назад, домой, меню, инфо, "
          "запомни, открой, забудь, хватит, громче, тише, подсветка, печать. Два, три, четыре, пять.")


class SttUnavailable(RuntimeError):
    """Распознавание речи недоступно в этой среде (нет библиотеки/модели) — текст для человека."""


_model = None
_device = None
_lock = threading.Lock()          # одно распознавание за раз
_load_lock = threading.Lock()     # одна загрузка за раз (30.10: прогрев и первое голосовое грузили параллельно)
_cuda_failed = False              # CUDA уже не прошла в этом процессе — повторно не пробуем
LOCK_WAIT_S = float(os.getenv("OKO_STT_LOCK_WAIT_S", "45"))


def _default_factory(name: str, device: str, compute: str):
    from faster_whisper import WhisperModel
    return WhisperModel(name, device=device, compute_type=compute)


def _probe(model) -> None:
    """Пробное распознавание 0,5 с тишины — проверка, что устройство реально работает."""
    import numpy as np
    segs, _ = model.transcribe(np.zeros(8000, dtype="float32"), language="ru", beam_size=1)
    list(segs)


def load(factory: Callable | None = None) -> tuple[object, str]:
    """-> (модель, «cuda/float16» | «cpu/int8»). Кэшируется.

    30.10 13:52 (снимок py-spy): прогрев (`bot._stt_warmup`, без блокировки) и первое голосовое (`transcribe`)
    загружали модель ОДНОВРЕМЕННО; второй поток навсегда повис в пробе (`_probe`), бот перестал отвечать.
    Теперь загрузка — под своей блокировкой с повторной проверкой: второй поток ждёт и берёт готовую модель.
    """
    if _model is not None:
        return _model, _device
    with _load_lock:
        if _model is not None:
            return _model, _device
        return _load_locked(factory)


def _load_locked(factory: Callable | None) -> tuple[object, str]:
    global _model, _device, _cuda_failed
    try:
        import faster_whisper  # noqa: F401
    except ImportError as e:
        if factory is None:
            raise SttUnavailable("faster-whisper не установлен в среде бота") from e
    make = factory or _default_factory
    name = os.getenv("OKO_STT_MODEL", "small")
    want = os.getenv("OKO_STT_DEVICE", "auto").lower()
    attempts = [("cuda", "float16"), ("cpu", "int8")] if want == "auto" else \
        [("cuda", "float16")] if want == "cuda" else [("cpu", "int8")]
    if factory is None and _cuda_failed and len(attempts) > 1:
        attempts = [a for a in attempts if a[0] != "cuda"]
    errors = []
    for device, compute in attempts:
        try:
            m = make(name, device, compute)
            _probe(m)
        except Exception as e:  # ctranslate2/CUDA ошибки разнотипны
            errors.append(f"{device}: {type(e).__name__}: {str(e)[:120]}")
            logger.warning("[STT] %s/%s недоступно: %s", device, compute, e)
            if device == "cuda" and factory is None:
                _cuda_failed = True
            continue
        _model, _device = m, f"{device}/{compute}"
        logger.info("[STT] модель %s загружена на %s", name, _device)
        return _model, _device
    raise SttUnavailable("модель не загрузилась: " + "; ".join(errors))


def transcribe(path: str | Path, factory: Callable | None = None) -> str:
    """Файл голосового -> текст (пустая строка, если речи нет)."""
    # одна модель на процесс; ctranslate2 не обещает потокобезопасность. Ожидание ограничено: зависшее
    # предыдущее распознавание не должно навсегда запирать следующие голосовые.
    if not _lock.acquire(timeout=LOCK_WAIT_S):
        raise SttUnavailable("предыдущее голосовое ещё распознаётся — повтори чуть позже")
    try:
        model, _ = load(factory)
        segs, _info = model.transcribe(str(path), language="ru", beam_size=5, vad_filter=True,
                                       initial_prompt=PROMPT, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segs).strip()
    finally:
        _lock.release()
    logger.info("[STT] распознано символов: %d", len(text))
    return text


def device() -> str | None:
    return _device


def reset() -> None:
    """Для тестов: забыть загруженную модель."""
    global _model, _device, _cuda_failed
    _model, _device, _cuda_failed = None, None, False
