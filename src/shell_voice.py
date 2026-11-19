"""shell_voice.py -- голос окна (B18, решение Оператора 18.11 13:39, Q6): окно пишет запись во временный файл
%TEMP%\\oko_voice\\<uuid>.wav и присылает боту путь; бот распознаёт общей моделью (stt, cpu/int8) и УДАЛЯЕТ файл в
finally -- и при успехе, и при ошибке. Путь проверяется до чтения: только папка oko_voice, имя <uuid>.wav, размер не больше
MAX_BYTES. Запись без речи (активный RMS ниже порога тишины) не распознаётся. Текст распознавания в журнал не пишется.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

DIR_NAME = "oko_voice"
MAX_BYTES = 2 * 1024 * 1024
NAME_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.wav$")


class VoiceRejected(ValueError):
    """Файл не принят (путь, имя, размер, тишина) -- текст для человека."""


def check_path(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute() or p.parent.name != DIR_NAME or not NAME_RE.match(p.name):
        raise VoiceRejected("путь записи не из папки голоса окна")
    if p.is_symlink() or not p.is_file():
        raise VoiceRejected("файла записи нет")
    if p.stat().st_size > MAX_BYTES:
        raise VoiceRejected("запись длиннее допустимой")
    return p


def has_speech(p: Path) -> bool:
    try:
        import voice_dsp as d
    except Exception:  # noqa: BLE001 -- без numpy/scipy проверку пропускаем, решает распознавание
        return True
    x, sr = d.read_wav(p)
    return d.active_rms_db(x, sr) > d.SILENCE_DBFS


def transcribe_and_delete(raw: str, transcribe: Callable[[str], str]) -> str:
    """-> текст. Файл удаляется всегда (finally), если путь прошёл проверку."""
    p = check_path(raw)
    try:
        if not has_speech(p):
            raise VoiceRejected("в записи нет речи")
        text = (transcribe(str(p)) or "").strip()
        if not text:
            raise VoiceRejected("речь не распознана")
        return text
    finally:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
