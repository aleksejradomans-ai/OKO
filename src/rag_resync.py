"""rag_resync.py -- лечение рассинхрона SQLite <-> ChromaDB.

Источник правды -- SQLite (knowledge.content хранит файл целиком). Скрипт
находит записи source='file', чей content_hash отсутствует в ChromaDB
(MISSING) или представлен неполным набором чанков (PARTIAL), и
переиндексирует их через knowledge_indexer.index_chunks_to_rag.

Dry-run по умолчанию. Запуск (PYTHONPATH=C:\\oko;C:\\oko\\src):
    python src\\rag_resync.py                   # только отчёт
    python src\\rag_resync.py --apply           # доиндексировать MISSING
    python src\\rag_resync.py --apply --include-partial   # + пересобрать PARTIAL
Нужна доступная Ollama (эмбеддинги). Код возврата: 0 -- ок, 1 -- были сбои,
2 -- oko_rag недоступен.
"""
from __future__ import annotations

import argparse
import sys
from contextlib import closing

import db
import knowledge_indexer as ki
import logger as oko_logger

logger = oko_logger.get_logger("rag_resync")

MAX_CONSECUTIVE_FAILURES = 3


def chroma_counts(collection) -> dict:
    counts: dict = {}
    got = collection.get(include=["metadatas"])
    for meta in got.get("metadatas") or []:
        h = (meta or {}).get("content_hash")
        if h:
            counts[h] = counts.get(h, 0) + 1
    return counts


def scan(conn, collection):
    """Возвращает (missing, partial, total_rows)."""
    counts = chroma_counts(collection)
    rows = conn.execute(
        "SELECT id, filename, content, content_hash, category FROM knowledge "
        "WHERE source = 'file' AND content_hash IS NOT NULL AND content_hash != '' "
        "ORDER BY id"
    ).fetchall()
    missing, partial = [], []
    for rid, filename, content, h, category in rows:
        expected = len(ki.chunk_text(content, ki.CHUNK_SIZE, ki.CHUNK_OVERLAP))
        actual = counts.get(h, 0)
        item = {
            "id": rid, "filename": filename or f"knowledge_id_{rid}", "content": content,
            "hash": h, "category": category, "expected": expected, "actual": actual,
        }
        if actual == 0:
            missing.append(item)
        elif actual < expected:
            partial.append(item)
    return missing, partial, len(rows)


def heal(item: dict, replace: bool) -> int:
    if replace:
        with ki._chroma_write_lock:
            ki.oko_rag_indexer.get_collection().delete(where={"content_hash": item["hash"]})
    return ki.index_chunks_to_rag(
        item["content"], item["filename"], item["hash"], item["category"]
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--include-partial", action="store_true")
    args = ap.parse_args(argv)

    if not ki.RAG_SYNC_AVAILABLE:
        print("oko_rag недоступен -- нечего синхронизировать")
        return 2

    with closing(db.get_connection()) as conn:
        collection = ki.oko_rag_indexer.get_collection()
        missing, partial, total = scan(conn, collection)

    print(f"SQLite file-records: {total}")
    print(f"MISSING (в ChromaDB нет): {len(missing)}")
    for it in missing:
        print(f"  id={it['id']} {it['filename']} [{it['category']}] hash={it['hash'][:12]} чанков ожидается {it['expected']}")
    print(f"PARTIAL (чанков меньше ожидаемого): {len(partial)}")
    for it in partial:
        print(f"  id={it['id']} {it['filename']} hash={it['hash'][:12]} {it['actual']}/{it['expected']}")

    if not args.apply:
        print("dry-run: ничего не изменено. Для лечения: --apply")
        return 0

    todo = [(it, False) for it in missing]
    if args.include_partial:
        todo += [(it, True) for it in partial]
    elif partial:
        print("PARTIAL пропущены (нужен --include-partial)")

    failures = consecutive = healed = 0
    for it, replace in todo:
        n = heal(it, replace)
        if n > 0:
            healed += 1
            consecutive = 0
            print(f"[OK] id={it['id']} {it['filename']}: {n} чанков")
        else:
            failures += 1
            consecutive += 1
            print(f"[FAIL] id={it['id']} {it['filename']}: 0 чанков (Ollama/Chroma?)")
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                print(f"стоп: {MAX_CONSECUTIVE_FAILURES} сбоя подряд")
                break
    print(f"итог: восстановлено {healed}, сбоев {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    oko_logger.setup_logging()
    sys.exit(main())
