import sys
from pathlib import Path

OKO_RAG_DIR = Path(__file__).resolve().parent / "oko_rag"
sys.path.append(str(OKO_RAG_DIR))

import chromadb
from rag_config import DB_DIR, COLLECTION_NAME, TOP_K
from embeddings import get_embedding

client = chromadb.PersistentClient(path=DB_DIR)
collection = client.get_or_create_collection(name=COLLECTION_NAME)

# Подозреваемые "хабы" — короткие тестовые файлы, которые доминировали
# в предыдущем ранжировании независимо от смысла запроса. Ничего не
# удаляем из коллекции — просто исключаем эти source из результатов
# ПОСЛЕ запроса, на стороне клиента. Обратимо, безопасно, не требует
# бэкапа/восстановления.
SUSPECTED_HUBS = {
    "stress_restart_test2.txt",
    "rag_check2.txt",
    "final_check_txt.txt",
    "rag_status_v2.txt",
    "deploy_test2.txt",
}

query_text = "Что говорится про obsidian compass ridgeline?"
query_embedding = get_embedding(query_text)

results = collection.query(
    query_embeddings=[query_embedding],
    n_results=collection.count(),
)
docs = results["documents"][0]
metas = results["metadatas"][0]
dists = results["distances"][0]

filtered = [
    (doc, meta, dist) for doc, meta, dist in zip(docs, metas, dists)
    if meta.get("source") not in SUSPECTED_HUBS
]

print(f"Запрос: {query_text!r}")
print(f"Исключено как предполагаемые хабы: {SUSPECTED_HUBS}")
print(f"Осталось чанков после фильтрации: {len(filtered)} из {len(docs)}")
print()

target_rank = None
for rank, (doc, meta, dist) in enumerate(filtered, start=1):
    source = meta.get("source")
    chunk = meta.get("chunk")
    if source == "pdf_stress_test.pdf" and chunk == 4:
        target_rank = rank
    if rank <= 8:
        in_topk = "IN TOP_K" if rank <= TOP_K else "excluded"
        preview = doc[:50].replace("\n", " ")
        marker = " <-- ИСКОМЫЙ" if source == "pdf_stress_test.pdf" and chunk == 4 else ""
        print(f"  #{rank:2d} [{in_topk:9s}] dist={dist:.4f}  {source} chunk={chunk}  {preview!r}{marker}")

print(f"\n>>> После исключения хабов искомый чанк занял место: #{target_rank} из {len(filtered)}")
