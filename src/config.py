"""
config.py — единый валидатор конфигурации и переменных окружения ядра.
Защищает систему от запуска с неполными, некорректными или платформенно
непереносимыми параметрами.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(
            f"FATAL: переменная окружения {name} не задана. "
            f"Проверьте файл .env и добавьте туда {name}."
        )
    return value


def _require_env_int(name: str) -> int:
    raw = _require_env(name)
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"FATAL: {name} должна быть числом, получено: {raw!r}")


def _get_env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --- Транспорт Telegram (Bot API) ---
TELEGRAM_TOKEN: str = _require_env("TELEGRAM_TOKEN")

# --- Локальная инфраструктура LLM (Ollama) ---
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "localhost")
OLLAMA_PORT: int = int(os.getenv("OLLAMA_PORT", "11434"))
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gemma2:latest")

# Модель для эмбеддингов (knowledge_indexer.py, RAG-контур). Отдельная
# переменная, не переиспользуем OLLAMA_MODEL — это принципиально другая
# модель другого назначения (embedding, не генерация текста), и в будущем
# они должны меняться независимо друг от друга через .env.
OLLAMA_EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# --- Таймауты и лимиты governor ---
LLM_TIMEOUT: float = _get_env_float("LLM_TIMEOUT", 30.0)
JUDGE_TIMEOUT: float = _get_env_float("JUDGE_TIMEOUT", 15.0)
MAX_REFLECTION_DEPTH: int = int(os.getenv("MAX_REFLECTION_DEPTH", "3"))

# PATCH_V2_STAGNATION: сколько ОДИНАКОВЫХ сбоев подряд (одна и та же
# сигнатура ошибки) считается застоем рефлексии. Обязан быть меньше
# MAX_REFLECTION_DEPTH, иначе защита от застоя недостижима (сработает
# жёсткий лимит глубины раньше) -- проверяется инвариантом ниже.
MAX_STAGNATION_CYCLES: int = int(os.getenv("MAX_STAGNATION_CYCLES", "2"))


def check_stagnation_invariant(stagnation=None, depth=None) -> None:
    """FATAL-стоп (как _require_env), если нарушено
    1 <= MAX_STAGNATION_CYCLES < MAX_REFLECTION_DEPTH. Без аргументов
    читает ТЕКУЩИЕ значения модуля (их можно подменить в тесте)."""
    s = MAX_STAGNATION_CYCLES if stagnation is None else stagnation
    d = MAX_REFLECTION_DEPTH if depth is None else depth
    if not (1 <= s < d):
        sys.exit(
            f"FATAL: нарушен инвариант 1 <= MAX_STAGNATION_CYCLES ({s}) < "
            f"MAX_REFLECTION_DEPTH ({d}). Проверьте .env."
        )


check_stagnation_invariant()

# --- Concurrency ---
# Единый лимит одновременных запросов к Ollama. Раньше существовал в двух
# не связанных друг с другом местах (GPU_LIMITER в bot.py и неограниченный
# доступ из governor.py) — теперь это одна точка правды, governor оборачивает
# _route_to_llm этим семафором, bot.py больше не создаёт собственный лимитер.
GPU_CONCURRENCY: int = int(os.getenv("GPU_CONCURRENCY", "1"))

# --- Пути и директории (платформонезависимые) ---
# ВАЖНО: default — относительный путь ("./data"), не Windows-специфичный.
# Path(r"C:\AI") на Linux/macOS не бросает исключение — он трактуется как
# буквальное имя поддиректории с обратными слешами в названии, что тихо
# создаёт мусорную структуру файлов вместо ожидаемого пути. Если деплой
# реально на Windows и нужен C:\AI — это задаётся через .env (AI_BASE_DIR),
# не зашивается как платформенно-зависимый дефолт в код.
#
# КРИТИЧЕСКИ ВАЖНО при относительном AI_BASE_DIR (например "../data" в
# регламенте запуска из src/): Path(...).resolve() разрешает путь
# относительно cwd ПРОЦЕССА на момент импорта этого модуля, не относительно
# расположения config.py. Подтверждено тестом: если оператор забудет
# `cd src` и запустит `python src/bot.py` из корня проекта, тот же самый
# "../data" резолвится на уровень ВЫШЕ корня проекта, а не в oko/data —
# без единой ошибки, молча. Поэтому BASE_DIR логируется в stderr при
# каждом импорте — это единственная практическая защита, раз сам код не
# может знать, что оператор имел в виду.
BASE_DIR: Path = Path(os.getenv("AI_BASE_DIR", "./data")).resolve()
print(f"[config] BASE_DIR resolved to: {BASE_DIR}", file=sys.stderr)

DB_PATH: Path = BASE_DIR / "knowledge_base.db"
REGISTRY_FILEPATH: Path = BASE_DIR / os.getenv("REGISTRY_FILENAME", "registry_state.json")

# Карантин для входящих документов перед HITL-подтверждением (Task B,
# RAG Staging — см. историю решений, отделена от отложенной Task A,
# self-modification кода). Внутри BASE_DIR, не отдельная корневая
# переменная окружения — нет причины конфигурировать её независимо от
# остального data layer.
STAGING_DIR: Path = BASE_DIR / "staging"

# Снапшоты файлов src/ перед применением патча (Task A, Human-in-the-Loop
# Modification — см. историю решений). Снапшотится КОНКРЕТНЫЙ файл, который
# патчится, не только заранее зафиксированный список Sacred Areas — иначе
# откат не сработал бы для патча, затрагивающего файл за пределами этого
# списка. Sacred Areas — это про требование более строгого impact-analysis
# перед патчем, не про то, какие файлы вообще защищены backup'ом.
SNAPSHOT_DIR: Path = BASE_DIR / "snapshots"

# --- Резервные креденшы (MTProto / опционально, не используется сейчас) ---
# Опциональны намеренно: MTProto-транспорт (telethon) не используется в
# текущей архитектуре (см. историю решений — выбор сделан в пользу Bot API).
# Если когда-нибудь потребуется — эти поля уже на месте, без FATAL на старте.
TELEGRAM_API_ID: str = os.getenv("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "")

# --- Bot-level admin access ---
_admin_ids_raw = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS: frozenset[int] = frozenset(
    int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip()
)

# --- Алертинг (мониторинг деградации через JobQueue в bot.py) ---
# ALERT_ERROR_THRESHOLD применяется к governor.monitor.get_error_rate() —
# это окно window_size=100 (см. AgentGovernor.__init__), не отдельное окно.
# Решение не плодить вторую очередь: ComplianceMonitor уже хранит нужную
# историю, алертинг — это просто другой потребитель того же состояния.
ALERT_ERROR_THRESHOLD: float = _get_env_float("ALERT_ERROR_THRESHOLD", 0.15)

# Минимальный sample_size перед тем, как алертинг начинает доверять
# error_rate. Без этого порога get_error_rate() == 0.0 при пустом окне
# (см. monitor.py — пустой/почти пустой deque даёт 0.0, не "неизвестно")
# выглядел бы как "всё хорошо" сразу после рестарта процесса, хотя на
# самом деле это просто отсутствие данных, а не подтверждённое здоровье.
ALERT_MIN_SAMPLE_SIZE: int = int(os.getenv("ALERT_MIN_SAMPLE_SIZE", "10"))

# Интервал опроса JobQueue, в секундах. 60с — компромисс между
# оперативностью обнаружения деградации и не-перегрузкой бота частыми
# проверками. При низком трафике (см. историю решений — ожидается
# индивидуальное использование) более частый опрос не даёт выигрыша:
# окно из ALERT_MIN_SAMPLE_SIZE=10 событий не наполнится быстрее, чем
# реально приходят сообщения.
ALERT_CHECK_INTERVAL_S: float = _get_env_float("ALERT_CHECK_INTERVAL_S", 60.0)

# Отдельный, более частый интервал для опроса pending_tasks (HITL staging
# из истории решений — Task B). Это про UX, не про здоровье системы:
# задержка алертинга на деградацию в пару минут некритична, но если
# пользователь положил файл в knowledge/, он ожидает уведомление о
# необходимости approve в течение нескольких секунд, не минут. Проверка
# реестра — чтение небольшого in-memory состояния, не тяжёлая операция,
# 10с не создают заметной нагрузки.
STAGING_CHECK_INTERVAL_S: float = _get_env_float("STAGING_CHECK_INTERVAL_S", 10.0)

# --- process_watchdog.py (Sidecar process health monitor) ---
# Не путать с библиотекой watchdog (файловый мониторинг, используется в
# run_system.py) — process_watchdog.py — наш собственный скрипт, отдельная
# вещь. Отвечает за "логику здоровья" (норма/деградация), не за бинарное
# состояние процесса (это ответственность systemd/NSSM, Restart=on-failure
# уже настроен в deploy/*.service). Если watchdog видит деградацию — он
# посылает SIGTERM целевому процессу и логирует причину; supervisor
# (systemd/NSSM) решает, перезапускать или нет. Watchdog НЕ вызывает
# SnapshotManager.rollback() сам — откат кода требует решения человека
# (см. историю решений: автоматический rollback на основании метрики
# создаёт риск "качелей" rollback -> auto-restart -> crash -> rollback).
WATCHDOG_MEMORY_LIMIT_MB: int = int(os.getenv("WATCHDOG_MEMORY_LIMIT_MB", "512"))
# PATCH_V2_STAGNATION: порог светофора -- SIGTERM на 6-й красной строке за окно
# (проверка в watchdog: count > limit, limit=5 => срабатывает на 6-й).
WATCHDOG_ERROR_RATE_LIMIT: int = int(os.getenv("WATCHDOG_ERROR_RATE_LIMIT", "5"))
WATCHDOG_ERROR_WINDOW_SEC: float = _get_env_float("WATCHDOG_ERROR_WINDOW_SEC", 60.0)
WATCHDOG_HEARTBEAT_TIMEOUT_SEC: float = _get_env_float("WATCHDOG_HEARTBEAT_TIMEOUT_SEC", 120.0)
WATCHDOG_CHECK_INTERVAL_SEC: float = _get_env_float("WATCHDOG_CHECK_INTERVAL_SEC", 10.0)

# --- Operational Latency Profile (Pacing, не Retry) ---
# Retry (io_retry.py) — реактивный механизм: операция упала, ждём, пробуем
# снова. Pacing — превентивный: снижаем ЧАСТОТУ операций заранее, чтобы
# Windows I/O subsystem (антивирус/EDR/индексатор) не успевала триггерить
# блокировки в принципе, не только реагировать на них постфактум.
#
# ВАЖНО: это НЕ управляет холостым циклом ожидания в run_system.main()
# (while True: time.sleep(1) — тот существует только чтобы не дать
# главному потоку завершиться, пока observer работает в фоне; он не influences
# частоту реальных файловых операций, watchdog событийный — inotify на
# Linux, ReadDirectoryChangesW на Windows — не активный polling с интервалом).
# Profile управляет параметрами, которые РЕАЛЬНО определяют частоту
# обращений к диску: DEBOUNCE_SECONDS (как быстро после изменения файла
# запускается индексатор) и retry-параметры в io_retry (как агрессивно
# повторяются попытки при конфликте).
#
# Платформа определяется один раз при импорте через sys.platform, не
# разбрасывается как if sys.platform == 'win32' по всему коду — единая
# точка решения, как и остальные платформенные особенности в этом файле.
_IS_WINDOWS = sys.platform == "win32"

# DEBOUNCE_SECONDS — переопределяем дефолт в зависимости от платформы, ЕСЛИ
# переменная окружения не задана явно. Явный DEBOUNCE_SECONDS в .env всегда
# имеет приоритет — platform-профиль это только дефолт, не принудительное
# значение.
_DEBOUNCE_DEFAULT = "3.0" if _IS_WINDOWS else "2.0"
DEBOUNCE_SECONDS: float = _get_env_float("DEBOUNCE_SECONDS", float(_DEBOUNCE_DEFAULT))

# Retry-профиль для io_retry.retry_on_permission_error. На Windows даём
# больше попыток с более долгой базовой задержкой — выше реальный шанс,
# что антивирус/индексатор отпустят хендл за это время, без необходимости
# трогать сам алгоритм retry (он платформонезависим, меняются только числа).
IO_RETRY_MAX_ATTEMPTS: int = int(
    os.getenv("IO_RETRY_MAX_ATTEMPTS", "7" if _IS_WINDOWS else "5")
)
IO_RETRY_BASE_DELAY_S: float = _get_env_float(
    "IO_RETRY_BASE_DELAY_S", 0.2 if _IS_WINDOWS else 0.1
)

# Ядро памяти: отдельно от knowledge_base.db
MEMORY_DB_PATH: Path = BASE_DIR / "memory" / "memory.db"
MEMORY_BACKUP_DIR: Path = BASE_DIR / "memory" / "backups"

# --- M7.1/M7.2 «сон»: сводка лога и индекс дрейфа (только наблюдение) ---
# ops.db — служебная БД, отдельно от знаний и памяти (решение 6, 19.10).
OPS_DB_PATH: Path = BASE_DIR / "ops.db"
# Лог OkoBot = файл NSSM AppStderr (logger.py пишет только в stderr).
# Пусто -> сон выключен (одна INFO-строка при первом тике), бот работает.
_OKO_LOG_FILE_RAW = os.getenv("OKO_LOG_FILE", "").strip()
OKO_LOG_FILE: "Path | None" = Path(_OKO_LOG_FILE_RAW) if _OKO_LOG_FILE_RAW else None
SLEEP_IDLE_S: float = _get_env_float("SLEEP_IDLE_S", 20 * 60.0)
SLEEP_MIN_INTERVAL_S: float = _get_env_float("SLEEP_MIN_INTERVAL_S", 6 * 3600.0)
SLEEP_CHECK_INTERVAL_S: float = _get_env_float("SLEEP_CHECK_INTERVAL_S", 300.0)

# --- Метрика S (векторный маркер): только наблюдение, порогов нет ---
# OKO_VECTOR_S=0 -> выключено (откат без правки кода). Модель -- OLLAMA_EMBED_MODEL.
VECTOR_S_ENABLED: bool = os.getenv("OKO_VECTOR_S", "1").strip().lower() not in ("0", "false", "no", "off")

# --- Рубеж погружения, этап 1 (только наблюдение, порогов нет) ---
# Битые/неположительные значения -> дефолт (наблюдение не роняет бота).
# Имена атрибутов = имена в getattr(config, ...) governor/bot; сверяет
# test_config_switches.py (иначе переключатель мёртв, а тесты зелёные).
# OKO_CTX_LOG=0 -> строки [CTX] выключены (откат без правки кода).
OKO_CTX_LOG: bool = os.getenv("OKO_CTX_LOG", "1").strip().lower() not in ("0", "false", "no", "off")


def _get_env_pos_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Знаменатель fill в [CTX], если в options нет num_ctx. Должен совпадать с
# реальным контекстом рантайма Ollama (/api/ps context_length; 4096 при
# незаданном OLLAMA_CONTEXT_LENGTH). На сам num_ctx запросов НЕ влияет.
OKO_RUNTIME_NUM_CTX: int = _get_env_pos_int("OKO_RUNTIME_NUM_CTX", 4096)

# Граница сессии для [DEPTH]: пауза между ответами > N секунд. На историю
# диалога НЕ влияет (граница истории -- только /newepoch, M7.3).
_depth_pause = _get_env_float("OKO_DEPTH_PAUSE_S", 2400.0)
OKO_DEPTH_PAUSE_S: float = _depth_pause if _depth_pause > 0 else 2400.0

# --- Этап 2 рубежа (наблюдение, 25.10) ---
# engaged_s в [DEPTH]: сумма интервалов между ответами <= OKO_ENGAGED_GAP_S
# («плотный диалог»). active_s/turns/span_s и OKO_DEPTH_PAUSE_S не меняются.
_engaged = _get_env_float("OKO_ENGAGED_GAP_S", 300.0)
OKO_ENGAGED_GAP_S: float = _engaged if _engaged > 0 else 300.0
# Граница базы дрейфа (unix ts): сводки с окном раньше неё в базу не входят.
# Пусто/битое -> None (база с первой сводки, как раньше). Значение — в .env
# (смена identity 25.10 17:05), не в коде.
def _get_env_float_or_none(name: str) -> "float | None":
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


# Присваивание на верхнем уровне модуля: test_config_switches сверяет имена через ast.
OKO_DRIFT_BASELINE_SINCE: "float | None" = _get_env_float_or_none("OKO_DRIFT_BASELINE_SINCE")

# --- M7.4 RAG-гигиена во сне (только отчёт, лечение руками) ---
# OKO_RAG_HYGIENE=0 -> скан SQLite<->ChromaDB перед сводкой сна не делается.
OKO_RAG_HYGIENE: bool = os.getenv("OKO_RAG_HYGIENE", "1").strip().lower() not in ("0", "false", "no", "off")
