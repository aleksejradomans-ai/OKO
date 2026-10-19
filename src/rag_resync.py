"""rag_resync.py -- лечение рассинхрона SQLite <-> ChromaDB.

Источник правды -- SQLite (knowledge.content хранит файл целиком).

Направление SQLite -> ChromaDB (как раньше):
  MISSING -- запись source='file' есть, чанков нет;
  PARTIAL -- чанков меньше ожидаемого.
Направление ChromaDB -> SQLite (RAG_ORPHAN_SCAN, 19.10):
  ORPHAN  -- чанки с content_hash, которого нет ни в одной строке knowledge
             (документ удалён из SQLite, чанки остались и занимают top_k);
  NO_HASH -- чанки без content_hash (ручной oko_rag/indexer.py) -- только отчёт.
Работает с ОДНОЙ коллекцией -- той, что отдаёт oko_rag_indexer.get_collection()
(rag_config.COLLECTION_NAME / OKO_COLLECTION из .env); имя печатается первым.

Dry-run по умолчанию. Запуск (PYTHONPATH=C:\\oko;C:\\oko\\src):
    python src\\rag_resync.py                         # только отчёт
    python src\\rag_resync.py --apply                 # доиндексировать MISSING
    python src\\rag_resync.py --apply --include-partial   # + пересобрать PARTIAL
    python src\\rag_resync.py --apply --prune-orphans     # + удалить ORPHAN-чанки
    python src\\rag_resync.py --apply --drop-id 7         # удалить запись id=7 (source='file')
                                                          # из SQLite и её чанки
Запись в ChromaDB/SQLite -- только при остановленном OkoBot и после бэкапа
(БД + chroma_db). Для MISSING/PARTIAL нужна Ollama. Код возврата: 0 -- ок,
1 -- были сбои, 2 -- oko_rag недоступен, 3 -- неверные аргументы.
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


def scan_orphans(conn, collection):
    """RAG_ORPHAN_SCAN: чанки ChromaDB, у которых нет строки в SQLite.

    Сравнение со ВСЕМИ content_hash таблицы knowledge (не только source='file'):
    чанк считается сиротой, только если его хэша нет нигде. Возвращает
    (orphans, no_hash), orphans = [{"hash", "chunks", "sources", "no_category"}]
    по убыванию числа чанков, no_hash = число чанков без content_hash.
    """
    known = {
        r[0] for r in conn.execute(
            "SELECT content_hash FROM knowledge "
            "WHERE content_hash IS NOT NULL AND content_hash != ''"
        )
    }
    got = collection.get(include=["metadatas"])
    by_hash: dict = {}
    no_hash = 0
    for meta in got.get("metadatas") or []:
        meta = meta or {}
        h = meta.get("content_hash")
        if not h:
            no_hash += 1
            continue
        if h in known:
            continue
        o = by_hash.setdefault(h, {"hash": h, "chunks": 0, "sources": set(), "no_category": 0})
        o["chunks"] += 1
        if meta.get("source"):
            o["sources"].add(str(meta["source"]))
        if not meta.get("category"):
            o["no_category"] += 1
    orphans = sorted(by_hash.values(), key=lambda o: (-o["chunks"], o["hash"]))
    return orphans, no_hash


def heal(item: dict, replace: bool) -> int:
    if replace:
        with ki._chroma_write_lock:
            ki.oko_rag_indexer.get_collection().delete(where={"content_hash": item["hash"]})
    return ki.index_chunks_to_rag(
        item["content"], item["filename"], item["hash"], item["category"]
    )


def prune_hash(content_hash: str) -> int:
    """Удаляет все чанки с данным content_hash. Возвращает число удалённых."""
    with ki._chroma_write_lock:
        coll = ki.oko_rag_indexer.get_collection()
        before = len(coll.get(where={"content_hash": content_hash}).get("ids") or [])
        if before:
            coll.delete(where={"content_hash": content_hash})
        after = len(coll.get(where={"content_hash": content_hash}).get("ids") or [])
    return before - after


def drop_record(conn, rid: int):
    """Удаляет строку knowledge (только source='file'). Возвращает
    (filename, content_hash) или None, если строки нет / она не 'file'.
    SQLite -- первым: при сбое ChromaDB чанки станут ORPHAN и будут видны
    следующему прогону, а не наоборот (запись без чанков незаметна для RAG)."""
    row = conn.execute(
        "SELECT filename, content_hash FROM knowledge WHERE id = ? AND source = 'file'",
        (rid,),
    ).fetchone()
    if row is None:
        return None
    with conn:
        conn.execute("DELETE FROM knowledge WHERE id = ? AND source = 'file'", (rid,))
    return row


def _collection_name() -> str:
    return str(getattr(ki.oko_rag_indexer, "COLLECTION_NAME", "?"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--include-partial", action="store_true")
    ap.add_argument("--prune-orphans", action="store_true")
    ap.add_argument("--drop-id", type=int, action="append", default=[])
    args = ap.parse_args(argv)

    if not ki.RAG_SYNC_AVAILABLE:
        print("oko_rag недоступен -- нечего синхронизировать")
        return 2
    if (args.prune_orphans or args.drop_id) and not args.apply:
        print("--prune-orphans / --drop-id требуют --apply")
        return 3

    with closing(db.get_connection()) as conn:
        collection = ki.oko_rag_indexer.get_collection()
        missing, partial, total = scan(conn, collection)
        orphans, no_hash = scan_orphans(conn, collection)

    print(f"Коллекция ChromaDB: {_collection_name()}")
    print(f"SQLite file-records: {total}")
    print(f"MISSING (в ChromaDB нет): {len(missing)}")
    for it in missing:
        print(f"  id={it['id']} {it['filename']} [{it['category']}] hash={it['hash'][:12]} чанков ожидается {it['expected']}")
    print(f"PARTIAL (чанков меньше ожидаемого): {len(partial)}")
    for it in partial:
        print(f"  id={it['id']} {it['filename']} hash={it['hash'][:12]} {it['actual']}/{it['expected']}")
    print(f"ORPHAN (в ChromaDB есть, в SQLite нет): {len(orphans)} хэшей, "
          f"{sum(o['chunks'] for o in orphans)} чанков")
    for o in orphans:
        src = ", ".join(sorted(o["sources"])) or "?"
        print(f"  hash={o['hash'][:12]} чанков={o['chunks']} без category={o['no_category']} source={src}")
    print(f"NO_HASH (чанки без content_hash, только отчёт): {no_hash}")

    if not args.apply:
        print("dry-run: ничего не изменено. Для лечения: --apply")
        return 0

    failures = 0
    dropped: set = set()

    for rid in args.drop_id:
        with closing(db.get_connection()) as conn:
            row = drop_record(conn, rid)
        if row is None:
            failures += 1
            print(f"[FAIL] drop id={rid}: нет записи source='file'")
            continue
        name, h = row
        dropped.add(rid)
        n = prune_hash(h) if h else 0
        print(f"[OK] drop id={rid} {name}: строка SQLite удалена, чанков удалено {n}")

    if args.prune_orphans:
        for o in orphans:
            n = prune_hash(o["hash"])
            print(f"[OK] prune hash={o['hash'][:12]}: удалено {n}/{o['chunks']} чанков")
            if n != o["chunks"]:
                failures += 1
    elif orphans:
        print("ORPHAN пропущены (нужен --prune-orphans)")

    # удалённые через --drop-id не переиндексируются обратно
    todo = [(it, False) for it in missing if it["id"] not in dropped]
    if args.include_partial:
        todo += [(it, True) for it in partial if it["id"] not in dropped]
    elif partial:
        print("PARTIAL пропущены (нужен --include-partial)")

    consecutive = healed = 0
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
