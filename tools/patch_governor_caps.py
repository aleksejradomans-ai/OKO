"""CAPS_CATALOG (этап A, 30.10.2026): патч src/governor.py.

Добавляет каталог функций Ока в system prompt основного контура:
  - compose_system_prompt() (чистая функция), CAPABILITIES_MAX_CHARS;
  - AgentGovernor.set_capabilities() / _main_system();
  - _process и _self_correct: system_prompt=self._main_system().
Судья и рефлексия не меняются (system_prompt=None), /discuss -- identity_text.
Без вызова set_capabilities() поведение прежнее: _main_system() == identity_text.

Предусловие: байтовый SHA256[:16] src/governor.py == EXPECTED_SRC (CONFIRMED
выводом Оператора 30.10 15:45). Якоря сверяются по содержимому (без BOM, LF);
исходные BOM и переводы строк сохраняются.

Коды выхода: 0 -- OK или ALREADY; 2 -- предусловие (файл/хэш); 3 -- якорь;
4 -- py_compile после патча (файл откатан из бэкапа).
"""
from __future__ import annotations

import hashlib
import os
import py_compile
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "governor.py"
EXPECTED_SRC = "EEA52CF0419DBEF6"
MARKER = "CAPS_CATALOG"

R1_OLD = 'DETERMINISTIC_OPTIONS = {"temperature": 0, "seed": 0}\n'
R1_NEW = R1_OLD + '''
# CAPS_CATALOG (30.10, этап A): каталог функций Ока в system prompt основного
# контура. Только _process и _self_correct (тексты, которые дойдут до
# пользователя); судья и рефлексия остаются холодными (system_prompt=None),
# /discuss -- только identity (окно занято документом). Каталог собирается вне
# governor (src/capabilities.py) и ставится set_capabilities() ПО СОБЫТИЮ
# (старт, selfcheck, смена сети, успех модуля): в пути LLM-вызова диска нет.
# Без вызова set_capabilities поведение прежнее: _main_system() == identity_text.
CAPABILITIES_MAX_CHARS = 1500


def compose_system_prompt(
    identity: Optional[str], capabilities: Optional[str]
) -> Optional[str]:
    """Чистая функция: identity + каталог через пустую строку. Пустые и
    пробельные части отбрасываются; если не осталось ничего -- None."""
    parts = [p.strip() for p in (identity, capabilities) if p and p.strip()]
    return "\\n\\n".join(parts) or None
'''

R2_OLD = "        self.identity_text = self._load_identity()\n"
R2_NEW = R2_OLD + "        self._main_system_prompt = self.identity_text  # CAPS_CATALOG\n"

R3_OLD = "    @property\n    def transport_gate_state(self) -> str:\n"
R3_NEW = '''    def set_capabilities(self, text: Optional[str]) -> None:
        """CAPS_CATALOG: ставит каталог функций. Вызывать по событию, не на
        каждый запрос. Присваивание строки атомарно в asyncio -- блокировка не
        нужна. None/пусто -- возврат к чистому identity_text."""
        text = (text or "").strip()
        if len(text) > CAPABILITIES_MAX_CHARS:
            logger.warning(
                "[CAPS] catalog truncated %d -> %d chars", len(text), CAPABILITIES_MAX_CHARS
            )
            text = text[:CAPABILITIES_MAX_CHARS]
        self._main_system_prompt = compose_system_prompt(self.identity_text, text or None)
        logger.info("[CAPS] catalog set: %d chars", len(text))

    def _main_system(self) -> Optional[str]:
        """CAPS_CATALOG: system prompt основного контура, только из памяти.
        getattr: тесты строят governor через __new__ без __init__."""
        return getattr(self, "_main_system_prompt", None) or self.identity_text

''' + R3_OLD

R4_OLD = "result = await self._call_llm_with_timeout(prompt, system_prompt=self.identity_text)"
R4_NEW = "result = await self._call_llm_with_timeout(prompt, system_prompt=self._main_system())"

R5_OLD = "prompt, system_prompt=self.identity_text, options=DETERMINISTIC_OPTIONS"
R5_NEW = "prompt, system_prompt=self._main_system(), options=DETERMINISTIC_OPTIONS"

REPLACEMENTS = [
    ("R1 const+compose", R1_OLD, R1_NEW),
    ("R2 __init__", R2_OLD, R2_NEW),
    ("R3 methods", R3_OLD, R3_NEW),
    ("R4 _process", R4_OLD, R4_NEW),
    ("R5 _self_correct", R5_OLD, R5_NEW),
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
    text = raw[len(BOM):].decode("utf-8") if has_bom else raw.decode("utf-8")
    crlf = "\r\n" in text
    norm = text.replace("\r\n", "\n")

    if MARKER in norm:
        print(f"ALREADY {MARKER} present | {h16(raw)}")
        return 0

    src_hash = h16(raw)
    if src_hash != EXPECTED_SRC:
        print(f"SRC_HASH_MISMATCH got={src_hash} expected={EXPECTED_SRC}")
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

    print(f"PATCHED {TARGET.relative_to(ROOT)} | {src_hash} -> {h16(out_bytes)} | "
          f"bom={has_bom} crlf={crlf} | backup={backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
