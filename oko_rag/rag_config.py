import os
import sys
import pathlib

# --- Единый источник правды по параметрам Ollama (устраняет config drift) ---
# До этого патча OLLAMA_HOST/EMBED_MODEL были независимыми литералами внутри
# oko_rag/rag_config.py, отдельными от config.py в корне проекта, которым
# пользуются knowledge_indexer.py и bot.py. Совпадение значений было чистой
# случайностью настройки, а не гарантией: смена модели эмбеддингов в одном
# конфиге без синхронной правки второго молча разводила запись и поиск по
# разным векторным пространствам (add()/query() при этом не бросают
# исключений — см. историю решений, разбор retriever.py/indexer.py).
#
# oko_rag/ подключается как отдельный sys.path-модуль (см. тот же паттерн
# в knowledge_indexer.py), поэтому здесь та же операция в обратную
# сторону — добавляем src/ в sys.path, чтобы имортировать корневой config.py
# независимо от того, кто и как запускает retriever.py/indexer.py.
_ROOT_SRC_DIR = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_ROOT_SRC_DIR) not in sys.path:
    # append, не insert(0): нет причины вытеснять уже загруженные модули
    # с тем же именем — коллизий имён между src/ и oko_rag/ сейчас нет,
    # а insert(0) без необходимости меняет порядок разрешения для ВСЕХ
    # последующих импортов в процессе, не только для config.
    sys.path.append(str(_ROOT_SRC_DIR))

import config as _root_config  # noqa: E402  (после манипуляции sys.path)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "documents")   # сюда кладёшь PDF/TXT для индексации
DB_DIR = os.path.join(BASE_DIR, "chroma_db")     # векторная БД, персистентная, на диске
# Имя коллекции берётся из .env (OKO_COLLECTION). Так переключение на другую
# коллекцию (например, после смены модели эмбеддингов) и откат делаются
# правкой .env, без правки кода.
# RAG_COLLECTION_GUARD (19.10): по умолчанию -- коллекция под текущую модель
# эмбеддингов (bge-m3), а не прежняя "oko_knowledge" (другая модель, удалена
# 19.10): при пропавшей строке в .env поиск молча шёл бы по векторам чужой
# модели -- без ошибок, с мусором в контексте. Пустое значение = не задано.
DEFAULT_COLLECTION = "oko_knowledge_bge_m3"
COLLECTION_NAME = (os.getenv("OKO_COLLECTION") or "").strip() or DEFAULT_COLLECTION
# Метрика коллекции: порог OKO_RAG_MAX_DISTANCE (ADR-006) откалиброван под cosine.
# Применяется только при СОЗДАНИИ коллекции (существующую chromadb не меняет).
COLLECTION_METADATA = {"hnsw:space": "cosine"}

# Формат собирается так же, как в knowledge_indexer.py::_request_embedding_vector
# (f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/..."), чтобы контракт
# адреса совпадал буквально, а не "по договорённости".
OLLAMA_HOST = f"http://{_root_config.OLLAMA_HOST}:{_root_config.OLLAMA_PORT}"
EMBED_MODEL = _root_config.OLLAMA_EMBED_MODEL
CHAT_MODEL = getattr(_root_config, "OLLAMA_MODEL", "llama3")  # fallback на случай, если поле ещё не заведено в config.py

TESSERACT_CMD = None  # напр. r"C:\Program Files\Tesseract-OCR\tesseract.exe", если PATH не подхватился

CHUNK_SIZE = 800      # символов на чанк
CHUNK_OVERLAP = 150   # перекрытие между чанками
TOP_K = 5             # сколько чанков подтягивать при поиске
