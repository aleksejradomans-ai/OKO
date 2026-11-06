import logging
import os
import hashlib
import chromadb

from rag_config import DATA_DIR, DB_DIR, COLLECTION_NAME, CHUNK_SIZE, CHUNK_OVERLAP
from rag_config import COLLECTION_METADATA
from loader import load_document, chunk_text
from embeddings import get_embedding

logger = logging.getLogger("oko_rag.indexer")

SUPPORTED_EXT = (".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg")


def get_client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(path=DB_DIR)


def get_collection():
    """Коллекция для ЗАПИСИ (создаётся при первом Approve).

    RAG_COLLECTION_GUARD: новая коллекция создаётся с метрикой cosine -- без
    неё chromadb берёт l2, и порог OKO_RAG_MAX_DISTANCE (cosine, ADR-006) на
    свежей установке отсекал бы почти всё. Для существующей коллекции
    metadata игнорируется (проверено на chromadb 1.5.9)."""
    client = get_client()
    return client.get_or_create_collection(name=COLLECTION_NAME, metadata=COLLECTION_METADATA)


def content_hash(text: str) -> str:
    """Хэш ИЗВЛЕЧЁННОГО ТЕКСТА, не сырых байтов файла — унифицировано с
    content_hash в knowledge_indexer.py (см. историю решений, аудит блока
    №4: ранее здесь был file_hash() от сырых байтов, из-за чего один и
    тот же документ, попавший в систему и через Telegram HITL, и через
    этот ручной обход, не распознавался как дубль ни на одном из двух
    уровней дедупликации — два разных id на один и тот же контент).

    Дороже, чем хэш сырых байтов (требует, чтобы load_document() —
    парсинг/OCR — уже отработал), но это приемлемо: text к моменту
    вызова этой функции в index_file() уже извлечён для чанкования,
    так что лишнего парсинга не добавляется — просто хэш считается
    после, а не до этого шага.

    Используется как неймспейс для chunk id (см. index_file — реальная
    дедупликация при переиндексации идёт по full_path через
    collection.delete(), не по совпадению этого хэша); полной
    межпроцессной дедупликации между этим модулем и SQLite-таблицей
    knowledge это по-прежнему не даёт (разные хранилища), но хотя бы
    один и тот же контент теперь получает одинаковый хэш в обоих местах.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def index_file(path: str, collection=None) -> None:
    """Индексирует один файл. Переиндексация: старые чанки этого файла удаляются по full_path
    (не по имени) — так подпапки с одинаковыми названиями файлов не конфликтуют."""
    collection = collection or get_collection()
    text = load_document(path)
    chunks = chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)

    fname = os.path.basename(path)
    full_path = os.path.abspath(path)
    fhash = content_hash(text)

    collection.delete(where={"full_path": full_path})

    ids, embeddings, metadatas, documents = [], [], [], []
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        if i % 10 == 0:
            logger.info("%s: embedding chunk %d/%d", fname, i + 1, total)
        ids.append(f"{fhash}_{i}")
        embeddings.append(get_embedding(chunk))
        metadatas.append({"source": fname, "full_path": full_path, "chunk": i})
        documents.append(chunk)

    if ids:
        collection.add(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents)

    print(f"[+] {fname}: {len(chunks)} чанков")


def index_directory(directory: str = DATA_DIR) -> None:
    """Рекурсивный обход — можно раскидывать документы по подпапкам, дедупликация по full_path
    это переживёт."""
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
        print(f"Создал {directory} — положи туда файлы и запусти снова")
        return

    collection = get_collection()
    paths = []
    for root, _, files in os.walk(directory):
        for f in files:
            if f.lower().endswith(SUPPORTED_EXT):
                paths.append(os.path.join(root, f))

    if not paths:
        print(f"Нет файлов ({SUPPORTED_EXT}) в {directory}")
        return

    for path in paths:
        try:
            index_file(path, collection)
        except Exception as e:
            print(f"[!] Ошибка индексации {os.path.basename(path)}: {e}")

    print(f"Готово. Всего в коллекции: {collection.count()} чанков")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    # После унификации RAG-контуров (см. историю решений: "Split Brain",
    # Путь А — Merge вокруг Telegram HITL) единственная официальная точка
    # приёма новых данных — knowledge_indexer.py через Telegram-подтверждение.
    # Прямой запуск этого скрипта пишет в ту же ChromaDB-коллекцию, минуя
    # approve/reject — то есть в обход единственного механизма контроля
    # того, что попадает в базу знаний. Не удалено (может быть осознанно
    # нужно для доверенных файлов), но требует явного подтверждения, чтобы
    # не быть использованным случайно по старой памяти вместо Telegram-потока.
    print(
        "\n[!] ВНИМАНИЕ: этот путь индексации обходит Telegram HITL-подтверждение.\n"
        "    Официальный способ добавить документ в базу знаний — положить файл\n"
        "    в data/knowledge/ и подтвердить через Telegram (Approve).\n"
        "    Используй этот скрипт только для доверенных данных, которые не\n"
        "    нуждаются в ручной проверке.\n"
    )
    confirm = input("Продолжить и проиндексировать документы из oko_rag/documents/ напрямую? (yes/no): ")
    if confirm.strip().lower() != "yes":
        print("Отменено.")
    else:
        index_directory()
