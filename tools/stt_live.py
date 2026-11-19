"""stt_live.py -- замер STT на живой фразе (B18b, шаг Оператора после GREEN). Тот же путь, что у голоса окна:
микрофон (shell_client.Recorder, 16 кГц моно, автостоп 30 с) -> %TEMP%\\oko_voice\\<uuid>.wav ->
shell_voice.transcribe_and_delete(путь, stt.transcribe) -- проверка пути, тишины, общая модель stt, файл удаляется
в finally. Отдельный процесс: модель грузится заново (load_s -- отдельной строкой; в боте она уже в памяти).

    python -I tools/stt_live.py [--seconds 30]

stdout: строки STTLIVE ... (метрики, без текста) и одна строка TEXT ... (распознанный текст -- только для экрана;
обёртка в отчёт её не пишет). Сеть не нужна, служб не трогает.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import shell_client as sc      # noqa: E402
import shell_voice as sv       # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=sc.MAX_REC_S)
    a = ap.parse_args(argv)
    import stt                 # noqa: E402 -- faster-whisper грузится только здесь
    t0 = time.monotonic()
    try:
        _m, dev = stt.load()
    except stt.SttUnavailable as e:
        print(f"STTLIVE load failed={type(e).__name__}")
        return 2
    print(f"STTLIVE load_s={time.monotonic() - t0:.2f} device={dev}")
    rec = sc.Recorder(max_s=min(a.seconds, sc.MAX_REC_S))
    stop = threading.Event()
    print(f"STTLIVE say the phrase now; Enter = stop (auto stop {rec.max_s:.0f} s)", flush=True)
    rec.start()
    threading.Thread(target=lambda: (sys.stdin.readline(), stop.set()), daemon=True).start()
    while not stop.is_set() and not rec.due():
        time.sleep(0.1)
    path, secs = rec.stop()
    print(f"STTLIVE audio_s={secs:.2f} auto_stop={not stop.is_set()}")
    if path is None:
        print("STTLIVE result=too_short")
        return 3
    t1 = time.monotonic()
    try:
        text = sv.transcribe_and_delete(str(path), stt.transcribe)
        res = "ok"
    except sv.VoiceRejected as e:
        text, res = "", f"rejected:{e}"
    finally:
        sc.discard(path)
    dec = time.monotonic() - t1
    print(f"STTLIVE decode_s={dec:.2f} rtf={dec / max(secs, 0.01):.2f} result={res} chars={len(text)} "
          f"words={len(text.split())} file_left={Path(path).exists()}")
    print(f"TEXT {text}")
    return 0 if res == "ok" else 4


if __name__ == "__main__":
    sys.exit(main())
