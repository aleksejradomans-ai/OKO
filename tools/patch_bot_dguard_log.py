"""D_GUARD_LOG (этап D, 01.11.2026): строка журнала D -- под контракт test_stagnation_guard.

Контракт (TestBotSourceContract.test_skipped_history_write_is_warning_with_marker): каждая строка
bot.py с «exchange not persisted» обязана содержать маркер [HISTORY_SKIPPED]. Строка D его не имела.
Меняется ОДНА строка: маркер [HISTORY_SKIPPED] впереди, D_GUARD остаётся для поиска.

Предусловие: src/bot.py -- байтовый SHA256[:16] 5B3E0E3174154EE2 (CONFIRMED 01.11 20:19).
Коды выхода: 0 -- OK/ALREADY; 2 -- предусловие; 3 -- якорь; 4 -- py_compile (откат).
"""
from __future__ import annotations

import hashlib
import os
import py_compile
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "bot.py"
EXPECTED_RAW = "5B3E0E3174154EE2"
OLD = 'logger.warning("[D_GUARD] fake_done user=%s word=%s — exchange not persisted", user_id, fake)'
NEW = 'logger.warning("[HISTORY_SKIPPED] [D_GUARD] fake_done user=%s word=%s — exchange not persisted", user_id, fake)'
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
    if NEW in text:
        print(f"ALREADY | {h16(raw)}")
        return 0
    if h16(raw) != EXPECTED_RAW:
        print(f"SRC_HASH_MISMATCH raw={h16(raw)} expected={EXPECTED_RAW}")
        return 2
    if text.count(OLD) != 1:
        print(f"ANCHOR_FAIL count={text.count(OLD)}")
        return 3
    out_bytes = (BOM if has_bom else b"") + text.replace(OLD, NEW, 1).encode("utf-8")
    backup = TARGET.with_name(TARGET.name + ".before_dguard_log")
    shutil.copy2(TARGET, backup)
    tmp = TARGET.with_name(TARGET.name + ".dguard_tmp")
    tmp.write_bytes(out_bytes)
    os.replace(tmp, TARGET)
    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(backup, TARGET)
        print(f"COMPILE_FAIL rolled back from {backup.name}: {e}")
        return 4
    print(f"PATCHED src/bot.py | {h16(raw)} -> {h16(out_bytes)} | backup={backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
