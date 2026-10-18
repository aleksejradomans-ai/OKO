import os
import sys
from indexer import get_collection
from embeddings import get_embedding
from rag_config import TOP_K


def search(query: str, top_k: int = TOP_K) -> list[dict]:
    collection = get_collection()
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
