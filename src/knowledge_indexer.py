import sqlite3
import pathlib
import shutil
import re
import sys
import argparse
import contextlib
import hashlib
import json
import threading
from datetime import datetime

import requests

import config
import db
import logger as oko_logger
from src.io_retry import retry_on_permission_error
from src.path_guard import SecurityAccessViolation, guard_path

# --- Интеграция с oko_rag (унификация RAG-контуров, "Путь А: Merge") ---
# До этой правки существовало два независимых, не связанных друг с другом
# векторных хранилища: knowledge_indexer.py писал в SQLite (knowledge_base.db,
# BLOB-колонка embedding, без chunking), а oko_rag/retriever.py читал из
# отдельного ChromaDB (oko_rag/chroma_db), наполняемого только через ручной
# `python oko_rag/indexer.py`, минуя Telegram HITL. console_chat.py и bot.py
# используют retriever.py -> ChromaDB для поиска контекста, поэтому файлы,
# подтверждённые через Telegram, были структурно невидимы для генерации
# ответов ("Split Brain", обнаружено и подтверждено в сессии на реальном
# запросе к console_chat.py). Решение: knowledge_indexer.py остаётся
# единственной точкой приёма данных (HITL через Telegram — сердце системы),
# но дополнительно чанкует контент и пишет per-chunk эмбеддинги в ТУ ЖЕ
# ChromaDB-коллекцию, которую использует retriever.py — так оба потребителя
# (console_chat.py, bot.py) видят весь одобренный через Telegram контент.
#
# load_document импортирован из того же модуля (oko_rag/loader.py) — она
# умеет читать .pdf (pypdf + OCR-фолбэк pytesseract/PyMuPDF для сканов),
# в отличие от read_text(), который умеет только utf-8 текстовые файлы.
# Используется в process_single для файлов с расширением .pdf (см. историю
# решений — расширение RAG-контура на приём PDF).
#
# oko_rag/ добавляется в sys.path так же, как это уже делает console_chat.py
# (см. историю решений там) — общий, проверенный паттерн, не новый.
OKO_RAG_DIR = pathlib.Path(__file__).resolve().parent.parent / "oko_rag"
sys.path.append(str(OKO_RAG_DIR))

try:
    from loader import chunk_text, load_document
    from rag_config import CHUNK_SIZE, CHUNK_OVERLAP
    import indexer as oko_rag_indexer  # алиас во избежание путаницы с этим файлом
    RAG_SYNC_AVAILABLE = True
except ImportError as e:
    # Частичный успех и здесь: если oko_rag/ недоступен (например, при
    # переносе только src/ без остального проекта) — knowledge_indexer.py
    # продолжает писать в SQLite как раньше, просто без синхронизации
    # в ChromaDB. Явный warning в лог, не молчаливая деградация.
    #
    # ВАЖНО: этот же флаг теперь также означает "умеем ли мы читать PDF" —
    # load_document физически живёт в том же oko_rag/loader.py, что и
    # chunk_text, и импортируется в этом же try-блоке. Если oko_rag
    # недоступен вообще, PDF всё равно нечем читать — отдельный флаг для
    # этого заводить избыточно, семантика RAG_SYNC_AVAILABLE расширена
    # сознательно, не по недосмотру.
    oko_logger.get_logger("knowledge_indexer").warning(
        "oko_rag недоступен (%s) — синхронизация с ChromaDB отключена, "
        "RAG-поиск через console_chat.py/bot.py не увидит эти записи, "
        "PDF-файлы обрабатываться не будут.", e
    )
    RAG_SYNC_AVAILABLE = False

# --- КОНФИГУРАЦИЯ ---
# BASE_DIR берётся из config.py — единая точка правды, общая с bot.py и
# run_system.py. Раньше здесь был захардкожен pathlib.Path(r"C:\AI"), что на
# Linux/macOS не бросало исключение, а создавало мусорную директорию с
# буквальным именем "C:\AI" внутри текущей рабочей папки (тот же класс
# дефекта уже был найден и исправлен в config.py и bot.py — см. историю
# решений). Если watcher (run_system.py) и индексатор смотрят на разные
# директории из-за рассинхрона хардкода, файловые события watchdog никогда
# не совпадут с тем, что индексатор реально обрабатывает.
BASE_DIR = config.BASE_DIR
INPUT_DIR = BASE_DIR / "knowledge"
ARCHIVE_DIR = INPUT_DIR / "archived"
DB_FILE = config.DB_PATH

