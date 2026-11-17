"""av_compat.py — совместимость faster-whisper и PyAV для распознавания голоса (29.10.2026).

Причина (CONFIRMED 29.10 15:09 пробой на python службы): faster-whisper 1.2.1 декодирует аудио через
`av.open(..., metadata_errors="ignore")`, а PyAV 19.0.2 этот аргумент не принимает:
`open() got an unexpected keyword argument 'metadata_errors'` — отсюда «Не получилось распознать: TypeError»
на каждом голосовом 28.10 21:28–22:06. С 11:23 29.10 (временная подмена в stt.py) голос распознаётся.

Подмена включается ТОЛЬКО если установленный PyAV действительно отвергает аргумент (проба на пустых байтах).
После обновления faster-whisper или PyAV до совместимой пары она сама станет пустой операцией.
Повторный вызов apply() не оборачивает av.open второй раз.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger("oko.voice")


def rejects_metadata_errors(av_mod) -> bool:
    """True — PyAV не знает аргумент metadata_errors (TypeError с его именем)."""
    try:
        c = av_mod.open(io.BytesIO(b""), metadata_errors="ignore")
    except TypeError as e:
        return "metadata_errors" in str(e)
    except Exception:              # аргумент принят, пустые байты отвергнуты как данные — совместимость не нужна
        return False
    try:
        c.close()
    except Exception:
        pass
    return False


def apply(av_mod=None) -> bool:
    """Включает совместимость при необходимости. True — подмена активна."""
    if av_mod is None:
        import av as av_mod
    if getattr(av_mod.open, "_oko_compat", False):
        return True
    if not rejects_metadata_errors(av_mod):
        return False
    original = av_mod.open

    def open_compat(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return original(*args, **kwargs)

    open_compat._oko_compat = True
    av_mod.open = open_compat
    logger.warning("[VOICE] PyAV %s не принимает metadata_errors — включена совместимость для faster-whisper",
                   getattr(av_mod, "__version__", "?"))
    return True
