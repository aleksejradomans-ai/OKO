#!/usr/bin/env python3
"""
test_rag_pipeline.py — сквозная проверка контура эмбеддингов, которую
НЕ делает smoke_test.py (тот проверяет только heartbeat() клиента).

Проверяет реальный путь данных:
  1. Ollama отдаёт эмбеддинг ожидаемой размерности для OLLAMA_EMBED_MODEL.
  2. Запись тестового чанка в РЕАЛЬНУЮ коллекцию oko_rag (тем же кодом,
     что использует index_chunks_to_rag) действительно персистится на диск.
  3. Прямой query() ЭТИМ ЖЕ вектором находит записанный чанк с высоким
     сходством (косвенно подтверждает, что add() и query() говорят на
     одном языке векторов, если вызывать их одинаково).
  4. ВАЖНО: этот скрипт НЕ может подтвердить, что retriever.py реально
     используемый в bot.py/console_chat.py, embeds запрос тем же способом
     (Ollama vs встроенная embedding_function коллекции). Для этого нужен
     отдельный прогон через сам retriever.py — см. предупреждение в конце.

Тестовая запись удаляется в конце (finally), не должна засорять базу.
"""

import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "oko_rag"))

import config  # noqa
from knowledge_indexer import _request_embedding_vector  # noqa

TEST_ID = f"__pipeline_test__{int(time.time())}"
TEST_TEXT = "Тестовая фраза для проверки сквозного RAG-пайплайна Oko."

FAILED = False


def report(name, ok, detail=""):
    global FAILED
    status = "OK  " if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED = True
    return ok


def main():
    print("=== Сквозная проверка RAG-пайплайна ===\n")

    # 1. Эмбеддинг от Ollama
    vector = _request_embedding_vector(TEST_TEXT)
    if not report("Ollama возвращает эмбеддинг", vector is not None):
        print("\nОстановлено: без вектора дальнейшие шаги бессмысленны.")
        sys.exit(1)

    dim = len(vector)
    report("Размерность вектора", dim > 0, f"{dim} компонент, модель={config.OLLAMA_EMBED_MODEL}")

    # 2. Запись в реальную коллекцию тем же путём, что index_chunks_to_rag
    try:
        import indexer as oko_rag_indexer  # тот же алиас, что в knowledge_indexer.py
        collection = oko_rag_indexer.get_collection()
        collection.add(
            ids=[TEST_ID],
            embeddings=[vector],
            metadatas=[{"source": "pipeline_test", "category": "test"}],
            documents=[TEST_TEXT],
        )
        report("Запись в ChromaDB (add)", True, f"id={TEST_ID}")
    except Exception as e:
        report("Запись в ChromaDB (add)", False, repr(e))
        sys.exit(1)

    # 3. Персистентность: перечитываем по id напрямую с диска
    try:
        fetched = collection.get(ids=[TEST_ID], include=["embeddings", "documents"])
        persisted = bool(fetched["ids"])
        report("Запись персистентна (get by id)", persisted)
    except Exception as e:
        report("Запись персистентна (get by id)", False, repr(e))

    # 4. Тем же вектором ищем — должен найтись сам себя первым результатом
    try:
        result = collection.query(query_embeddings=[vector], n_results=1)
        found_id = result["ids"][0][0] if result["ids"] and result["ids"][0] else None
        distance = result["distances"][0][0] if result.get("distances") else None
        ok = found_id == TEST_ID
        report(
            "Query тем же вектором находит запись",
            ok,
            f"найдено id={found_id}, distance={distance}",
        )
    except Exception as e:
        report("Query тем же вектором находит запись", False, repr(e))

    # Уборка
    try:
        collection.delete(ids=[TEST_ID])
        print(f"\n(тестовая запись {TEST_ID} удалена)")
    except Exception as e:
        print(f"\n[!] Не удалось удалить тестовую запись {TEST_ID}: {e} — удалите вручную")

    print("\n" + "=" * 60)
    print("НЕ ПРОВЕРЕНО этим скриптом (нужен отдельный прогон):")
    print(" - Использует ли retriever.py (тот, что реально дергают")
    print("   bot.py/console_chat.py) тот же способ эмбеддинга запроса,")
    print("   что и запись выше (Ollama), или встроенную embedding_function")
    print("   коллекции Chroma (по умолчанию ONNX MiniLM).")
    print(" - Если способы разные — add() и query() работают в разных")
    print("   векторных пространствах, ошибок при этом не будет,")
    print("   но реальный поиск контента будет тихо деградировать.")
    print("=" * 60)

    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
