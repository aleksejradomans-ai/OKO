"""stt_probe.py — замер локального распознавания речи (faster-whisper) на этой машине (28.10.2026).

Что меряется, по шагам, каждый отдельно:
  1. GPU: грузится ли модель на CUDA (ctranslate2 на Windows требует библиотеки cuBLAS/cuDNN — их может не быть);
  2. CPU int8: запасной путь, работает всегда;
  3. время загрузки модели и время распознавания (на синтетическом сигнале — это замер скорости, не точности;
     точность проверяется живым голосовым из Telegram после подключения).
Первая загрузка модели скачивает её с huggingface (small ≈ 480 МБ) в кэш пользователя.
Ничего не меняет в проекте.

Запуск:  python stt_probe.py [--model small]
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path


def _tone_wav(path: Path, seconds: float = 3.0, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * i / rate)))
                          for i in range(int(seconds * rate)))
        w.writeframes(frames)


def _try(model_name: str, device: str, compute: str, wav: Path) -> None:
    from faster_whisper import WhisperModel
    t0 = time.monotonic()
    try:
        m = WhisperModel(model_name, device=device, compute_type=compute)
    except Exception as e:
        print(f"[{device}/{compute}] загрузка НЕ удалась: {type(e).__name__}: {str(e)[:200]}")
        return
    t1 = time.monotonic()
    try:
        segs, info = m.transcribe(str(wav), language="ru", vad_filter=False, beam_size=1)
        text = " ".join(s.text for s in segs).strip()
    except Exception as e:
        print(f"[{device}/{compute}] распознавание НЕ удалось: {type(e).__name__}: {str(e)[:200]}")
        return
    t2 = time.monotonic()
    print(f"[{device}/{compute}] OK: загрузка {t1 - t0:.1f} с, распознавание 3 с звука {t2 - t1:.2f} с, "
          f"текст «{text[:60]}»")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="small")
    a = ap.parse_args()
    try:
        import faster_whisper
    except ImportError:
        print("СТОП: faster-whisper не установлен в этой среде")
        return 4
    print(f"faster-whisper {faster_whisper.__version__}, модель {a.model}")
    wav = Path(tempfile.gettempdir()) / "oko_stt_probe.wav"
    _tone_wav(wav)
    _try(a.model, "cuda", "float16", wav)
    _try(a.model, "cpu", "int8", wav)
    return 0


if __name__ == "__main__":
    sys.exit(main())
