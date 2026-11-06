import sys
from pathlib import Path

OKO_RAG_DIR = Path(__file__).resolve().parent / "oko_rag"
sys.path.append(str(OKO_RAG_DIR))

import chromadb
from rag_config import DB_DIR, COLLECTION_NAME, TOP_K
from embeddings import get_embedding

client = chromadb.PersistentClient(path=DB_DIR)
collection = client.get_or_create_collection(name=COLLECTION_NAME)

print(f"TOP_K из rag_config.py: {TOP_K}")
print(f"Всего чанков в коллекции: {collection.count()}")
print()

queries = [
    "Что говорится про obsidian compass ridgeline?",
    "Процитируй дословно фразу про obsidian compass ridgeline",
    "obsidian compass ridgeline",
]

for query in queries:
    print(f"=== Запрос: {query!r} ===")
    query_embedding = get_embedding(query)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=collection.count(),  # берём ВСЕ чанки, не только top_k, чтобы увидеть полное ранжирование
    )
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    dists = results["distances"][0]

    for rank, (doc, meta, dist) in enumerate(zip(docs, metas, dists), start=1):
        source = meta.get("source")
        chunk = meta.get("chunk")
        marker = " <-- pdf_stress_test.pdf chunk 4 (искомый)" if source == "pdf_stress_test.pdf" and chunk == 4 else ""
        in_topk = "IN TOP_K" if rank <= TOP_K else "excluded"
        preview = doc[:60].replace("\n", " ")
        print(f"  #{rank:2d} [{in_topk:9s}] dist={dist:.4f}  {source} chunk={chunk}  {preview!r}{marker}")
    print()
