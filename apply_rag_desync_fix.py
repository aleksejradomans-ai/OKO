#!/usr/bin/env python3
"""apply_rag_desync_fix.py -- RAG_DESYNC_FIX для src/knowledge_indexer.py.

1. index_chunks_to_rag: all-or-nothing (при сбое эмбеддинга любого чанка в
   ChromaDB не пишется НИЧЕГО; без неполных документов), маркер [RAG_DESYNC].
2. process_single: статус "processed", но чанков 0 при доступном RAG ->
   WARNING [RAG_DESYNC] с хешем (SQLite записан, ChromaDB нет).
3. Три `with db.get_connection() as conn` -> contextlib.closing (утечка
   соединений: sqlite3 `with` не закрывает соединение).

Dry-run по умолчанию. --apply пишет файл, бэкап: knowledge_indexer.py.bak_rag_desync
Использование:  python apply_rag_desync_fix.py [--src C:\\oko\\src] [--apply]
"""
from __future__ import annotations

import argparse
import difflib
import pathlib
import shutil
import sys

MARKER = "RAG_DESYNC_FIX"

EDITS = [
    # (имя, old, new, ожидаемое число вхождений)
    (
        "import contextlib",
        "import argparse\n",
        "import argparse\nimport contextlib\n",
        1,
    ),
    (
        "all-or-nothing по чанкам",
        """        if vector is None:
            logger.warning(
                "Чанк %d/%d файла %s пропущен (эмбеддинг не удался)",
                i + 1, len(chunks), filename,
            )
            continue
""",
        """        if vector is None:
            # RAG_DESYNC_FIX: all-or-nothing. Неполный набор чанков в
            # ChromaDB неотличим от полного -- поэтому при сбое любого
            # чанка не пишем ничего и не тратим таймауты на остальные.
            logger.warning(
                "[RAG_DESYNC] файл %s: чанк %d/%d не векторизован, в ChromaDB "
                "ничего не записано (all-or-nothing)",
                filename, i + 1, len(chunks),
            )
            return 0
""",
        1,
    ),
    (
        "маркер на сбое записи в Chroma",
        'logger.error("Не удалось записать чанки %s в ChromaDB: %s", filename, e)',
        'logger.error("[RAG_DESYNC] не удалось записать чанки %s в ChromaDB: %s", filename, e)',
        1,
    ),
    (
        "предупреждение о рассинхроне в process_single",
        '        status = "processed"\n',
        """        if RAG_SYNC_AVAILABLE and chunks_indexed == 0:
            # RAG_DESYNC_FIX: SQLite уже закоммичен, ChromaDB пуст для этого
            # хеша. Лечение: python src/rag_resync.py --apply
            logger.warning(
                "[RAG_DESYNC] %s: запись в SQLite есть (hash=%s), в ChromaDB нет. "
                "Лечение: python src/rag_resync.py --apply",
                file_path.name, content_hash[:12],
            )
        status = "processed"
""",
        1,
    ),
    (
        "closing() для соединений",
        "with db.get_connection() as conn:",
        "with contextlib.closing(db.get_connection()) as conn:",
        3,
    ),
]


def apply_edits(text: str) -> str:
    if MARKER in text:
        raise SystemExit("[SKIP] маркер RAG_DESYNC_FIX уже есть -- патч применён")
    for name, old, new, count in EDITS:
        found = text.count(old)
        if found != count:
            raise SystemExit(f"[FAIL] якорь '{name}': найдено {found}, ожидалось {count}")
        text = text.replace(old, new)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\oko\src")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    path = pathlib.Path(args.src) / "knowledge_indexer.py"
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
        original.splitlines(), patched.splitlines(), "a/knowledge_indexer.py", "b/knowledge_indexer.py", lineterm="",
    ))
    plus = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    minus = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    print(f"diff: +{plus} -{minus} | CRLF={crlf} BOM={bom}")

    if not args.apply:
        print("dry-run: файл не изменён. Для записи: --apply")
        return 0

    backup = path.with_name(path.name + ".bak_rag_desync")
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
