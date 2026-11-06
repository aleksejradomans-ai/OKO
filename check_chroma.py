import sys
from pathlib import Path

OKO_RAG_DIR = Path(__file__).resolve().parent / "oko_rag"
sys.path.append(str(OKO_RAG_DIR))

import chromadb
from rag_config import DB_DIR, COLLECTION_NAME

client = chromadb.PersistentClient(path=DB_DIR)
collection = client.get_or_create_collection(name=COLLECTION_NAME)

print(f"DB_DIR: {DB_DIR}")
print(f"Коллекция: {COLLECTION_NAME}")
print(f"Всего чанков в коллекции: {collection.count()}")
print()

result = collection.get(limit=20, include=["documents", "metadatas"])
for i, (doc, meta) in enumerate(zip(result["documents"], result["metadatas"])):
    print(f"--- Чанк {i} ---")
    print(f"  source: {meta.get('source')}")
    print(f"  chunk: {meta.get('chunk')}")
    print(f"  content_hash: {meta.get('content_hash', meta.get('full_path'))}")
    print(f"  text: {doc[:100]}")
    print()
