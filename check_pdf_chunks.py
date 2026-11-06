import sys
from pathlib import Path

OKO_RAG_DIR = Path(__file__).resolve().parent / "oko_rag"
sys.path.append(str(OKO_RAG_DIR))

import chromadb
from rag_config import DB_DIR, COLLECTION_NAME

client = chromadb.PersistentClient(path=DB_DIR)
collection = client.get_or_create_collection(name=COLLECTION_NAME)

result = collection.get(where={"source": "pdf_stress_test.pdf"}, include=["documents", "metadatas"])

print(f"Всего чанков pdf_stress_test.pdf: {len(result['ids'])}")
for doc, meta in zip(result["documents"], result["metadatas"]):
    print(f"\n--- chunk {meta.get('chunk')} ---")
    print(doc)
