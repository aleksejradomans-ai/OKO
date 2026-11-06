"""D_GUARD (этап D, 01.11.2026): патч src/bot.py -- запрет ложного «сделал».

process_logic: если человек просил действие, а модель отвечает, что выполнила (router.fake_done),
обмен НЕ пишется в историю (иначе ложь повторяется -- находка 30.10 16:10), ответ показывается
с пометкой. Голос идёт через тот же process_logic (_brain_answer). /discuss не затрагивается.

Предусловие: src/bot.py после этапа C -- байтовый SHA256[:16] AC72B8264F035D4B (CONFIRMED 01.11)
или то же содержимое без BOM/CRLF 6DCBC9D743F0CA0D.
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
EXPECTED_RAW = "AC72B8264F035D4B"
EXPECTED_CONTENT = "6DCBC9D743F0CA0D"
MARKER = "D_GUARD (этап D"

R = [
    ("D1 note",
     'P1_NOTE = "(P1: повтор ответа — обмен не сохранён в память)"\n',
     'P1_NOTE = "(P1: повтор ответа — обмен не сохранён в память)"\n'
     '# D_GUARD (этап D, 01.11): модель заявила выполнение, которого не было (бот команду не распознал).\n'
     'D_NOTE = ("⚠️ Модель написала «{word}», но бот это НЕ выполнял — команда не распознана. "\n'
     '          "Ответ не сохранён в память. Скажи иначе, например «подсветка музыка» или «громкость плюс 5».")\n'),
    ("D2 process_logic",
     '    else:\n'
     '        persisted = await asyncio.to_thread(_save_exchange, user_id, sanitized_input, raw_response)\n'
     '        await asyncio.to_thread(_log_depth, user_id)\n'
     '    return _with_p1_note(_with_notice(raw_response, getattr(result, "incidents", None)), persisted)\n',
     '    else:\n'
     '        fake = router.fake_done(sanitized_input, raw_response)   # D_GUARD (этап D)\n'
     '        if fake:\n'
     '            logger.warning("[D_GUARD] fake_done user=%s word=%s — exchange not persisted", user_id, fake)\n'
     '            note = D_NOTE.format(word=fake)\n'
     '            return f"{note}\\n{_with_notice(raw_response, getattr(result, \'incidents\', None))}"\n'
     '        persisted = await asyncio.to_thread(_save_exchange, user_id, sanitized_input, raw_response)\n'
     '        await asyncio.to_thread(_log_depth, user_id)\n'
     '    return _with_p1_note(_with_notice(raw_response, getattr(result, "incidents", None)), persisted)\n'),
]

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
    raw_hash, content_hash = h16(raw), h16(norm.encode("utf-8"))
    if raw_hash != EXPECTED_RAW and content_hash != EXPECTED_CONTENT:
        print(f"SRC_HASH_MISMATCH raw={raw_hash} content={content_hash} "
              f"expected raw={EXPECTED_RAW} or content={EXPECTED_CONTENT}")
        return 2
    for name, old, new in R:
        n = norm.count(old)
        if n != 1:
            print(f"ANCHOR_FAIL {name}: count={n}")
            return 3
        norm = norm.replace(old, new, 1)
    out = norm.replace("\n", "\r\n") if crlf else norm
    out_bytes = (BOM if has_bom else b"") + out.encode("utf-8")
    backup = TARGET.with_name(TARGET.name + ".before_dguard")
    i = 2
    while backup.exists():
        backup = TARGET.with_name(f"{TARGET.name}.before_dguard{i}")
        i += 1
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
    print(f"PATCHED {TARGET.relative_to(ROOT)} | {raw_hash} -> {h16(out_bytes)} | "
          f"bom={has_bom} crlf={crlf} | backup={backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