# Сериализация записи в ChromaDB (см. историю решений, аудит блока №3 —
# oko_rag/indexer.py::get_collection() создаёт НОВЫЙ chromadb.PersistentClient
# на каждый вызов, не кэширует его — это не "общий объект без блокировки",
# а несколько независимых клиентов, открывающих один и тот же каталог на
# диске конкурентно. С тех пор как process_single стал вызываться через
# asyncio.to_thread из bot.py (несколько approve подряд могут выполняться
# в разных потоках одновременно), это стало реальным сценарием, не
# гипотетическим. Точное поведение PersistentClient при конкурентной
# записи из разных инстансов в один путь не проверено (версия chromadb
# не подтверждена на момент этого патча) — Lock здесь defense-in-depth,
# устраняющий риск СО СТОРОНЫ НАШЕГО КОДА независимо от того, насколько
# сама библиотека безопасна. Сериализует только вызовы ИЗ ЭТОГО процесса
# (bot.py) — НЕ защищает от конкурентной записи из run_system.py, если тот
# когда-нибудь тоже станет писать в RAG напрямую (сейчас не пишет).
_chroma_write_lock = threading.Lock()

logger = oko_logger.get_logger("knowledge_indexer")

# Единое хранилище. bot.py читает ИЗ ЭТОЙ ЖЕ базы через retrieve_context.
# Текстовый knowledge_base.txt больше не используется как источник правды.


