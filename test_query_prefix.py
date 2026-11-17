import sys
from pathlib import Path

OKO_RAG_DIR = Path(__file__).resolve().parent / "oko_rag"
sys.path.append(str(OKO_RAG_DIR))

import chromadb
from rag_config import DB_DIR, COLLECTION_NAME, TOP_K
from embeddings import get_embedding

client = chromadb.PersistentClient(path=DB_DIR)
collection = client.get_or_create_collection(name=COLLECTION_NAME)

def run_query(label: str, query_text: str):
    print(f"=== {label}: {query_text!r} ===")
    query_embedding = get_embedding(query_text)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=collection.count(),
    )
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    dists = results["distances"][0]

    target_rank = None
    for rank, (doc, meta, dist) in enumerate(zip(docs, metas, dists), start=1):
        source = meta.get("source")
        chunk = meta.get("chunk")
        if source == "pdf_stress_test.pdf" and chunk == 4:
            target_rank = rank
        if rank <= 8:  # показываем только первые 8 мест, не весь список
            in_topk = "IN TOP_K" if rank <= TOP_K else "excluded"
            preview = doc[:50].replace("\n", " ")
            marker = " <-- ИСКОМЫЙ" if source == "pdf_stress_test.pdf" and chunk == 4 else ""
            print(f"  #{rank:2d} [{in_topk:9s}] dist={dist:.4f}  {source} chunk={chunk}  {preview!r}{marker}")

    print(f"  >>> Искомый чанк (chunk 4) занял место: #{target_rank} из {collection.count()}")
    print()


# Без префикса (как сейчас, baseline)
run_query("BASELINE (без префикса)", "Что говорится про obsidian compass ridgeline?")

# С префиксом search_query: — только к запросу, документы остаются без префикса
run_query("С ПРЕФИКСОМ search_query:", "search_query: Что говорится про obsidian compass ridgeline?")

# Голые слова, с префиксом
run_query("Голые слова + префикс", "search_query: obsidian compass ridgeline")
