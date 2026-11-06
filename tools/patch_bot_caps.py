"""CAPS_CATALOG (этап A, 30.10.2026): патч src/bot.py -- каталог функций в контекст мозга.

Добавляет:
  - import capabilities, proof_registry (стиль import как у device_watch: ОДИН
    способ импорта, иначе src.X и X дают два модуля с разным состоянием);
  - _caps_apply(app): ProofRegistry (в app.bot_data, создаётся один раз) ->
    capabilities.build_catalog(latest) -> governor.set_capabilities();
  - вызов в post_init ПОСЛЕ governor.start(), в потоке (asyncio.to_thread):
    первичное чтение реестра -- дисковое.
Сбой каталога не мешает старту: WARNING [CAPS], governor остаётся на identity.

Предусловие: src/bot.py -- байтовый SHA256[:16] == C97F0C1185AAF414 (CONFIRMED выводом
Оператора 30.10 15:45) или то же содержимое без BOM (1870AE414F81C5D4, копия из чата).
Якоря -- по содержимому (без BOM, LF); исходные BOM и переводы строк сохраняются.
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
EXPECTED_RAW = "C97F0C1185AAF414"
EXPECTED_CONTENT = "1870AE414F81C5D4"  # тот же текст без BOM
MARKER = "CAPS_CATALOG"

A1_OLD = "import garmin_report\n"
A1_NEW = A1_OLD + "import capabilities      # CAPS_CATALOG (30.10): каталог функций для мозга\nimport proof_registry\n"

A2_OLD = "async def post_init(app: Application) -> None:\n"
A2_NEW = '''def _caps_apply(app: Application) -> None:
    """CAPS_CATALOG (30.10, этап A): реестр подтверждений -> каталог функций -> governor.
    Реестр создаётся один раз (полный проход) и живёт в bot_data; дальше -- только
    refresh() (stat + дельта). В путь LLM-вызова диск не попадает: governor хранит
    готовую строку. Сбой -- WARNING, governor остаётся на чистом identity."""
    try:
        reg = app.bot_data.get("proof_registry")
        if reg is None:
            reg = proof_registry.ProofRegistry()
            app.bot_data["proof_registry"] = reg
        else:
            reg.refresh()
        app.bot_data["governor"].set_capabilities(capabilities.build_catalog(reg.latest()))
    except Exception as e:
        logger.warning("[CAPS] catalog not applied: %s: %s", type(e).__name__, e)


''' + A2_OLD

A3_OLD = '    await governor.start()\n    logger.info("Governor lifecycle: started.")\n'
A3_NEW = A3_OLD + "    await asyncio.to_thread(_caps_apply, app)  # CAPS_CATALOG\n"

REPLACEMENTS = [
    ("A1 imports", A1_OLD, A1_NEW),
    ("A2 _caps_apply", A2_OLD, A2_NEW),
    ("A3 post_init", A3_OLD, A3_NEW),
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
    body = raw[len(BOM):] if has_bom else raw
    text = body.decode("utf-8")
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

    for name, old, new in REPLACEMENTS:
        n = norm.count(old)
        if n != 1:
            print(f"ANCHOR_FAIL {name}: count={n}")
            return 3
        norm = norm.replace(old, new, 1)

    out = norm.replace("\n", "\r\n") if crlf else norm
    out_bytes = (BOM if has_bom else b"") + out.encode("utf-8")

    backup = TARGET.with_name(TARGET.name + ".before_caps")
    if backup.exists():
        i = 2
        while TARGET.with_name(f"{TARGET.name}.before_caps{i}").exists():
            i += 1
        backup = TARGET.with_name(f"{TARGET.name}.before_caps{i}")
    shutil.copy2(TARGET, backup)

    tmp = TARGET.with_name(TARGET.name + ".caps_tmp")
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