def setup_database():
    """
    Создаёт таблицу knowledge с полями под будущий RAG:
    - source: откуда пришла запись (file, telegram_pulse, watcher)
    - category: тип контента, определяется правилами по источнику и содержимому
    - embedding: зарезервировано под векторное представление (NULL пока RAG не подключён)
    - content_hash: защита от дублей при повторной обработке файла, который
      остался в INPUT_DIR из-за прерывания процесса между commit() и move().
      Хеш контента, не имени файла: filename может легитимно повторяться
      (например, pulse_realtime.txt перезаписывается новыми показаниями), и
      в этом случае разное содержимое — НЕ дубль.

    Колонка добавлена через ALTER TABLE, а не как часть CREATE TABLE, потому
    что CREATE TABLE IF NOT EXISTS не трогает уже существующую таблицу — на
    БД, созданной до этого изменения, content_hash просто не появился бы,
    и INSERT начал бы падать с "no such column".
    """
    with contextlib.closing(db.get_connection()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                category TEXT NOT NULL,
                pulse_bpm INTEGER,
                embedding BLOB,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge)")}
        if "content_hash" not in existing_columns:
            # ALTER TABLE ADD COLUMN не может добавить UNIQUE напрямую в
            # SQLite — поэтому колонка обычная, а уникальность обеспечивает
            # отдельный UNIQUE INDEX ниже. Старые строки получат NULL — это
            # safe: SQLite считает несколько NULL в UNIQUE-индексе различными
            # значениями (не конфликтуют друг с другом).
            conn.execute("ALTER TABLE knowledge ADD COLUMN content_hash TEXT")
            logger.info("Миграция: добавлена колонка content_hash в существующую БД")

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_content_hash ON knowledge(content_hash)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_category ON knowledge(category)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp ON knowledge(timestamp)
        """)
        conn.commit()


def extract_pulse(text: str):
    """Если в тексте есть запись формата 'PULSE: 96 BPM', вытаскивает число."""
    match = re.search(r"PULSE:\s*(\d+)\s*BPM", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _request_embedding_vector(text: str):
    """Запрашивает у Ollama сырой вектор (list[float]) — общая сетевая
    логика для двух потребителей: get_embedding() (BLOB для SQLite,
    legacy-поле на весь файл) и чанкового пайплайна ChromaDB ниже
    (per-chunk векторы для реального RAG-поиска через oko_rag/retriever.py).

    Возвращает None при любой ошибке (модель недоступна, таймаут, пустой
    ответ) — по стратегии "частичный успех": knowledge_indexer.py работает
    как отдельный subprocess без доступа к governor._gpu_limiter (тот живёт
    в асинхронном процессе bot.py, семафор не пробрасывается через границу
    процессов) — отсутствие вектора не должно блокировать индексацию текста.
    """
    url = f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/api/embeddings"
    payload = {"model": config.OLLAMA_EMBED_MODEL, "prompt": text}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        vector = response.json().get("embedding")
        if not vector:
            logger.warning("Embedding: пустой вектор от Ollama")
            return None
        return vector
    except requests.exceptions.RequestException as e:
        logger.warning("Embedding недоступен (%s)", e)
        return None


def get_embedding(text: str):
    """Обёртка над _request_embedding_vector для BLOB-колонки в SQLite.

    Сериализует вектор через json, не pickle/numpy — не тащим лишние
    зависимости в этот изолированный subprocess. Это legacy-поле на весь
    файл целиком (не per-chunk) — сохранено для обратной совместимости с
    существующей проверкой has_embedding; реальный векторный поиск теперь
    идёт через per-chunk эмбеддинги в ChromaDB (см. index_chunks_to_rag).
    """
    vector = _request_embedding_vector(text)
    if vector is None:
        return None
    return json.dumps(vector).encode("utf-8")


def index_chunks_to_rag(content: str, filename: str, content_hash: str, category: str) -> int:
    """Чанкует контент и пишет per-chunk эмбеддинги в ChromaDB-коллекцию
    oko_rag (ту же, что читает retriever.py). Вызывается только для
    файлов, которые реально прошли Telegram HITL-approve и являются
    новыми (не дублями) — см. вызов в process_single.

    Идентификатор чанка строится на content_hash (не на full_path staging-
    пути, который транзиентен и меняется между staging/ и archived/) —
    это стабильный ключ, совпадающий с тем, что уже используется как
    UNIQUE-ограничение в SQLite. Явного collection.delete() перед add()
    не требуется (в отличие от oko_rag/indexer.py, рассчитанного на
    повторную ручную реиндексацию по full_path): раз content_hash уникален
    и process_single вызывает эту функцию только при status=="processed",
    повторная запись с тем же content_hash сюда structurally не попадёт.

    category — та же категория, что записана в SQLite knowledge.category
    (либо override_category из Telegram-кнопки, либо результат
    classify_content() — см. process_single). Пишется в metadata каждого
    чанка ChromaDB, чтобы retriever.py мог фильтровать поиск через
    collection.query(where={"category": "..."}) — без этого поля
    RAG-поиск при росте базы неизбежно деградирует: top_k по всей
    недифференцированной коллекции всё хуже находит релевантный чанк по
    мере роста числа нерелевантных (см. историю решений — инцидент с
    BCI-датасетом 13 июля, 346 нерелевантных чанков перекрывали retrieval
    для новых тестовых записей до точечной очистки).

    Возвращает количество успешно проиндексированных чанков (0, если
    oko_rag недоступен, или все чанки не удалось векторизовать — не
    бросает исключение, чтобы не блокировать основной поток SQLite/архив).
    """
    if not RAG_SYNC_AVAILABLE:
        return 0

    chunks = chunk_text(content, CHUNK_SIZE, CHUNK_OVERLAP)
    if not chunks:
        return 0

    ids, embeddings, metadatas, documents = [], [], [], []
    for i, chunk in enumerate(chunks):
        vector = _request_embedding_vector(chunk)
        if vector is None:
            # RAG_DESYNC_FIX: all-or-nothing. Неполный набор чанков в
            # ChromaDB неотличим от полного -- поэтому при сбое любого
            # чанка не пишем ничего и не тратим таймауты на остальные.
            logger.warning(
                "[RAG_DESYNC] файл %s: чанк %d/%d не векторизован, в ChromaDB "
                "ничего не записано (all-or-nothing)",
                filename, i + 1, len(chunks),
            )
            return 0
        ids.append(f"{content_hash}_{i}")
        embeddings.append(vector)
        metadatas.append({
            "source": filename,
            "content_hash": content_hash,
            "chunk": i,
            "category": category,
        })
        documents.append(chunk)

    if not ids:
        return 0

    # Lock охватывает ТОЛЬКО получение коллекции и саму запись — не цикл
    # эмбеддинга чанков выше (HTTP-запросы к Ollama, потенциально секунды
    # на чанк). Если бы Lock охватывал весь index_chunks_to_rag, второй
    # конкурентный approve ждал бы завершения embedding ПЕРВОГО файла
    # целиком, прежде чем начать считать свои эмбеддинги — то есть Lock
    # свёл бы на нет весь смысл asyncio.to_thread (параллельная обработка
    # двух approve). Критическая секция — только сама запись в ChromaDB,
    # которая быстрая относительно embedding-запросов.
    try:
        with _chroma_write_lock:
            collection = oko_rag_indexer.get_collection()
            collection.add(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents)
    except Exception as e:
        logger.error("[RAG_DESYNC] не удалось записать чанки %s в ChromaDB: %s", filename, e)
        return 0

    return len(ids)


PATTERN_MAP = {
    "biometric_log": {
        "filename_markers": ["pulse_realtime"],
        "content_markers": ["PULSE:", "BPM"],
    },
    "troubleshooting": {
        "filename_markers": [],
        "content_markers": ["ошибка", "error", "exception", "traceback", "failed"],
    },
    "code_context": {
        "filename_markers": [".py", ".js", ".sql"],
        "content_markers": ["def ", "import ", "class ", "function", "async def"],
    },
    "project_notes": {
        "filename_markers": [],
        "content_markers": ["проект", "архитектур", "идея", "план", "concept"],
    },
}
# Порядок проверки важен: первая совпавшая категория побеждает.
# biometric_log проверяется первым, потому что пульс — самый специфичный и
# однозначный сигнал; project_notes последним, потому что это самая широкая
# категория и легко даёт ложные срабатывания если проверить её раньше.
CATEGORY_PRIORITY = ["biometric_log", "troubleshooting", "code_context", "project_notes"]
DEFAULT_CATEGORY = "general_knowledge"

# Явный whitelist расширений на входе. Раньше intake был ограничен только
# *.txt — что делало категорию code_context (markers: .py/.js/.sql)
# недостижимой, несмотря на то, что классификатор формально её поддерживал.
# .pdf добавлен вместе с интеграцией oko_rag/loader.py::load_document()
# (см. историю решений — расширение RAG-контура на приём PDF).
# whitelist, а не glob("*") — чтобы сканер не подхватил случайно бинарники,
# скрытые директории типа .git, или сам файл базы данных knowledge_base.db,
# если он окажется в INPUT_DIR.
ALLOWED_EXTENSIONS = {".txt", ".py", ".js", ".sql", ".pdf"}


def classify_content(text: str, filename: str) -> str:
    """
    Классификация через PATTERN_MAP вместо хардкоженных if.
    Чтобы добавить новую категорию — просто дописать запись в PATTERN_MAP
    и её имя в CATEGORY_PRIORITY, логику функции трогать не нужно.
    """
    lower_text = text.lower()
    lower_name = filename.lower()

    for category in CATEGORY_PRIORITY:
        rules = PATTERN_MAP[category]

        if any(marker in lower_name for marker in rules["filename_markers"]):
            return category

        if any(marker.lower() in lower_text for marker in rules["content_markers"]):
            return category

    return DEFAULT_CATEGORY


def process_single(
    file_path: pathlib.Path,
    conn: sqlite3.Connection,
    override_category: str | None = None,
) -> str:
    """Обрабатывает один файл: классификация, хеш, запись в БД, архивирование.

    Извлечена из тела цикла index_all_files() — используется и batch-
    сканированием (для файлов, найденных через glob в INPUT_DIR), и
    StagingManager-потоком (для одного конкретного файла из STAGING_DIR
    после HITL-approve). До этого рефакторинга subprocess-вызов
    knowledge_indexer.py после approve не сработал бы вообще: index_all_files
    сканирует только INPUT_DIR, а подтверждённый файл физически лежит в
    STAGING_DIR — он был бы структурно невидим для batch-сканирования.

    file_path должен УЖЕ быть проверен через guard_path вызывающим кодом
    (allowed_root зависит от контекста — INPUT_DIR для batch, STAGING_DIR
    для approve-потока) — эта функция не делает повторную проверку, чтобы
    не навязывать единственный allowed_root обоим вызывающим контекстам.

    override_category: если задан (пришёл из post-hoc выбора кнопкой в
    Telegram — см. историю решений, Развилка 1), имеет приоритет над
    classify_content(). None (по умолчанию) сохраняет старое поведение
    для batch-сканирования (index_all_files), где категория выбирается
    только автоматически.

    Возвращает строку статуса: "processed", "duplicate", "empty" — не
    бросает исключение на штатных путях, только на реальных сбоях I/O
    (которые вызывающий код должен поймать сам, как и раньше), либо если
    PDF получен, а oko_rag/loader.py недоступен (RuntimeError — см. ниже).
    """
    # ARCHIVE_DIR.mkdir раньше вызывался только в index_all_files() —
    # это делало process_single небезопасной при прямом вызове извне
    # (approve-поток в bot.py), не через batch-сканирование. Подтверждено
    # тестом: process_single упала с FileNotFoundError на shutil.move,
    # потому что архивная директория не существовала. Перемещено сюда —
    # это собственная ответственность функции, которая перемещает файл
    # именно в эту директорию, не вызывающего кода.
    ARCHIVE_DIR.mkdir(exist_ok=True, parents=True)

    # Чтение содержимого зависит от расширения: .pdf идёт через
    # oko_rag/loader.py::load_document() (pypdf + OCR-фолбэк для сканов),
    # всё остальное — как раньше, read_text(encoding="utf-8"). Явный
    # RuntimeError, а не тихий skip, если PDF пришёл, а oko_rag недоступен —
    # тот же принцип, что уже применяется в этой функции для реальных
    # сбоев I/O: вызывающий код (index_all_files, handle_staging_callback
    # в bot.py) уже ловит Exception и логирует, не теряя файл молча.
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        if not RAG_SYNC_AVAILABLE:
            raise RuntimeError(
                f"Невозможно обработать PDF {file_path.name}: "
                f"oko_rag/loader.py недоступен"
            )
        content = load_document(str(file_path))
    else:
        content = file_path.read_text(encoding="utf-8")

    if not content.strip():
        logger.warning("Пропущен пустой файл: %s", file_path.name)
        return "empty"

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    if override_category:
        category = override_category
        logger.info(
            "Категория задана вручную (Telegram): %s -> %s",
            file_path.name, category,
        )
    else:
        category = classify_content(content, file_path.name)

    pulse = extract_pulse(content)
    embedding = get_embedding(content)

    cursor = conn.cursor()
    cursor.execute(
        """INSERT OR IGNORE INTO knowledge
           (filename, content, content_hash, source, category, pulse_bpm, embedding)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (file_path.name, content, content_hash, "file", category, pulse, embedding)
    )
    conn.commit()

    if cursor.rowcount == 0:
        logger.info("Дубль по содержимому, пропущен: %s", file_path.name)
        status = "duplicate"
    else:
        # Синхронизация с ChromaDB только для реально новых записей —
        # дубли (rowcount==0) не должны повторно плодить чанки с тем же
        # content_hash (структурно невозможно из-за id на его основе, но
        # избегаем и лишней работы: не считаем эмбеддинги чанков впустую).
        chunks_indexed = index_chunks_to_rag(content, file_path.name, content_hash, category)
        logger.info(
            "Обработан: %s | Категория: %s | Пульс: %s | Вектор: %s | RAG-чанков: %d",
            file_path.name, category, pulse, "да" if embedding else "нет", chunks_indexed,
        )
        if RAG_SYNC_AVAILABLE and chunks_indexed == 0:
            # RAG_DESYNC_FIX: SQLite уже закоммичен, ChromaDB пуст для этого
            # хеша. Лечение: python src/rag_resync.py --apply
            logger.warning(
                "[RAG_DESYNC] %s: запись в SQLite есть (hash=%s), в ChromaDB нет. "
                "Лечение: python src/rag_resync.py --apply",
                file_path.name, content_hash[:12],
            )
        status = "processed"

    # Коммит ПЕРЕД перемещением файла: если процесс прервётся здесь, файл
    # останется на месте и будет переобработан при следующем запуске —
    # благодаря UNIQUE(content_hash) это даст максимум безвредный
    # INSERT OR IGNORE, а не дубль или потерю.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest_path = ARCHIVE_DIR / f"{file_path.stem}_{stamp}{file_path.suffix}"

    success, _ = retry_on_permission_error(
        lambda: shutil.move(str(file_path), str(dest_path)),
        max_attempts=config.IO_RETRY_MAX_ATTEMPTS,
        base_delay_s=config.IO_RETRY_BASE_DELAY_S,
        op_description=f"Archive move ({file_path.name})",
    )
    if not success:
        raise PermissionError(
            f"Could not move {file_path.name} to archive after retries"
        )

    return status


