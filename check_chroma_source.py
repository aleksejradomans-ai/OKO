import sys
from pathlib import Path

OKO_RAG_DIR = Path(__file__).resolve().parent / "oko_rag"
sys.path.append(str(OKO_RAG_DIR))

import chromadb
from rag_config import DB_DIR, COLLECTION_NAME

client = chromadb.PersistentClient(path=DB_DIR)
collection = client.get_or_create_collection(name=COLLECTION_NAME)

result = collection.get(where={"source": "rag_check2.txt"}, include=["documents", "metadatas"])

print(f"Найдено чанков с source=rag_check2.txt: {len(result['ids'])}")
for doc, meta in zip(result["documents"], result["metadatas"]):
    print(f"  meta: {meta}")
    print(f"  text: {doc}")

# Заодно посмотрим, сколько всего РАЗНЫХ источников в коллекции —
# чтобы понять реальный масштаб "старого" датасета vs наших HITL-файлов
all_meta = collection.get(include=["metadatas"])["metadatas"]
sources = set(m.get("source") for m in all_meta)
print(f"\nВсего уникальных source в коллекции: {len(sources)}")
for s in sorted(sources):
    count = sum(1 for m in all_meta if m.get("source") == s)
    print(f"  {s}: {count} чанков")
