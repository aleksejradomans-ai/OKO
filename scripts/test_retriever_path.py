#!/usr/bin/env python3
"""
test_retriever_path.py — закрывает последний непроверенный пункт
test_rag_pipeline.py: использует ли retriever.py (реальный путь
bot.py/console_chat.py) тот же способ эмбеддинга запроса, что и запись
через index_chunks_to_rag.

В отличие от test_rag_pipeline.py (низкоуровневый ChromaDB add()/query()
напрямую), этот скрипт вызывает retriever.build_context() — ту самую
функцию, которую реально дёргают bot.py и console_chat.py. Если оба
теста проходят, но по-разному (например, этот падает, хотя
test_rag_pipeline.py — OK), это укажет конкретно на retriever.py как
на точку расхождения, а не на схему ChromaDB/эмбеддинги в целом.

Тестовая запись удаляется в конце (finally), не засоряет базу.
"""

import sys
import time
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "oko_rag"))

import config  # noqa
from knowledge_indexer import _request_embedding_vector  # noqa
import indexer as oko_rag_indexer  # noqa
import retriever  # noqa — тот самый модуль, который используют bot.py/console_chat.py

TEST_ID = f"__retriever_test__{int(time.time())}"
UNIQUE_MARKER = f"zephyr crimson lantern {int(time.time())}"
TEST_TEXT = f"Тестовая фраза для проверки retriever.py. Уникальный маркер: {UNIQUE_MARKER}."

FAILED = False


def report(name, ok, detail=""):
    global FAILED
    status = "OK  " if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED = True
    return ok


def main():
    print("=== Проверка retriever.py (реальный путь bot.py/console_chat.py) ===\n")

    # 1. Пишем тестовую запись тем же способом, что index_chunks_to_rag
    vector = _request_embedding_vector(TEST_TEXT)
    if not report("Ollama возвращает эмбеддинг для записи", vector is not None):
        sys.exit(1)

    try:
        collection = oko_rag_indexer.get_collection()
        collection.add(
            ids=[TEST_ID],
            embeddings=[vector],
            metadatas=[{"source": "retriever_test", "chunk": 0}],
            documents=[TEST_TEXT],
        )
        report("Тестовая запись создана в коллекции", True, f"id={TEST_ID}")
    except Exception as e:
        report("Тестовая запись создана в коллекции", False, repr(e))
        sys.exit(1)

    try:
        # 2. Вызываем РЕАЛЬНЫЙ retriever.build_context() — тот самый путь,
        # которым идут bot.py и console_chat.py при обычном (не /discuss) запросе.
        query = f"Что говорится про {UNIQUE_MARKER}?"
        context, hits = retriever.build_context(query)

        found_in_context = UNIQUE_MARKER in context
        report(
            "build_context() находит маркер в контексте",
            found_in_context,
            f"context length={len(context)} символов" if context else "context пуст",
        )

        found_in_hits = any(TEST_ID in str(h) or UNIQUE_MARKER in str(h.get("text", "")) for h in hits)
        report("Тестовая запись есть среди hits", found_in_hits, f"hits count={len(hits)}")

        if not found_in_context:
            print("\n  Первые 300 символов реально полученного контекста:")
            print(f"  {context[:300]!r}")

    except Exception as e:
        report("Вызов retriever.build_context()", False, repr(e))

    # Уборка
    try:
        collection.delete(ids=[TEST_ID])
        print(f"\n(тестовая запись {TEST_ID} удалена)")
    except Exception as e:
        print(f"\n[!] Не удалось удалить тестовую запись {TEST_ID}: {e} — удалите вручную")

    print("\n" + "=" * 60)
    if FAILED:
        print("ИТОГ: retriever.py НЕ подтверждён как использующий тот же путь эмбеддинга.")
    else:
        print("ИТОГ: retriever.py использует тот же путь эмбеддинга, что и запись — контур замкнут.")
    print("=" * 60)

    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