def index_all_files():
    ARCHIVE_DIR.mkdir(exist_ok=True, parents=True)

    # Раньше: files = list(INPUT_DIR.glob("*.txt")) — это делало категорию
    # code_context в PATTERN_MAP структурно недостижимой: .py/.js/.sql файлы
    # физически не попадали в выборку, даже если их контент содержал маркеры
    # классификации. Подтверждено тестом на реальной файловой системе.
    #
    # ARCHIVE_DIR лежит внутри INPUT_DIR — это безопасно, потому что glob()
    # (в отличие от rglob()) не рекурсивен и не спускается в поддиректории;
    # подтверждено отдельным тестом, что архивные файлы не попадают повторно
    # в выборку даже при расширении набора расширений.
    files = []
    for ext in ALLOWED_EXTENSIONS:
        files.extend(INPUT_DIR.glob(f"*{ext}"))
    files = sorted(files, key=lambda p: p.name)

    # Защита от символических ссылок, указывающих за пределы INPUT_DIR.
    # glob() (подтверждено тестом) видит symlink как обычный файл — он не
    # фильтрует ссылки сам по себе. Path.resolve() внутри guard_path
    # разрешает симлинк до реального целевого пути ДО сравнения с
    # allowed_root: если цель снаружи INPUT_DIR, файл отбрасывается здесь,
    # не доходя до read_text()/shutil.move() ниже.
    safe_files = []
    for f in files:
        try:
            guard_path(f, allowed_root=INPUT_DIR)
            safe_files.append(f)
        except SecurityAccessViolation as e:
            logger.critical("Отказ обработки файла вне разрешённой зоны: %s | %s", f.name, e)
    files = safe_files

    logger.info("Найдено файлов для обработки: %d", len(files))

    if not files:
        logger.info("Нет новых файлов. Индексация не требуется.")
        return

    with contextlib.closing(db.get_connection()) as conn:
        for file_path in files:
            try:
                process_single(file_path, conn)
            except Exception as e:
                logger.error("Ошибка при обработке %s: %s", file_path.name, e)
                # Файл остаётся в INPUT_DIR при ошибке — не теряется и не архивируется
                continue

    logger.info("Индексация завершена.")


