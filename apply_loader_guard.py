#!/usr/bin/env python3
"""apply_loader_guard.py -- RAG_READABILITY_GUARD для oko_rag/loader.py.

load_document() теперь отказывается отдавать нечитаемый текст: если доля
символов-заменителей (U+25A0 ■, U+FFFD) среди непробельных символов выше
GARBAGE_MAX_RATIO, бросается UnreadableDocumentError (подкласс ValueError)
и пишется WARNING. Причина появления: PDF, собранный шрифтом без кириллицы
(ReportLab/Helvetica), хранит кириллицу как ■ прямо в файле -- такой
документ раньше молча индексировался как мусор.

Dry-run по умолчанию. --apply пишет файл, бэкап: loader.py.bak_readability
Использование:  python apply_loader_guard.py [--src C:\\oko\\oko_rag] [--apply]
"""
from __future__ import annotations

import argparse
import difflib
import pathlib
import shutil
import sys

MARKER = "RAG_READABILITY_GUARD"

GUARD_BLOCK = '''# RAG_READABILITY_GUARD: нечитаемые документы (кириллица потеряна при
# генерации PDF, шрифт без кириллицы -> символы-заменители) не должны
# попадать в RAG: они занимают top_k и ничего не отвечают.
GARBAGE_CHARS = "\\u25a0\\ufffd"  # ■ и �
GARBAGE_MAX_RATIO = 0.20  # доля мусорных символов среди непробельных


class UnreadableDocumentError(ValueError):
    """Документ прочитан, но текст непригоден для индексации."""


def garbage_ratio(text: str) -> float:
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return 0.0
    return sum(1 for c in visible if c in GARBAGE_CHARS) / len(visible)


def check_readable(text: str, name: str = "") -> None:
    ratio = garbage_ratio(text)
    if ratio > GARBAGE_MAX_RATIO:
        logger.warning(
            "[UNREADABLE_DOCUMENT] %s: %.0f%% символов-заменителей (порог %.0f%%), "
            "документ не будет проиндексирован",
            name, ratio * 100, GARBAGE_MAX_RATIO * 100,
        )
        raise UnreadableDocumentError(
            f"документ нечитаем: {ratio:.0%} символов-заменителей "
            f"(вероятно, шрифт без кириллицы при создании файла): {name}"
        )


'''

LOAD_OLD = '''def load_document(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
'''
LOAD_NEW = '''def load_document(path: str) -> str:
    """RAG_READABILITY_GUARD: читает документ и отбраковывает нечитаемый текст."""
    text = _load_document_raw(path)
    check_readable(text, os.path.basename(path))
    return text


def _load_document_raw(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
'''

EDITS = [
    ("блок предохранителя", "def _require_ocr() -> None:\n", GUARD_BLOCK + "def _require_ocr() -> None:\n", 1),
    ("обёртка load_document", LOAD_OLD, LOAD_NEW, 1),
]


def apply_edits(text: str) -> str:
    if MARKER in text:
        raise SystemExit("[SKIP] маркер RAG_READABILITY_GUARD уже есть -- патч применён")
    for name, old, new, count in EDITS:
        found = text.count(old)
        if found != count:
            raise SystemExit(f"[FAIL] якорь '{name}': найдено {found}, ожидалось {count}")
        text = text.replace(old, new)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\oko\oko_rag")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    path = pathlib.Path(args.src) / "loader.py"
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    crlf = b"\r\n" in raw
    original = raw.decode("utf-8").replace("\r\n", "\n")

    patched = apply_edits(original)
    compile(patched, str(path), "exec")
    for name, *_ in EDITS:
        print(f"[OK] {name}")

    diff = list(difflib.unified_diff(
        original.splitlines(), patched.splitlines(), "a/loader.py", "b/loader.py", lineterm="",
    ))
    plus = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    minus = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    print(f"diff: +{plus} -{minus} | CRLF={crlf} BOM={bom}")

    if not args.apply:
        print("dry-run: файл не изменён. Для записи: --apply")
        return 0

    backup = path.with_name(path.name + ".bak_readability")
    shutil.copy2(path, backup)
    out = patched.replace("\n", "\r\n") if crlf else patched
    data = out.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    print(f"written: {path} | backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
