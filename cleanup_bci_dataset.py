import sys
from pathlib import Path

OKO_RAG_DIR = Path(__file__).resolve().parent / "oko_rag"
sys.path.append(str(OKO_RAG_DIR))

import chromadb
from rag_config import DB_DIR, COLLECTION_NAME

TARGET_SOURCE = "Brain_Computer_Interfaces_a_Review (1).pdf"

client = chromadb.PersistentClient(path=DB_DIR)
collection = client.get_or_create_collection(name=COLLECTION_NAME)

before = collection.count()
print(f"Всего чанков до удаления: {before}")

to_delete = collection.get(where={"source": TARGET_SOURCE}, include=[])
ids = to_delete["ids"]
print(f"Найдено чанков для удаления (source='{TARGET_SOURCE}'): {len(ids)}")

if not ids:
    print("Нечего удалять.")
else:
    confirm = input(f"Удалить {len(ids)} чанков? Это необратимо. (yes/no): ")
    if confirm.strip().lower() != "yes":
        print("Отменено.")
    else:
        collection.delete(ids=ids)
        after = collection.count()
        print(f"Удалено. Чанков осталось: {after} (было {before})")