def log_pulse_anomaly_alert():
    """Записывает алерт об аномалии пульса напрямую в БД, минуя файловую очередь."""
    alert_text = f"Аномальный всплеск пульса. Требуется внимание. ({datetime.now().isoformat()})"
    content_hash = hashlib.sha256(alert_text.encode("utf-8")).hexdigest()
    try:
        with contextlib.closing(db.get_connection()) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO knowledge (content, content_hash, source, category)
                   VALUES (?, ?, ?, ?)""",
                (alert_text, content_hash, "trigger", "biometric_log")
            )
            conn.commit()
        logger.info("Алерт пульсовой аномалии записан в БД.")
    except sqlite3.DatabaseError as e:
        # DatabaseError — базовый класс для OperationalError/IntegrityError/etc,
        # не только "БД занята" (OperationalError) — найдено при честном
        # ревью реального файла: process_single() использует общий except
        # Exception, эта функция была у́же без явной причины.
        logger.error("Не удалось записать алерт пульсовой аномалии: %s", e)


def main():
    # Легаси-путь: до интеграции Task B (HITL staging) этот main() был
    # точкой входа, вызываемой run_system.py через subprocess с флагом
    # --trigger. Теперь run_system.py вызывает StagingManager.move_to_staging()
    # и (после approve) knowledge_indexer.process_single() напрямую — этот
    # CLI-путь больше не используется реальной архитектурой, но сохранён
    # рабочим для ручного запуска `python knowledge_indexer.py --trigger ...`.
    oko_logger.setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", help="Тип триггера (например, pulse_anomaly)")
    args = parser.parse_args()

    setup_database()

    if args.trigger == "pulse_anomaly":
        logger.info("Получен сигнал пульсовой аномалии.")
        log_pulse_anomaly_alert()

    index_all_files()


if __name__ == "__main__":
    main()
