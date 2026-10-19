import logging
import os
import sys
from indexer import get_client, get_collection  # noqa: F401  (get_collection -- обратная совместимость)
from embeddings import get_embedding
from rag_config import COLLECTION_NAME, TOP_K

logger = logging.getLogger("oko_rag.retriever")

_metric_warned = False


def _is_missing_collection(e: Exception) -> bool:
    """chromadb 1.x: NotFoundError; 0.x: ValueError("Collection ... does not exist")."""
    if type(e).__name__ == "NotFoundError":
        return True
    return isinstance(e, ValueError) and "does not exist" in str(e)


def _space(collection) -> str:
    meta = getattr(collection, "metadata", None) or {}
    if meta.get("hnsw:space"):
        return str(meta["hnsw:space"])
    cfg = getattr(collection, "configuration_json", None) or {}
    return str((cfg.get("hnsw") or {}).get("space") or "l2")


def _open_collection():
    """RAG_COLLECTION_GUARD: коллекция для ПОИСКА. В отличие от
    indexer.get_collection() (запись, get_or_create) не создаёт коллекцию:
    раньше опечатка/пропажа OKO_COLLECTION или удалённая chroma_db молча
    давали новую пустую коллекцию и пустой контекст без единого сигнала.
    Нет коллекции -> WARNING [RAG_COLLECTION_MISSING] и None (пустой
    контекст). Это не ошибка на свежей установке: коллекцию создаёт первый
    Approve. Метрика не cosine при включённом пороге -> один WARNING
    [RAG_METRIC_MISMATCH] за процесс."""
    global _metric_warned
    try:
        collection = get_client().get_collection(name=COLLECTION_NAME)
    except Exception as e:
        if not _is_missing_collection(e):
            raise
        logger.warning(
            "[RAG_COLLECTION_MISSING] коллекции %r нет в chroma_db -- контекст пуст. "
            "Проверь OKO_COLLECTION в .env; на свежей установке коллекцию создаёт "
            "первый Approve.", COLLECTION_NAME,
        )
        return None
    if not _metric_warned and _max_distance() > 0 and _space(collection) != "cosine":
        _metric_warned = True
        logger.warning(
            "[RAG_METRIC_MISMATCH] коллекция %r использует метрику %s, а порог "
            "OKO_RAG_MAX_DISTANCE откалиброван под cosine (ADR-006).",
            COLLECTION_NAME, _space(collection),
        )
    return collection


def search(query: str, top_k: int = TOP_K) -> list[dict]:
    collection = _open_collection()
    if collection is None:
        return []
    query_embedding = get_embedding(query)
    results = collection.query(query_embeddings=[query_embedding], n_results=top_k)

    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    hits = []
    for doc, meta, dist in zip(docs, metas, dists):
        hits.append({
            "text": doc,
            "source": meta.get("source"),
            "chunk": meta.get("chunk"),
            "distance": dist,
        })
    return hits


def _max_distance() -> float:
    """Порог релевантности из .env (OKO_RAG_MAX_DISTANCE). Чанки с большим
    расстоянием в контекст не попадают. 0, пусто или нечисловое значение —
    фильтр выключен (прежнее поведение: всегда top_k ближайших, какими бы
    далёкими они ни были). Шкала зависит от метрики коллекции (для cosine
    это 0..2, меньше = ближе), поэтому значение подбирается по измерениям."""
    try:
        return float(os.getenv("OKO_RAG_MAX_DISTANCE", "0") or 0)
    except ValueError:
        return 0.0


def build_context(query: str, top_k: int = TOP_K, max_chars: int = 3000) -> tuple[str, list[dict]]:
    """Собирает контекст для промпта, обрезая по max_chars, чтобы не раздувать окно модели.

    Если задан OKO_RAG_MAX_DISTANCE, чанки дальше порога отбрасываются; когда
    подходящих нет, возвращается пустой контекст (вызывающий код сообщает
    "релевантный контекст не найден"), а не случайные ближайшие куски.
    search() порог НЕ применяет: он остаётся "сырым" для диагностики."""
    hits = search(query, top_k)
    max_d = _max_distance()
    if max_d > 0:
        hits = [h for h in hits if h["distance"] <= max_d]
    context = ""
    used_hits = []

    for h in hits:
        block = f"[Источник: {h['source']}, чанк {h['chunk']}]\n{h['text']}\n\n"
        if len(context) + len(block) > max_chars:
            break
        context += block
        used_hits.append(h)

    return context.strip(), used_hits


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "тест"
    ctx, hits = build_context(q)
    print(ctx)
    print("\n--- hits ---")
    for h in hits:
        print(h["source"], h["chunk"], round(h["distance"], 4))
