"""EVENTS (этап C, 01.11.2026): патч src/device_watch.py -- state_text(): текущее состояние техники.

Детектор переходов уже держит в памяти состояние (last), программу (active_cycle) и плановое
окончание (planned_end, completionTime облака). state_text() отдаёт это текстом БЕЗ сети --
для блока фактов мозга и ответа на «когда закончит сушилка?».

Предусловие: содержимое src/device_watch.py (без BOM, LF) SHA256[:16] == A439F4A64EF0C81D
(копия Оператора 01.11 16:27). Коды выхода: 0 -- OK/ALREADY; 2 -- предусловие; 3 -- якорь;
4 -- py_compile (откат).
"""
from __future__ import annotations

import hashlib
import os
import py_compile
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "device_watch.py"
EXPECTED_CONTENT = "A439F4A64EF0C81D"
MARKER = "def state_text("

ANCHOR = "    return notes + _MONITOR.poll_once()\n"
ADDITION = r'''

STATE_LABEL = {"washer": "Стиральная", "dryer": "Сушильная"}


def state_text(monitor: "Monitor | None" = None) -> str:
    """EVENTS (01.11): текущее состояние техники из памяти детектора, без сети. Пусто -- опросов ещё не было.
    Неподтверждённый stop (STOP_CONFIRM_POLLS) ещё считается run: отставание не больше одного опроса."""
    m = monitor if monitor is not None else _MONITOR
    if m is None or not m.watcher.started:
        return ""
    w = m.watcher
    try:
        last = dict(w.last)
        cycles = dict(w.active_cycle)
        ends = dict(w.planned_end)
    except RuntimeError:                 # словарь меняется в потоке опроса -- в следующий раз
        return ""
    parts = []
    for kind in sorted(last):
        title = STATE_LABEL.get(kind, kind)
        st = last.get(kind)
        if st in ("run", "pause"):
            cyc = cycles.get(kind)
            name = samsung_names_ru.name_ru(kind, cyc) if cyc else "программа не известна"
            end = _local_hhmm(ends.get(kind))
            parts.append(f"{title}: {'идёт' if st == 'run' else 'на паузе'} «{name}»"
                         + (f", по плану до {end}" if end else "") + ".")
        elif st == "stop":
            parts.append(f"{title}: не работает.")
        else:
            parts.append(f"{title}: состояние неизвестно.")
    if not parts:
        parts.append("машин в аккаунте нет.")
    if m.auth_broken or m.transport_fails:
        parts.append("(связь с облаком SmartThings сейчас нарушена — данные могут быть устаревшими)")
    return " ".join(parts)
'''

BOM = b"\xef\xbb\xbf"


def h16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16].upper()


def main() -> int:
    if not TARGET.is_file():
        print(f"TARGET_NOT_FOUND {TARGET}")
        return 2
    raw = TARGET.read_bytes()
    has_bom = raw.startswith(BOM)
    text = (raw[len(BOM):] if has_bom else raw).decode("utf-8")
    crlf = "\r\n" in text
    norm = text.replace("\r\n", "\n")
    if MARKER in norm:
        print(f"ALREADY {MARKER} present | {h16(raw)}")
        return 0
    content_hash = h16(norm.encode("utf-8"))
    if content_hash != EXPECTED_CONTENT:
        print(f"SRC_HASH_MISMATCH raw={h16(raw)} content={content_hash} expected content={EXPECTED_CONTENT}")
        return 2
    if norm.count(ANCHOR) != 1 or not norm.endswith(ANCHOR):
        print(f"ANCHOR_FAIL count={norm.count(ANCHOR)} at_end={norm.endswith(ANCHOR)}")
        return 3
    norm = norm + ADDITION
    out = norm.replace("\n", "\r\n") if crlf else norm
    out_bytes = (BOM if has_bom else b"") + out.encode("utf-8")
    backup = TARGET.with_name(TARGET.name + ".before_events")
    if backup.exists():
        i = 2
        while TARGET.with_name(f"{TARGET.name}.before_events{i}").exists():
            i += 1
        backup = TARGET.with_name(f"{TARGET.name}.before_events{i}")
    shutil.copy2(TARGET, backup)
    tmp = TARGET.with_name(TARGET.name + ".events_tmp")
    tmp.write_bytes(out_bytes)
    os.replace(tmp, TARGET)
    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(backup, TARGET)
        print(f"COMPILE_FAIL rolled back from {backup.name}: {e}")
        return 4
    print(f"PATCHED {TARGET.relative_to(ROOT)} | {h16(raw)} -> {h16(out_bytes)} | "
          f"bom={has_bom} crlf={crlf} | backup={backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
