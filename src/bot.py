"""
bot.py — интерфейсный I/O-слой системы.
Отвечает за транспорт сообщений Telegram, ведение сессий пользователей в SQLite,
первичную фильтрацию ввода и передачу запроса в изолированное ядро AgentGovernor.
"""

import re
import logging
import sqlite3
import asyncio
import os
import time
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import db
import logger as oko_logger
# GUARDRAILS_REMOVED (17.10): guardrails.py / RealityPrivacyDispatcher выведены
# в карантин -- их результат не использовался, входной фильтр -- DataHygieneFilter.
from src.data_hygiene import DataHygieneFilter, build_notice
from src.memory_core import MemoryCore, Envelope
from src.registry import Registry
from src.governor import AgentGovernor, DISCUSS_NUM_CTX
from src.monitor import ComplianceEvent, ComplianceMonitor
from src.staging_manager import StagingManager
from src.staging_repository import StagingRepository
import knowledge_indexer
import sleep_digest
import layout_fix
import tv_bot_handler
import device_watch
import net_context
import doc_print
import doc_intake
import stt
import tv_nav
import garmin_report
import camera_watcher   # CAMERA_WATCHER (03.11): кадр экрана ТВ с камеры по /cam
import capabilities      # CAPS_CATALOG (30.10): каталог функций для мозга
import proof_registry
import router            # ROUTER (этап B, 01.11): общий маршрутизатор текста и голоса
import event_log         # EVENTS (этап C, 01.11): события модулей -> факты для мозга
import game_mode         # GAME_MODE v2 (02.11): в игре бот живёт, мозг спит
import amb_music         # AMB_MUSIC (04.11): подсветка под музыку своим цветом, пульт /amb

# --- Унификация RAG-контура с console_chat.py (устранение Split Brain) ---
# knowledge_indexer.py (импортирован строкой выше) уже добавляет oko_rag/ в
# sys.path и пишет одобренный через Telegram контент в ChromaDB, если
# RAG_SYNC_AVAILABLE. До этой правки bot.py всё равно читал контекст только
# через _query_knowledge_db (SQL word-match по knowledge_base.db) — то есть
# писалось в оба хранилища, а читалось только из одного, независимо от
# того, что видел console_chat.py через retriever.build_context. См.
# докстринг retrieve_context() ниже для истории этого решения.
try:
    from retriever import build_context as _rag_build_context
    RAG_RETRIEVAL_AVAILABLE = True
except ImportError as e:
    oko_logger.get_logger("bot").warning(
        "retriever (oko_rag) недоступен (%s) — retrieve_context() "
        "откатывается на устаревший SQL word-match поиск по "
        "knowledge_base.db вместо семантического поиска через ChromaDB.", e
    )
    RAG_RETRIEVAL_AVAILABLE = False

logger = oko_logger.get_logger("bot")

# httpx пишет на уровне INFO полный URL каждого запроса, а у Telegram Bot
# API токен входит в сам путь (/bot<TOKEN>/getUpdates). Без этого ограничения
# токен оседает в логе при каждом опросе (раз в ~10 с) и утекает вместе с
# любым фрагментом лога, который кто-то покажет или отправит. Ошибки
# (WARNING и выше) по-прежнему пишутся.
# apscheduler: 2 строки INFO на каждый запуск job (staging каждые 10 с -> ~17 тыс. строк/сутки)
# топили реальные события в логе; пропуски и сбои job он пишет WARNING/ERROR -- они остаются.
for _noisy_logger in ("httpx", "httpcore", "apscheduler"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

HISTORY_LIMIT = 10  # последних сообщений (5 пар вопрос-ответ)
LOOP_GUARD_WINDOW = 10  # P1: сколько последних событий сверять
# P1_LOG: начало шаблона отказа identity (AEBF398B: «Не распознал запрос. Сформулируй
# задачу.»), в нижнем регистре после нормализации. Только для флага tpl в логе.
P1_TEMPLATE_PREFIX = "не распознал запрос"
P1_NOTE = "(P1: повтор ответа — обмен не сохранён в память)"
# D_GUARD (этап D, 01.11): модель заявила выполнение, которого не было (бот команду не распознал).
D_NOTE = ("⚠️ Модель написала «{word}», но бот это НЕ выполнял — команда не распознана. "
          "Ответ не сохранён в память. Скажи иначе, например «подсветка музыка» или «громкость плюс 5».")
# D1 (02.11, решение Оператора): выдуманная команда/путь API -> ответ скрыт целиком, только пометка.
D1_NOTE = ("⚠️ Ответ скрыт: модель выдумала команду «{cmd}» — такой у бота нет. "
           "Ответ не показан и не сохранён в память.\n"
           "Подсветка и звук ТВ — фразой без /tv: «подсветка музыка», «громкость плюс 5». "
           "Подсказка: «как сделать подсветку», «как убавить громкость». Список функций: «что умеешь».")
MAX_MSG_LEN = 4000

# Тематические ярлыки для PDF/документов, выбираемые вручную кнопкой в
# Telegram — не путать с classify_content() в knowledge_indexer.py, тот
# определяет технические категории (biometric_log/troubleshooting/etc.).
# Категория из этого словаря имеет приоритет над classify_content(), если
# была выбрана (см. историю решений — Развилка 1, post-hoc выбор).
CATEGORY_LABELS = {
    "cognitive": "Когнитивистика",
    "system0": "Система 0",
    "general": "Общее",
    "other": "Другое",
}

# --- /discuss: режим полнотекстового обсуждения одного документа ---
# (см. историю решений — отдельная фича от staging/HITL-пайплайна выше).
#
# discussion_sessions — state живёт ЗДЕСЬ, не в governor.py (см. историю
# решений, Развилка Г: Governor остаётся stateless-исполнительным ядром,
# bot.py — единственный владелец per-user состояния, тем же паттерном,
# что уже применён для _notified_uuids ниже). In-memory, per-process:
# сброс при рестарте бота — приемлемый UX (пользователь просто заново
# вызывает /discuss <filename>), не критичная потеря данных.
discussion_sessions: dict[int, str] = {}

# DISCUSS_RESERVED_TOKENS — бюджет на всё, что добавляется к тексту
# документа внутри промпта: identity_text (system), XML-обёртка
# <knowledge_base>/<user_input> из _build_user_prompt, сам вопрос
# пользователя, и место под ответ модели. 1500 — консервативный запас
# (см. историю решений, Развилка Б): лучше отклонить документ, который
# на грани лимита, чем узнать про переполнение уже после LLM-вызова.
DISCUSS_RESERVED_TOKENS = 1500

# CHARS_PER_TOKEN_ESTIMATE — эвристика для быстрой, дешёвой (без LLM-вызова)
# оценки числа токенов по длине текста. 2.5, не общепринятые для английского
# ~4 — потому что корпус документов (когнитивистика, "Система 0") в
# основном на русском, а кириллица токенизируется менее эффективно, чем
# латиница у большинства токенизаторов (см. историю решений, Развилка Б:
# консервативная оценка — лучше ложно отклонить документ, который на самом
# деле поместился бы, чем ложно принять и получить обрезанный/деградировавший
# ответ от Ollama).
CHARS_PER_TOKEN_ESTIMATE = 2.5


def _estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN_ESTIMATE)


# KNOWLEDGE_BUDGET (01.11): справка RAG режется по остатку окна модели. Всплеск
# [CTX] fill=0.842 (21:33:19) -- один фрагмент RAG 2530 симв. поверх пола ~0.61;
# retriever режет только по max_chars=3000 и по границе чанка, окно не учитывает.
# Порядок частей промпта не меняется; режется только справка, с хвоста.
KNOWLEDGE_GEN_RESERVE_TOKENS = int(os.getenv("OKO_KNOWLEDGE_GEN_RESERVE", "1024"))
PROMPT_OVERHEAD_CHARS = 700   # XML-обвязка _build_user_prompt + заголовки payload
KNOWLEDGE_MIN_CHARS = 300     # меньше -- обрывок без смысла, справка не включается
KNOWLEDGE_CUT_MARK = "\n[…справка обрезана по размеру окна модели]"
KNOWLEDGE_NO_ROOM = "Справка не включена: окно модели занято историей и фактами."


def _system_chars(governor) -> int:
    """Длина system prompt основного контура. Нет метода (мок в тестах) -- 0."""
    fn = getattr(governor, "system_prompt_chars", None)
    try:
        return int(fn()) if callable(fn) else 0
    except Exception:
        return 0


def _knowledge_budget(system_chars: int, history: str, facts: str, user_input: str) -> int:
    """Сколько символов справки помещается: (nctx - резерв генерации) * симв/токен
    минус остальные части промпта. nctx -- config.OKO_RUNTIME_NUM_CTX (то же
    значение, по которому считается fill в [CTX])."""
    nctx = int(getattr(config, "OKO_RUNTIME_NUM_CTX", 4096))
    usable = (nctx - KNOWLEDGE_GEN_RESERVE_TOKENS) * CHARS_PER_TOKEN_ESTIMATE
    used = (system_chars + len(history or "") + len(facts or "")
            + len(user_input or "") + PROMPT_OVERHEAD_CHARS)
    return max(0, int(usable - used))


def _fit_knowledge(knowledge: str, budget: int) -> str:
    """Справка в бюджет: без изменений, если влезает; иначе голова (ближайший чанк
    идёт первым) по последнему переводу строки + пометка. Бюджет < KNOWLEDGE_MIN_CHARS --
    честная строка «не включена» вместо обрывка."""
    if len(knowledge) <= budget:
        return knowledge
    if budget < KNOWLEDGE_MIN_CHARS:
        return KNOWLEDGE_NO_ROOM
    head = knowledge[: budget - len(KNOWLEDGE_CUT_MARK)]
    nl = head.rfind("\n")
    if nl >= len(head) // 2:
        head = head[:nl]
    return head.rstrip() + KNOWLEDGE_CUT_MARK


def _fetch_document_content(filename: str) -> Optional[str]:
    """Читает полный текст документа из SQLite knowledge.content по имени
    файла (см. историю решений: process_single() уже пишет туда полный
    текст для любого approved файла — PDF через load_document(), текстовые
    файлы как раньше — отдельного хранилища для /discuss заводить не
    пришлось).

    filename не имеет UNIQUE-ограничения в схеме (только content_hash) —
    если один и тот же filename переиндексировался несколько раз с разным
    содержимым, берём самую свежую запись (ORDER BY timestamp DESC).
    Возвращает None, если файл с таким именем не найден вообще.
    """
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT content FROM knowledge WHERE filename = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (filename,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _category_keyboard(task_uuid: str) -> InlineKeyboardMarkup:
    """Единая клавиатура категория+Approve/Reject в одном сообщении
    (см. историю решений, Развилка 1: post-hoc выбор, не отдельный шаг
    до/после Approve). Нажатие категории не убирает Approve/Reject —
    оно дописывает category в pending_tasks[uuid] и перерисовывает то же
    сообщение той же клавиатурой (см. handle_staging_callback), кнопки
    Approve/Reject остаются активны для последующего нажатия.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧠 Когнитивистика", callback_data=f"cat_cognitive_{task_uuid}"),
            InlineKeyboardButton("0️⃣ Система 0", callback_data=f"cat_system0_{task_uuid}"),
        ],
        [
            InlineKeyboardButton("📁 Общее", callback_data=f"cat_general_{task_uuid}"),
            InlineKeyboardButton("❓ Другое", callback_data=f"cat_other_{task_uuid}"),
        ],
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{task_uuid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{task_uuid}"),
        ],
    ])




class SystemZero:
    """
    Единый фильтр вывода модели.
    Вычищает вежливый шум (парафин) строго из готового ответа ядра.
    """
    def __init__(self):
        self.name = "Око"
        self.boilerplate_patterns = [
            r"(?i)As an AI language model.*",
            r"(?i)I apologize.*",
            r"(?i)It is important to remember.*",
            r"(?i)I am here to help you.*",
            r"(?i)However.*",
            r"(?i)\bпожалуйста\b",
            r"(?i)\bрад помочь\b",
            r"(?i)\bизвините\b",
            r"(?i)\bне могли бы\b",
        ]

    def clean_output(self, raw_text: str) -> str:
        if not raw_text:
            return "[ERR: EMPTY]"
        cleaned = raw_text
        for pattern in self.boilerplate_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r'\n\s*\n', '\n', cleaned)
        cleaned = re.sub(r' {2,}', ' ', cleaned)
        return cleaned.strip()

    def format_final(self, text: str) -> str:
        return f"[{self.name}]: {text}"


core = SystemZero()


def _query_knowledge_db(query: str, limit: int = 5) -> str:
    """Выполняет полнотекстовый поиск по словам в локальной базе знаний.

    ВАЖНО: встроенная функция SQLite LOWER() обрабатывает ТОЛЬКО ASCII —
    LOWER('Пульс') возвращает 'Пульс' без изменений, не 'пульс'. Это не
    то же самое ограничение, что у LIKE/NOCASE (тот хотя бы документирован
    как ASCII-only) — здесь поведение менее очевидно и было подтверждено
    отдельным тестом после того, как первая попытка фикса (LOWER(content)
    LIKE ?) тоже не сработала на кириллице.

    Решение: регистронезависимое сравнение выполняется в Python после
    выборки, не в SQL. Это означает чтение всей таблицы knowledge в
    память на каждый запрос — приемлемо для локальной однопользовательской
    базы знаний разумного размера, но НЕ масштабируется на большую базу.
    Если объём вырастет — нужен SQLite FTS5 (с юникод-токенизатором) или
    отдельная lowercase-колонка с индексом, не патч этой функции.

    Матчинг — word-boundary regex (\\b), не substring (см. историю решений,
    диагностика 16.07.2026 на фразе "purple hexagon lighthouse protocol
    seven"): старая substring-проверка (`w in content.lower()`) ловила
    короткие слова вроде "про" как подстроку внутри десятков нерелевантных
    слов ("проект", "проверка" и т.п.), засоряя full_context_payload
    нерелевантными документами. На реальном тесте это приводило к тому,
    что модель отвечала "информация отсутствует", хотя искомый факт
    физически присутствовал в контексте — просто был погребён под шумом
    из 4 нерелевантных совпадений при limit=5. \\b работает корректно и на
    кириллице: Python re по умолчанию Unicode-aware для \\w (ASCII-флаг
    не используется), граница слова определяется правильно для не-ASCII
    букв тоже.
    """
    if not config.DB_PATH.exists():
        return "База знаний пуста (knowledge_base.db не найдена)."

    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    if not words:
        return "Запрос слишком короткий для поиска."

    try:
        conn = db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT content, timestamp FROM knowledge ORDER BY timestamp DESC"
            )
            all_rows = cursor.fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.error(f"Knowledge DB query error: {e}")
        return "База знаний временно недоступна."

    # Паттерны компилируются один раз для всех строк, не заново на каждую
    # запись в цикле ниже — дешевле при большом количестве строк в knowledge.
    # re.IGNORECASE вместо ручного .lower() на content — слова уже в
    # нижнем регистре (query.lower() выше), контент матчится по флагу,
    # не через явное приведение регистра каждой строки.
    patterns = [re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE) for w in words]

    matched = [
        content for content, _ts in all_rows
        if any(p.search(content) for p in patterns)
    ][:limit]

    if not matched:
        return "Релевантный контекст не найден."
    return "\n---\n".join(matched)


def _query_rag_context(query: str) -> str:
    """Семантический поиск через retriever.build_context (ChromaDB).

    build_context сам по себе синхронный и блокирующий: get_embedding()
    делает requests.post к Ollama, collection.query() — дисковый I/O
    ChromaDB. Вызывается только из retrieve_context() через
    asyncio.to_thread — так же, как раньше был обёрнут _query_knowledge_db.
    """
    context, _hits = _rag_build_context(query, exclude=getattr(config, "OKO_RAG_BRAIN_EXCLUDE", ()))  # R2 (02.11)
    if not context:
        return "Релевантный контекст не найден."
    return context


async def retrieve_context(query: str) -> str:
    """Асинхронная обёртка над поиском контекста в базе знаний.

    ИЗМЕНЕНО (устранение Split Brain, см. историю решений): раньше здесь
    безусловно вызывался _query_knowledge_db — SQL word-match поиск,
    независимый от ChromaDB-контура, которым уже пользовался
    console_chat.py через retriever.build_context. knowledge_indexer.py
    писал одобренный через Telegram контент в оба хранилища, но читался
    отсюда только SQLite — семантический поиск был структурно недоступен
    боту, хотя данные для него уже лежали в ChromaDB.

    RAG_RETRIEVAL_AVAILABLE — фолбэк на случай, если oko_rag/ недоступен в
    окружении (см. try/except при импорте retriever выше, тот же паттерн,
    что RAG_SYNC_AVAILABLE в knowledge_indexer.py): деградация на старый
    SQL-поиск явная и залогированная при старте процесса, не молчаливая.
    """
    # RAG_OUTAGE_FIX: embeddings go to Ollama (requests.post). When Ollama is down
    # this is a transport failure (requests exceptions are OSError subclasses):
    # YELLOW (WARNING, not counted by process_watchdog), the request continues
    # with an empty context and the governor reports LLM_UNAVAILABLE itself.
    # Non-I/O errors (bugs) still propagate and stay RED.
    try:
        if RAG_RETRIEVAL_AVAILABLE:
            return await asyncio.to_thread(_query_rag_context, query)
        return await asyncio.to_thread(_query_knowledge_db, query)
    except OSError as e:
        logger.warning(
            "[RAG_UNAVAILABLE] retrieval skipped, transport failure (%s): %s",
            type(e).__name__, str(e)[:200],
        )
        return "Релевантный контекст не найден."


class _NullIndex:
    """Заглушка до подключения ChromaIndex (F2). События копятся в журнале;
    после подключения индекса — MemoryCore.rebuild_index()."""
    def upsert(self, event_id, subject_id, text): pass
    def delete_subject(self, subject_id): pass
    def search(self, subject_id, query, k): return []


_memory = None


def _get_memory() -> MemoryCore:
    """Ядро памяти: ключ — локальный subject_id, не Telegram user_id."""
    global _memory
    if _memory is None:
        hygiene = DataHygieneFilter()

        def _sanitize(t):
            r = hygiene.dispatch(t)
            return r.sanitized_text, [i.category for i in r.incidents]

        _memory = MemoryCore(config.MEMORY_DB_PATH, _NullIndex(), _sanitize,
                             backup_dir=config.MEMORY_BACKUP_DIR)
    return _memory


def _fetch_history(user_id: int, limit: int) -> list:
    """История из ядра памяти (порядок по event_id). M7.3: только текущая
    эпоха (/newepoch); без эпохи — как раньше. P1 loop guard использует ту же
    границу (см. _save_exchange)."""
    core = _get_memory()
    sid = core.resolve("telegram", str(user_id))
    return core.history(sid, limit, since_ts=core.epoch_start(sid))


def _save_exchange(user_id: int, user_text: str, assistant_text: str) -> bool:
    """Пара реплик одной транзакцией в журнал ядра. Слой B внутри.

    P1 (петля самоповтора): если ответ дословно совпадает с одним из последних
    ответов, а вопрос другой — это признак, что модель воспроизводит историю/RAG,
    а не отвечает. Такой обмен НЕ пишется: иначе он увеличивает вес паттерна
    в следующей истории (положительная обратная связь через память).

    P1_NOTE (26.10): возвращает False, если обмен НЕ записан (P1), иначе True --
    вызывающий код сообщает Оператору, что вопрос не попал в историю.
    P1_LOG (26.10): строка P1 самодостаточна для замера (без пары с Response routed):
    tpl=1 -- ответ начинается с шаблона отказа identity, len -- длина
    нормализованного ответа, back -- на сколько событий назад совпадение."""
    core = _get_memory()
    sid = core.resolve("telegram", str(user_id))
    norm = lambda t: " ".join(t.split()).lower()
    a_norm = norm(assistant_text)
    if len(a_norm) >= 40:
        # M7.3: окно P1 — только текущая эпоха. Ответы до /newepoch модель не
        # видит, обратной связи через память нет; без границы P1 блокировал
        # запись повторяющихся коротких ответов и эпоха не наполнялась (12:01:06).
        recent = core.history(sid, LOOP_GUARD_WINDOW, since_ts=core.epoch_start(sid))
        for i, (role, content) in enumerate(recent):
            if role == "assistant" and norm(content) == a_norm:
                prev_q = recent[i - 1][1] if i > 0 and recent[i - 1][0] == "user" else None
                if prev_q is None or norm(prev_q) != norm(user_text):
                    # Префикс строки не менять: по нему P1 ищут hud_state/sleep_digest/замер.
                    logger.warning(
                        "P1 loop guard: repeated answer not persisted (user=%s) tpl=%d len=%d back=%d",
                        user_id, int(a_norm.startswith(P1_TEMPLATE_PREFIX)), len(a_norm),
                        len(recent) - i,
                    )
                    return False
    core.record_exchange(
        Envelope("telegram", str(user_id), user_text), user_text, assistant_text
    )
    return True


def _with_p1_note(response: str, persisted) -> str:
    """P1_NOTE: пометка в НАЧАЛЕ ответа (конец может обрезаться MAX_MSG_LEN),
    только при явном False. None (заглушка в тестах/старый контракт) -- без пометки."""
    return f"{P1_NOTE}\n{response}" if persisted is False else response


def _is_error_marker(response) -> bool:
    """Ответ ядра — служебный маркер сбоя ([ERR: LLM_UNAVAILABLE],
    [ERR: SYSTEM_DEGRADED], ...), а не реплика ассистента. Такие маркеры
    нельзя писать в историю диалога: _fetch_history вернёт их в следующий
    промпт как 'ASSISTANT: [ERR: ...]', и модель начнёт воспринимать
    системные сбои как часть разговора."""
    return isinstance(response, str) and response.lstrip().startswith("[ERR:")


_ERR_TEXT = {
    "LLM_UNAVAILABLE": "Модель не ответила за отведённое время или недоступна. "
                       "Запрос не сохранён в память. Помогает более короткий запрос или повтор через минуту.",
    "SYSTEM_DEGRADED": "Предохранитель открыт: среди последних проверенных ответов слишком много нарушений, поэтому запросы временно не отправляются модели. Повторите через минуту.",
    "HALT_CONDITION": "Ответ модели заблокирован проверкой (логическое противоречие).",
    "RETRY_LIMIT_EXCEEDED": "Не удалось получить корректный ответ после нескольких попыток.",
    "RECOVERY_FAILED": "Сбой при попытке восстановления ответа.",
    # JUDGE_FAIL_CLOSED
    "JUDGE_UNPARSEABLE": "Проверка не дала понятного вердикта, поэтому ответ не показан. "
                         "Попробуйте переформулировать запрос.",
    "COMPLIANCE_UNRESOLVED": "Ответ нарушал правила и не исправился после попытки коррекции, "
                             "поэтому не показан.",
}


def _explain_error(response: str) -> str:
    """Служебный маркер [ERR: X] -> код + понятное объяснение. Код сохраняется
    для диагностики, объяснение - для Оператора. Остальной текст не трогается."""
    # MARKER_LINE_ONLY: объясняем только последнюю непустую строку, если она --
    # ровно известный маркер. Цитаты маркера внутри текста не трогаем.
    if not isinstance(response, str) or not response:
        return response
    lines = response.split("\n")
    idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    if idx is None:
        return response
    m = re.fullmatch(r"\[ERR: (\w+)\]", lines[idx].strip())
    if not m or m.group(1) not in _ERR_TEXT:
        return response
    lines[idx] = f"{m.group(0)} {_ERR_TEXT[m.group(1)]}"
    return "\n".join(lines)


def _transport_note(governor, response) -> str:
    """TRANSPORT_GATE: при открытом предохранителе связи -- сколько ждать
    следующей автоматической проверки Ollama. Пусто, если ответ не
    [ERR: LLM_UNAVAILABLE] или предохранитель закрыт."""
    if not (isinstance(response, str) and response.strip() == "[ERR: LLM_UNAVAILABLE]"):
        return ""
    try:
        seconds = float(governor.transport_retry_in())
    except Exception:
        return ""
    if seconds <= 0:
        return ""
    return f" Модель недоступна (служба Ollama не отвечает), автоповтор связи через ~{seconds:.0f} с."


def _explain_for_user(governor, response) -> str:
    """GATE_TEXT_FIX: при открытом предохранителе связи стандартное пояснение
    LLM_UNAVAILABLE ("короткий запрос или повтор через минуту") неверно --
    длина запроса ни на что не влияет. Тогда только маркер + _transport_note."""
    note = _transport_note(governor, response)
    if note:
        return response.strip() + note
    return _explain_error(response)


def _with_notice(response: str, incidents) -> str:
    """Уведомление слоя B в начале ответа (конец может обрезаться)."""
    notice = build_notice(list(incidents or []))
    return f"{notice}\n{response}" if notice else response


def _log_depth(user_id: int) -> None:
    """[DEPTH] (этап 1 рубежа погружения): глубина/длительность текущей сессии
    субъекта — одна INFO-строка без user_id и текста. Только чтение memory.db.
    Сбой наблюдателя не ломает ответ (WARNING, не ERROR -> не считается watchdog)."""
    try:
        core = _get_memory()
        sid = core.resolve("telegram", str(user_id))
        pause = float(getattr(config, "OKO_DEPTH_PAUSE_S", 2400))
        engaged_gap = float(getattr(config, "OKO_ENGAGED_GAP_S", 300))
        st = core.session_stats(sid, pause, since_ts=core.epoch_start(sid),
                                pause_within_epoch=True, engaged_gap_s=engaged_gap)
        logger.info("[DEPTH] turns=%d active_s=%.1f span_s=%.1f pause_s=%.0f engaged_s=%.1f gap_s=%.0f",
                    st["turns"], st["active_s"], st["span_s"], pause, st["engaged_s"], engaged_gap)
    except Exception as e:
        logger.warning("[DEPTH] fail=%s %s", type(e).__name__, e)


# R7 (02.11): сбой /force_brain в игре -- человеческий текст вместо служебного кода.
FORCE_FAIL_MARKERS = ("[ERR: LLM_TIMEOUT]", "[ERR: LLM_UNAVAILABLE]")
FORCE_FAIL_TEXT = ("⏱ Модель не успела ответить: видеокарта занята игрой. "
                   "Попробуй ещё раз или после игры.")


async def process_logic(
    user_input: str,
    user_id: int,
    governor: AgentGovernor,
    dispatcher,
    input_monitor: ComplianceMonitor,
) -> Optional[str]:
    """Реализует основной контур обработки: фильтрация, сбор данных, вызов ядра.

    input_monitor — отдельное скользящее окно от governor.monitor: то
    отслеживает качество ОТВЕТОВ модели, это — качество ВХОДНОГО трафика
    (как часто dispatcher блокирует запросы). Разные окна — разные вопросы:
    "пользователи шлют мусор" не равно "модель генерирует плохие ответы".
    Записываем VIOLATION/PASS на КАЖДЫЙ вызов dispatch(), не только на
    блокировку — без PASS-событий окно показывало бы error_rate=1.0 всегда,
    т.к. в него попадали бы только случаи блокировки.
    """
    if game_mode.brain_asleep(config):   # GAME_MODE v2 (02.11): до истории/RAG/LLM, обмен не пишется
        logger.info("[GAME_MODE] brain=sleep user=%s", user_id)
        return game_mode.SLEEP_TEXT
    result = await asyncio.to_thread(dispatcher.dispatch, user_input)
    if not result.is_allowed:
        critical = next((i for i in result.incidents if i.level == "CRITICAL"), None)
        category = critical.category if critical else "UNKNOWN"
        logger.warning(f"Blocked input from user {user_id}: category={category}")
        # input_blocked_count — отдельный счётчик от compliance_violations.
        # Семантика различна: это блокировка НА ВХОДЕ (диспетчер отфильтровал
        # запрос до того, как он попал в LLM) — нормальная работа фильтра,
        # не признак деградации модели. compliance_violations остаётся
        # сигналом о том, что модель сгенерировала плохой ОТВЕТ — это
        # требует внимания к промпту/модели, а не к входному трафику.
        # Смешивание этих двух метрик в одном счётчике лишило бы алертинг
        # диагностической ценности — не было бы способа отличить "пользователи
        # шлют мусор, фильтр работает штатно" от "модель пробивает фильтры".
        governor.registry.increment_state("input_blocked_count")
        input_monitor.record_event(ComplianceEvent.VIOLATION)
        return f"[BLOCKED: {category}]"

    input_monitor.record_event(ComplianceEvent.PASS)
    sanitized_input = result.sanitized_text

    # R7=F1 (02.11): /force_brain в игре -- малая модель и короткий промпт: без истории и RAG (факты дома остаются).
    forced = game_mode.brain_mode(config) == game_mode.FORCE
    if forced:
        history_list, knowledge = [], ""
    else:
        history_list = await asyncio.to_thread(_fetch_history, user_id, HISTORY_LIMIT)
        knowledge = await retrieve_context(sanitized_input)
    logger.info(f"user={user_id} history_layers={len(history_list)} knowledge_len={len(knowledge)}")

    formatted_history = "(начало диалога)" if not history_list else "\n".join(
        f"{role.upper()}: {content}" for role, content in history_list
    )

    facts = _home_facts()               # EVENTS (этап C): факты дома -- в канале справочных данных
    # KNOWLEDGE_BUDGET (01.11): справка по остатку окна; knowledge_len выше -- длина ДО обрезки.
    sys_chars = _system_chars(governor)
    budget = _knowledge_budget(sys_chars, formatted_history, facts, sanitized_input)
    fitted = _fit_knowledge(knowledge, budget)
    # PROMPT_PARTS (02.11): размеры частей промпта на КАЖДЫЙ запрос. [KNOWLEDGE_CUT]
    # пишется только при обрезке -- по нему одному петлю «история съедает справку»
    # не измерить. Без user_id и текста; kn -- длина справки до->после бюджета.
    logger.info("[PROMPT_PARTS] sys=%d hist=%d layers=%d facts=%d q=%d budget=%d kn=%d->%d",
                sys_chars, len(formatted_history), len(history_list), len(facts or ""),
                len(sanitized_input), budget, len(knowledge), len(fitted))
    if fitted != knowledge:
        logger.info("[KNOWLEDGE_CUT] user=%s len=%d->%d budget=%d",
                    user_id, len(knowledge), len(fitted), budget)
        knowledge = fitted

    # Экранирование внутренних XML-тегов (knowledge_base/user_input) происходит
    # внутри governor._build_user_prompt — bot.py передаёт сырой текст,
    # не занимается санитизацией промпта самостоятельно.
    full_context_payload = (
        f"История диалога:\n{formatted_history}\n\n"
        f"Дополнительные справочные данные:\n{knowledge}"
    )
    if facts:
        full_context_payload += f"\n\n{facts}"

    try:
        raw_response = await governor.execute_request(
            user_input=sanitized_input,
            user_id=user_id,
            context=full_context_payload,
        )
    except Exception as e:
        logger.error(f"Governor execution failure for user={user_id}: {e}")
        return None

    if forced and isinstance(raw_response, str) and raw_response.strip() in FORCE_FAIL_MARKERS:
        logger.warning("[HISTORY_SKIPPED] [GAME_MODE] force=fail %s user=%s — exchange not persisted",
                       raw_response.strip(), user_id)
        return FORCE_FAIL_TEXT

    persisted = None  # P1_NOTE: False -> пометка Оператору; None -> без пометки
    if _is_error_marker(raw_response):
        # Сбой ядра — пользователь увидит маркер, но обмен в историю не
        # пишем (ни запрос, ни маркер): пара без настоящего ответа только
        # зашумила бы контекст следующих запросов.
        logger.warning("[HISTORY_SKIPPED] Core returned %s for user=%s — exchange not persisted",
                       raw_response.strip()[:40], user_id)
    else:
        api = router.fake_api(raw_response)   # D1 (02.11): выдуманная команда/путь API -- и на вопрос
        if api:
            logger.warning("[HISTORY_SKIPPED] [D_GUARD] fake_api user=%s cmd=%s — answer hidden, exchange not persisted",
                           user_id, api)
            return _with_notice(D1_NOTE.format(cmd=api), getattr(result, "incidents", None))
        fake = router.fake_done(sanitized_input, raw_response)   # D_GUARD (этап D)
        if fake:
            logger.warning("[HISTORY_SKIPPED] [D_GUARD] fake_done user=%s word=%s — exchange not persisted", user_id, fake)
            note = D_NOTE.format(word=fake)
            return f"{note}\n{_with_notice(raw_response, getattr(result, 'incidents', None))}"
        snap = router.state_snapshot(sanitized_input, raw_response)   # RZ2 (03.11): показать, не писать
        if snap:
            logger.info("[HISTORY_SKIPPED] [STATE_SNAPSHOT] user=%s sig=%s — answer shown, exchange not persisted",
                        user_id, snap)
        elif forced:   # R7: ответ малой модели без истории -- в историю не пишется
            logger.info("[HISTORY_SKIPPED] [GAME_MODE] force user=%s — exchange not persisted", user_id)
        else:
            persisted = await asyncio.to_thread(_save_exchange, user_id, sanitized_input, raw_response)
            await asyncio.to_thread(_log_depth, user_id)
    return _with_p1_note(_with_notice(raw_response, getattr(result, "incidents", None)), persisted)


async def process_discuss_logic(
    user_input: str,
    user_id: int,
    governor: AgentGovernor,
    dispatcher,
    input_monitor: ComplianceMonitor,
) -> Optional[str]:
    """Аналог process_logic для активного режима /discuss (см. историю
    решений — Развилка В/Г).

    Дисптчер входного трафика применяется как обычно — защита от prompt
    injection в самом вопросе пользователя не зависит от решения не гонять
    output через compliance-judge (это Развилка В, про ВЫХОД, не про вход);
    dispatcher.dispatch() не LLM-вызов, а быстрая эвристическая проверка,
    так что не противоречит цели снизить latency в discuss-режиме.

    НЕ вызывает _fetch_history/retrieve_context (History Bypass, см.
    историю решений) — единственный источник контекста здесь: полный текст
    активного документа + текущий вопрос. История чата только зашумляла бы
    и без того ограниченный DISCUSS_NUM_CTX, вытесняя из контекста часть
    самого документа.
    """
    if game_mode.brain_asleep(config):   # GAME_MODE v2 (02.11): до истории/RAG/LLM, обмен не пишется
        logger.info("[GAME_MODE] brain=sleep user=%s", user_id)
        return game_mode.SLEEP_TEXT
    result = await asyncio.to_thread(dispatcher.dispatch, user_input)
    if not result.is_allowed:
        critical = next((i for i in result.incidents if i.level == "CRITICAL"), None)
        category = critical.category if critical else "UNKNOWN"
        logger.warning(f"Blocked input (discuss mode) from user {user_id}: category={category}")
        governor.registry.increment_state("input_blocked_count")
        input_monitor.record_event(ComplianceEvent.VIOLATION)
        return f"[BLOCKED: {category}]"

    input_monitor.record_event(ComplianceEvent.PASS)
    sanitized_input = result.sanitized_text

    filename = discussion_sessions.get(user_id)
    if filename is None:
        # Гонка: сессия могла быть сброшена (/enddiscuss, либо файл исчез —
        # см. ниже) между тем, как handle_message проверил discussion_sessions,
        # и этим вызовом. Маловероятно (single-threaded event loop, нет
        # await между проверкой и вызовом), но не должно падать необработанным.
        return "[ERR: DISCUSS_SESSION_LOST]"

    content = await asyncio.to_thread(_fetch_document_content, filename)
    if content is None:
        # Документ пропал из базы знаний (например, ручное вмешательство
        # в БД) — сбрасываем сессию, а не оставляем пользователя в режиме,
        # который больше не может ничего вернуть.
        discussion_sessions.pop(user_id, None)
        logger.warning(
            "Discuss session for user=%s referenced missing document: %s",
            user_id, filename,
        )
        return f"⚠️ Документ '{filename}' более недоступен в базе знаний. Режим обсуждения сброшен."

    try:
        raw_response = await governor.execute_discuss_request(
            user_input=sanitized_input,
            document_text=content,
            user_id=user_id,
        )
    except Exception as e:
        logger.error(f"Discuss execution failure for user={user_id}: {e}")
        return None

    # Сохраняем в общую историю sessions — не для использования внутри
    # discuss-режима (History Bypass, см. выше), а для непрерывности
    # обычного режима после /enddiscuss и для /status-подобной диагностики.
    # Префикс [/discuss:filename] в user_text — чтобы при последующем чтении
    # истории в обычном режиме было явно видно, что это был другой контур,
    # не обычный RAG-ответ.
    persisted = None
    if _is_error_marker(raw_response):
        logger.warning("[HISTORY_SKIPPED] Discuss core returned %s for user=%s — exchange not persisted",
                       raw_response.strip()[:40], user_id)
    else:
        persisted = await asyncio.to_thread(
            _save_exchange, user_id, f"[/discuss:{filename}] {sanitized_input}", raw_response
        )
    return _with_p1_note(_with_notice(raw_response, getattr(result, "incidents", None)), persisted)


# EVENTS (этап C, 01.11): события модулей -> журнал событий -> факты в контексте мозга.
def _event(src: str, text: str) -> None:
    event_log.record(src, text)          # не бросает; до init() -- no-op


def _home_facts() -> str:
    """Состояние техники (память детектора, без сети) + события за сутки. Пусто -- контекст прежний."""
    try:
        return event_log.facts_block(device_watch.state_text())
    except Exception as e:
        logger.warning("[EVENTS] facts failed: %s: %s", type(e).__name__, e)
        return ""


def _devices_answer() -> str:
    state = device_watch.state_text()
    last = event_log.recent(n=1, src="devices", within_s=7 * 86400)
    if not state:
        state = ("Данных о технике пока нет: облако SmartThings ещё не опрошено после запуска "
                 "или наблюдение выключено.")
    tail = f"\nПоследнее событие: {event_log.line(last[0])}" if last else ""
    return "🧺 " + state + tail


# ROUTER (этап B, 01.11): команда устройству / вопрос о себе -> модуль, мимо модели.
def _proof_record(app: Application, reg, fn: str, status: str) -> None:
    """Исход модуля -> реестр подтверждений (только смена статуса/дня) -> каталог в governor."""
    try:
        if router.should_record(reg.latest().get(fn), status, time.strftime("%Y-%m-%d")):
            reg.record(fn, status, src="router")
            app.bot_data["governor"].set_capabilities(capabilities.build_catalog(reg.latest()))
    except Exception as e:
        logger.warning("[PROOF] record failed fn=%s: %s: %s", fn, type(e).__name__, e)


# CTX_FOLLOW (03.11): предмет последней исполненной команды ТВ на пользователя -- в памяти процесса, TTL минуты.
# «sdelaj ewe», «vikljuci ee» после команды подсветки связываются с ней (router._follow_phrase). Текст не логируется.
TV_CTX_TTL_S = 600
_tv_ctx: dict = {}


def _tv_ctx_set(user_id: int, subject: Optional[str], phrase: str) -> None:
    if subject:
        _tv_ctx[user_id] = (subject, phrase, time.monotonic())


def _tv_ctx_get(user_id: int) -> Optional[tuple]:
    c = _tv_ctx.get(user_id)
    if not c or time.monotonic() - c[2] > TV_CTX_TTL_S:
        return None
    return (c[0], c[1])


async def _route_reply(app: Application, user_id: int, text: str, raw: str) -> Optional[str]:
    """Ответ модуля или None (-> мозг). Текст пользователя в журнал не пишется."""
    try:
        if game_mode.brain_asleep(config):   # GAME_MODE v2: только правила, без модели смысла (LLM)
            r = await asyncio.to_thread(router.route, text, raw, None, _tv_ctx_get(user_id))
        else:
            r = await asyncio.to_thread(router.route, text, raw, router.default_intent, _tv_ctx_get(user_id))
        logger.info("[ROUTE] user=%s kind=%s reason=%s", user_id, r.kind, r.reason)
        if r.kind == router.BRAIN:
            return None
        reg = app.bot_data.get("proof_registry")
        if r.kind == router.SELF:
            answer = capabilities.human_text(reg.latest() if reg is not None else None)
            await asyncio.to_thread(_save_exchange, user_id, text, answer)
            return answer
        if r.kind == router.HINT:         # R1 (02.11): подсказка фраз ТВ, без модели и без истории
            return r.text
        if r.kind == router.CLARIFY:      # CLARIFY (03.11): неясно, к какой технике -- переспросить, без модели и истории
            return r.text
        if r.kind == router.TVSTATE:      # TV_STATE (03.11): последнее известное из журнала событий, без модели и истории
            return router.tv_state_text(event_log.recent(n=200, within_s=30 * 86400, src="tv"))
        if r.kind == router.DEVICES:      # EVENTS (этап C)
            return _devices_answer()
        if r.kind == router.TV:
            answer, outcome = await asyncio.to_thread(
                router.run_tv, user_id, r, tv_bot_handler.handle, tv_bot_handler.make_factory())
            fn = "tv"
        else:
            answer, outcome = await asyncio.to_thread(router.run_garmin, r, garmin_report.run)
            fn = "garmin"
        logger.info("[ROUTE] user=%s fn=%s outcome=%s", user_id, fn, outcome)
        if fn == "tv" and outcome in ("PASS", "WARN"):     # CTX_FOLLOW: предмет исполненной команды -- для продолжения
            _tv_ctx_set(user_id, router.tv_subject(r.reason), r.text)
        if outcome:                       # EVENTS (этап C): исполненное -- факт для мозга
            _event(fn, answer)
        if outcome and reg is not None:
            await asyncio.to_thread(_proof_record, app, reg, fn, outcome)
        return answer
    except Exception as e:
        logger.warning("[ROUTE] failed, fallback to brain: %s: %s", type(e).__name__, e)
        return None


async def _brain_answer(app: Application, user_id: int, text: str, reserve: int = 0) -> str:
    """Хвост handle_message для голоса: мозг (или /discuss) -> текст ответа."""
    governor: AgentGovernor = app.bot_data["governor"]
    dispatcher = app.bot_data["dispatcher"]
    input_monitor: ComplianceMonitor = app.bot_data["input_monitor"]
    if user_id in discussion_sessions:
        raw_response = await process_discuss_logic(text, user_id, governor, dispatcher, input_monitor)
    else:
        raw_response = await process_logic(text, user_id, governor, dispatcher, input_monitor)
    if raw_response is None:
        return "[ERR: SYSTEM_FAULT]"
    clean = core.clean_output(_explain_for_user(governor, raw_response))
    return core.format_final(clean[:max(200, MAX_MSG_LEN - len(core.name) - 4 - reserve)])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Маршрутизатор апдейтов Telegram. Ветвление в /discuss-режим
    (см. историю решений, Развилка Г): если user_id есть в
    discussion_sessions — весь текст, полученный от пользователя, идёт
    через process_discuss_logic/execute_discuss_request вместо обычного
    process_logic/execute_request. Проверка discussion_sessions здесь,
    а не внутри process_logic — единая точка принятия решения о маршруте,
    не размазанная по нескольким функциям.
    """
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    user_id = update.message.from_user.id
    # ADMIN_ALLOWLIST_CHAT: fail-closed, как /status и staging-кнопки.
    # Посторонний не получает ни ответа, ни доступа к LLM, RAG, памяти и
    # мониторам. Текст сообщения в лог не пишется (только id и длина).
    if user_id not in config.ADMIN_USER_IDS:
        logger.warning("Unauthorized message ignored: user_id=%s len=%d", user_id, len(text))
        return
    logger.info(f"Processing message from {user_id}: len={len(text)}")

    # LAYOUT_FIX (24.10): текст не в той раскладке ("ghbdtn", "зкшмуе") исправляется
    # до фильтра, памяти и модели; пользователь видит, как бот понял. Текст в лог не пишется.
    layout_note = ""
    fix = layout_fix.normalize(text)
    if fix.kind:
        logger.info("[LAYOUT_FIX] kind=%s words=%d len=%d", fix.kind, fix.words_fixed, len(text))
        text = fix.text
        layout_note = f"(понял как: «{fix.text[:80]}»)\n"

    # TV_MODE (28.10): в режиме телевизора текст идёт телевизору (команды «/…» сюда не попадают).
    if _tv_mode_active(user_id):
        raw_text = update.message.text.strip()   # исходный текст: layout_fix портит латиницу команд («vverh»)
        if _tv_exit_word(tv_nav.canonical(raw_text)):
            _tv_mode.pop(user_id, None)
            await update.message.reply_text("📺 Режим телевизора выключен.")
            return
        _tv_mode[user_id] = time.time() + TV_MODE_S
        await update.message.reply_text(await _tv_answer(user_id, raw_text))
        return

    # PRINT_FOLLOWUP (28.10): «печать» отдельным сообщением -> последний присланный документ (не в модель).
    want_print, print_pages = doc_print.wants_print(text)
    if want_print:
        last = _last_document.get(user_id)
        if last and time.time() - last[1] <= PRINT_FOLLOWUP_S:
            await _print_uploaded(update, last[0], print_pages)
        else:
            await update.message.reply_text(
                "🖨 Нет документа для печати за последние 10 минут. Пришли файл с подписью «печать» "
                "или сначала файл, потом «печать»."
            )
        return

    # ROUTER (этап B): не в /discuss -- там текст относится к документу.
    if user_id not in discussion_sessions:
        routed = await _route_reply(context.application, user_id, text, update.message.text.strip())
        if routed is not None:
            await _reply_with_retry(update.message, layout_note + routed, user_id)
            return

    governor: AgentGovernor = context.application.bot_data["governor"]
    dispatcher = context.application.bot_data["dispatcher"]
    input_monitor: ComplianceMonitor = context.application.bot_data["input_monitor"]

    if user_id in discussion_sessions:
        raw_response = await process_discuss_logic(
            text, user_id, governor, dispatcher, input_monitor
        )
    else:
        raw_response = await process_logic(text, user_id, governor, dispatcher, input_monitor)

    if raw_response is None:
        await update.message.reply_text("[ERR: SYSTEM_FAULT]")
        return

    clean_response = layout_note + core.clean_output(_explain_for_user(governor, raw_response))
    final_response = core.format_final(clean_response[:MAX_MSG_LEN - len(core.name) - 4])
    logger.info(f"Response routed to {user_id}: {final_response[:50]}")

    await _reply_with_retry(update.message, final_response, user_id)


# TG_SEND_RETRY (25.10): к моменту отправки ответ уже сгенерирован и записан в память
# (_save_exchange). Разовый сбой сети Telegram (httpx.ConnectError 17:07:56) терял ответ:
# история модели расходилась с тем, что видел Оператор, а строка шла ERROR (красная).
# Транспорт -> до 2 повторов, WARNING [TG_TRANSPORT] (M5b). TimedOut НЕ повторяется:
# сообщение могло уже дойти (дубль хуже потери). Не транспорт (BadRequest) -> ERROR.
SEND_RETRY_DELAYS_S = (2.0, 5.0)


async def _reply_with_retry(message, text: str, user_id) -> bool:
    from telegram.error import TimedOut
    for attempt in range(len(SEND_RETRY_DELAYS_S) + 1):
        try:
            await message.reply_text(text)
            if attempt:
                logger.info("[TG_TRANSPORT] send ok after retry=%d user=%s", attempt, user_id)
            return True
        except Exception as e:
            if not _is_tg_transport_error(e):
                logger.error("Telegram send error for user=%s: %s", user_id, e)
                return False
            if isinstance(e, TimedOut) or attempt >= len(SEND_RETRY_DELAYS_S):
                logger.warning("[TG_TRANSPORT] send fail final attempts=%d user=%s err=%s",
                               attempt + 1, user_id, type(e).__name__)
                return False
            logger.warning("[TG_TRANSPORT] send fail attempt=%d user=%s err=%s retry_in_s=%.0f",
                           attempt + 1, user_id, type(e).__name__, SEND_RETRY_DELAYS_S[attempt])
            await asyncio.sleep(SEND_RETRY_DELAYS_S[attempt])
    return False


async def handle_discuss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /discuss <filename> — включает режим полнотекстового
    обсуждения документа (см. историю решений, Развилка Г).

    Проверяет размер документа ПЕРЕД включением режима (Развилка Б,
    комбинация "дешёвая эвристика + жёсткий отказ") — не пытается
    отправить заведомо слишком большой документ в LLM и не обрезает его
    автоматически (обрезка уничтожает структуру статьи, см. историю
    решений). Закрыта для не-админов тем же принципом fail-closed, что
    /status и staging-callback'и — это управляющая команда, не обычный чат.
    """
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        logger.warning("Unauthorized /discuss attempt from user_id=%s", user_id)
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /discuss <filename>\n"
            "Имя файла — как оно записано в базе знаний (после Approve)."
        )
        return

    filename = " ".join(context.args).strip()

    content = await asyncio.to_thread(_fetch_document_content, filename)
    if content is None:
        await update.message.reply_text(
            f"⚠️ Документ '{filename}' не найден в базе знаний. "
            f"Проверьте точное имя файла (после /approve в Telegram)."
        )
        return

    estimated_tokens = _estimate_tokens(content)
    budget = DISCUSS_NUM_CTX - DISCUSS_RESERVED_TOKENS
    if estimated_tokens > budget:
        await update.message.reply_text(
            f"⚠️ Документ слишком объёмный для /discuss "
            f"(~{estimated_tokens} токенов, лимит ~{budget}). "
            f"Требуется суммаризация или разбивка на части — это отдельная, "
            f"пока не реализованная функциональность."
        )
        return

    discussion_sessions[user_id] = filename
    await update.message.reply_text(
        f"📖 Режим обсуждения включён: {filename} (~{estimated_tokens} токенов).\n"
        f"Задавайте вопросы по документу — обычные сообщения теперь идут "
        f"в этот контур. История предыдущего диалога не учитывается, "
        f"только текст документа и текущий вопрос.\n"
        f"/enddiscuss — выйти из режима."
    )


async def handle_enddiscuss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /enddiscuss — выключает режим /discuss, возвращает
    пользователя к обычному RAG-режиму (см. историю решений, Развилка Г).
    """
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        return

    filename = discussion_sessions.pop(user_id, None)
    if filename:
        await update.message.reply_text(f"📖 Режим обсуждения завершён: {filename}")
    else:
        await update.message.reply_text("Режим обсуждения не был активен.")


# TV_MODE (28.10): «/tv» включает режим, в нём текст и голос идут телевизору без префикса.
TV_MODE_S = 300
TV_MODE_EXIT = {"хватит", "выйти", "выход", "закончили", "конец", "выключи режим", "режим выключи"}
TV_MODE_ON = (
    "📺 Режим телевизора включён. «хватит» — выйти; 5 минут без команд — выключится сам.\n"
    "Пиши или говори: «вниз три», «вправо вправо ок», «назад», «домой».\n"
    "Запомнить пункт: «запомни настройки домой вниз два ок» → потом «открой настройки».\n"
    "«что помнишь» — список, «забудь настройки» — удалить. Экран видишь ты: бот пишет «отправил», не «сделал»."
)
_tv_mode: dict[int, float] = {}


def _tv_mode_active(user_id: int) -> bool:
    deadline = _tv_mode.get(user_id)
    if deadline is None:
        return False
    if time.time() > deadline:
        _tv_mode.pop(user_id, None)
        return False
    return True


def _tv_exit_word(text: str) -> bool:
    return re.sub(r"[^\w\s]", "", (text or "").lower()).strip() in TV_MODE_EXIT


async def _tv_answer(user_id: int, text: str) -> str:
    try:
        return await asyncio.to_thread(tv_bot_handler.handle, user_id, text, tv_bot_handler.make_factory())
    except Exception:
        logger.exception("[TV] обработчик упал")
        return "Сбой контура управления телевизором, подробности в журнале."


VOICE_DL_ATTEMPTS = 3


async def _download_voice(voice, path) -> None:
    """Голосовое из Telegram на диск. Повтор — только на транспортных сбоях (NetworkError/TimedOut)."""
    last = None
    for attempt in range(VOICE_DL_ATTEMPTS):
        try:
            tg_file = await voice.get_file(read_timeout=30, connect_timeout=20)
            await tg_file.download_to_drive(custom_path=str(path), read_timeout=60, connect_timeout=20)
            if attempt:
                logger.info("[VOICE] скачано после повтора %d", attempt)
            return
        except Exception as e:
            if not _is_tg_transport_error(e):
                raise
            last = e
            logger.warning("[VOICE] транспорт Telegram, попытка %d: %s: %s", attempt + 1, type(e).__name__, e)
            if attempt + 1 < VOICE_DL_ATTEMPTS:
                await asyncio.sleep(2.0 * (attempt + 1))
    raise last


# 30.10: предел ожидания распознавания. Бот обрабатывает сообщения по очереди — зависшее распознавание
# (13:52, снимок py-spy: поток в stt._probe) молча останавливало ВСЕ ответы. Поток не убить, бот — работает.
STT_TIMEOUT_S = float(os.getenv("OKO_STT_TIMEOUT_S", "60"))


async def _stt_warmup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Загрузка модели распознавания заранее (первая загрузка может качать модель из сети)."""
    try:
        _, dev = await asyncio.to_thread(stt.load)
        logger.info("[STT] прогрев: модель готова (%s)", dev)
    except Exception as e:
        logger.warning("[STT] прогрев не удался: %s: %s", type(e).__name__, e)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Голосовое -> текст (stt, локально) -> в режиме ТВ на телевизор. Текст в журнал не пишется."""
    msg = update.message
    if not msg or not msg.voice or not msg.from_user:
        return
    user_id = msg.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        return
    vdir = config.BASE_DIR / "voice_tmp"
    vdir.mkdir(parents=True, exist_ok=True)
    path = vdir / f"{msg.voice.file_unique_id}.ogg"
    try:
        await _download_voice(msg.voice, path)
    except Exception as e:
        path.unlink(missing_ok=True)
        logger.warning("[VOICE] не скачано: %s: %s", type(e).__name__, e)
        await msg.reply_text(f"🎙 Не скачал голосовое из Telegram ({type(e).__name__}) — сбой связи, "
                             f"не распознавания. Повтори.")
        return
    try:
        text = await asyncio.wait_for(asyncio.to_thread(stt.transcribe, path), timeout=STT_TIMEOUT_S)
    except TimeoutError:
        logger.error("[VOICE] распознавание не уложилось в %.0f с — прервано, бот продолжает работу", STT_TIMEOUT_S)
        await msg.reply_text("🎙 Распознавание зависло и прервано — бот работает дальше. Повтори голосовое; "
                             "если повторится — перезапусти службу.")
        return
    except stt.SttUnavailable as e:
        await msg.reply_text(f"🎙 Распознавание речи недоступно: {e}")
        return
    except Exception as e:
        logger.exception("[VOICE] сбой распознавания")
        await msg.reply_text(f"🎙 Не получилось распознать: {type(e).__name__}")
        return
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # после таймаута файл ещё открыт зависшим потоком распознавания
            pass
    logger.info("[VOICE] user=%s символов=%d режим_тв=%s", user_id, len(text), _tv_mode_active(user_id))
    if not text:
        await msg.reply_text("🎙 Не расслышал. Повтори чуть громче или ближе к микрофону.")
        return
    if _tv_mode_active(user_id):
        if _tv_exit_word(text):
            _tv_mode.pop(user_id, None)
            await msg.reply_text(f"🎙 понял: «{text}»\n📺 Режим телевизора выключен.")
            return
        _tv_mode[user_id] = time.time() + TV_MODE_S
        answer = await _tv_answer(user_id, text)
        await msg.reply_text(f"🎙 понял: «{text}»\n{answer}")
        return
    # ROUTER (этап B, 01.11): голос вне /tv -- тот же маршрут, что текст; не узнано -> мозг.
    heard = f"🎙 понял: «{text}»\n"
    if user_id not in discussion_sessions:
        routed = await _route_reply(context.application, user_id, text, text)
        if routed is not None:
            await msg.reply_text(heard + routed)
            return
    answer = await _brain_answer(context.application, user_id, text, reserve=len(heard))
    await _reply_with_retry(msg, heard + answer, user_id)


def _amb_keyboard(rows) -> InlineKeyboardMarkup:
    """AMB_MUSIC: (подпись, callback_data) из amb_music.panel -> кнопки Telegram (модуль telegram не знает)."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


async def handle_amb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/amb -- пульт подсветки под музыку (AMB_MUSIC, 04.11): ползунки R/G/B кнопками, статика/переход, старт/стоп.
    «/amb стоп», «/amb старт». Только админ, отказ молчаливый (как /tv). Файлы пульта и ожидание моста -- в потоке."""
    if not update.message or not update.message.from_user:
        return
    if update.message.from_user.id not in config.ADMIN_USER_IDS:
        return
    arg = " ".join(context.args).strip().lower() if context.args else ""
    note = ""
    try:
        if arg in ("стоп", "stop", "выкл", "выключи"):
            note = await asyncio.to_thread(amb_music.stop) or "🎵 Не работала."
        elif arg in ("старт", "start", "вкл", "включи"):
            note = await asyncio.to_thread(amb_music.start, {})
        text, rows = await asyncio.to_thread(amb_music.panel_now)
    except Exception:
        logger.exception("[AMB] /amb упал")
        await update.message.reply_text("Сбой пульта подсветки, подробности в журнале.")
        return
    await update.message.reply_text((note + "\n\n" if note else "") + text, reply_markup=_amb_keyboard(rows))


async def _handle_amb_choice(query, data: str) -> None:
    """Кнопка пульта: ответ на нажатие сразу (ожидание старта моста до 8 с), потом правка сообщения."""
    await _answer_callback_safely(query)
    try:
        note = await asyncio.to_thread(amb_music.on_button, data)
        text, rows = await asyncio.to_thread(amb_music.panel_now)
    except Exception:
        logger.exception("[AMB] кнопка %s", data)
        return
    await _edit_message_safely(query, (note + "\n\n" if note else "") + text, reply_markup=_amb_keyboard(rows))


async def handle_tv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /tv — управление телевизором Philips (JointSpace API v6).

    Префикс обязателен, и это решение, а не удобство: разбор команд
    телевизора НЕ подключён к общему потоку сообщений. Иначе любой вопрос не
    про телевизор получал бы отказ разбора вместо ответа Ока, неадмин получал
    бы отказ доступа на любую свою реплику, а висящее ожидание подтверждения
    перехватывало бы следующую фразу Оператора, чем бы она ни была.

    Проверка прав — молчаливым возвратом, как в /enddiscuss и /newepoch:
    посторонний не должен узнавать о существовании команды. Внутри
    tv_bot_handler права проверяются повторно — на случай вызова не отсюда.

    asyncio.to_thread обязателен. tv_bot_handler.handle делает синхронные
    HTTPS-запросы с таймаутом 5 с; в событийном цикле это заблокировало бы
    ВСЕХ остальных пользователей на время ответа телевизора.
    """
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        return

    payload = " ".join(context.args) if context.args else ""
    if _tv_exit_word(tv_nav.canonical(payload)):
        _tv_mode.pop(user_id, None)
        await update.message.reply_text("📺 Режим телевизора выключен.")
        return
    if not payload:
        _tv_mode[user_id] = time.time() + TV_MODE_S
        await update.message.reply_text(TV_MODE_ON)
        return
    try:
        answer = await asyncio.to_thread(
            tv_bot_handler.handle,
            user_id,
            payload,
            tv_bot_handler.make_factory(),
        )
    except Exception:
        # Контур телевизора не должен ронять бота. Текст пользователю без
        # подробностей, подробности — в журнал.
        logger.exception("[TV] обработчик /tv упал")
        answer = "Сбой контура управления телевизором, подробности в журнале."

    await update.message.reply_text(answer)


async def _broadcast_to_admins(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup=None,
) -> None:
    """Рассылает сообщение всем ID из config.ADMIN_USER_IDS. Использует
    context.bot — атрибут Application, не модуль bot.py (имя совпадает
    с именем этого файла, что создавало путаницу в черновике API).
    Отправка best-effort: сбой доставки одному админу не должен прервать
    рассылку остальным.

    reply_markup опционален — общая функция используется и для текстовых
    алертов о деградации (без клавиатуры), и для staging-уведомлений
    (с InlineKeyboardMarkup Approve/Reject/категория). Раньше это были бы
    две похожие функции рассылки с одинаковой логикой обхода
    ADMIN_USER_IDS — извлечено в одну, специализированные обёртки
    (_send_admin_alert) сохранены для мест, которым не нужна клавиатура.
    """
    if not config.ADMIN_USER_IDS:
        logger.warning("Broadcast triggered but ADMIN_USER_IDS is empty — no Telegram recipients.")
        return
    for admin_id in config.ADMIN_USER_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Failed to deliver message to admin_id={admin_id}: {e}")


async def _send_admin_alert(context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    """Обёртка над _broadcast_to_admins специально для алертов деградации —
    сохраняет префикс ⚠️ [ALERT], не подходящий для других типов сообщений
    (например, staging-уведомлений, у которых другой тон и есть кнопки).
    """
    await _broadcast_to_admins(context, f"⚠️ [ALERT] {message}")


async def alert_checker_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодическая проверка деградации через JobQueue — pull-модель.
    Governor и input_monitor не знают о Telegram и не инициируют отправку
    сами; bot.py опрашивает их метрики и решает, нужен ли алерт. Это
    сохраняет AgentGovernor чистым от I/O-зависимостей.

    Раздельные пороги для входа (input_monitor — качество фильтрации
    запросов) и выхода (governor.monitor — качество ответов модели):
    рост одного без другого диагностически означает разное (см. docstring
    process_logic). last_alert_state_* в Registry защищает от спама —
    алерт шлётся только на переходе NORMAL -> ALARM, не на каждый цикл
    опроса, пока степень деградации остаётся стабильно высокой.
    """
    governor: AgentGovernor = context.application.bot_data["governor"]
    input_monitor: ComplianceMonitor = context.application.bot_data["input_monitor"]
    registry = governor.registry

    await _check_and_alert(
        context,
        monitor=governor.monitor,
        state_key="last_alert_state_compliance",
        registry=registry,
        label="Compliance (качество ответов модели)",
    )
    await _check_and_alert(
        context,
        monitor=input_monitor,
        state_key="last_alert_state_input",
        registry=registry,
        label="Input filtering (подозрение на атаку/спам)",
    )


async def _check_and_alert(
    context: ContextTypes.DEFAULT_TYPE,
    monitor: ComplianceMonitor,
    state_key: str,
    registry: Registry,
    label: str,
) -> None:
    """Общая логика для одного монитора. Вынесена отдельно, чтобы не
    дублировать проверку sample_size/threshold/state-transition дважды
    (для compliance и для input) с риском разойтись в деталях.
    """
    sample_size = monitor.get_sample_size()
    if sample_size < config.ALERT_MIN_SAMPLE_SIZE:
        # Недостаточно данных, чтобы доверять error_rate — НЕ трогаем
        # текущее last_alert_state. get_error_rate() при пустом/почти
        # пустом окне вернёт 0.0 (см. monitor.py), что выглядело бы как
        # "всё хорошо", хотя на самом деле это просто "пока нет данных",
        # не подтверждённое здоровье. Молчим, не сбрасываем и не алертим.
        return

    error_rate = monitor.get_error_rate()
    previous_state = registry.get_state(state_key, "NORMAL")

    if error_rate > config.ALERT_ERROR_THRESHOLD:
        # WARNING, не CRITICAL: эта строка повторяется каждый цикл опроса,
        # пока тревога держится, а process_watchdog считает любые строки
        # ERROR/CRITICAL в логе и при превышении лимита шлёт SIGTERM
        # процессу. Деградация КАЧЕСТВА ответов (или всплеск блокировок
        # входа) — не отказ инфраструктуры, перезапуск её не лечит и
        # обнулил бы окно монитора. Настоящие обрывы связи с Ollama по-
        # прежнему пишутся на уровне ERROR в governor._route_to_llm.
        logger.warning(
            "%s: error_rate=%.1f%% exceeds threshold=%.1f%% (sample_size=%d)",
            label, error_rate * 100, config.ALERT_ERROR_THRESHOLD * 100, sample_size,
        )
        if previous_state == "NORMAL":
            registry.update_state(state_key, "ALARM")
            await _send_admin_alert(
                context,
                f"{label}: error_rate {error_rate:.1%} превышает порог "
                f"{config.ALERT_ERROR_THRESHOLD:.1%} (окно={sample_size} событий)",
            )
        # Если previous_state уже ALARM — не повторяем отправку, это и есть
        # защита от спама при стабильно высокой деградации.
    else:
        if previous_state == "ALARM":
            logger.info("%s: error_rate вернулся в норму (%.1f%%)", label, error_rate * 100)
            registry.update_state(state_key, "NORMAL")
            await _send_admin_alert(context, f"{label}: восстановлено, error_rate={error_rate:.1%}")


async def handle_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Детерминированный ответ на «что ты обо мне хранишь» - из memory.db, без LLM.
    Аудит 06.10: модель на этот вопрос уверенно отвечала «личные данные не
    хранятся», хотя журнал хранит всю переписку."""
    if update.message is None or update.effective_user is None:
        return
    # ADMIN_GATE (25.10): fail-closed, как /discuss и обычный чат. Без проверки
    # посторонний получал сводку и resolve() создавал ему subject в memory.db.
    if update.effective_user.id not in config.ADMIN_USER_IDS:
        logger.warning("Unauthorized /memory attempt from user_id=%s", update.effective_user.id)
        return
    from datetime import datetime as _dt
    core = _get_memory()
    inv = core.inventory(core.resolve("telegram", str(update.effective_user.id)))
    f = lambda t: _dt.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"
    ttl = globals().get("MEMORY_TTL_DAYS")
    lines = [
        "ПАМЯТЬ (прямо из memory.db, без участия модели)",
        f"записей: {inv['events']} - ваши вопросы и ответы Око, после маскирования ключей и паролей",
        f"период: {f(inv['first_ts'])} - {f(inv['last_ts'])}",
        f"каналы: {', '.join(ch for ch, _ in inv['aliases']) or '-'}",
        f"производных ярлыков и выводов о вас: {inv['derived_labels']}",
        f"срок хранения: {f'{ttl:g} дн.' if ttl else 'не ограничен'}",
        "также: снапшоты базы (копии журнала), логи службы (без текста сообщений)",
        "удаление: /forget" if "handle_forget" in globals() else "удаление: команда /forget ещё не подключена",
    ]
    await update.message.reply_text("\n".join(lines))


def _retry_in_text(governor) -> str:
    try:
        return f"{float(governor.transport_retry_in()):.0f}"
    except Exception:
        return "-"


def _is_tg_transport_error(err) -> bool:
    """TG_TRANSPORT (M5b): сетевой сбой связи с Telegram (NetworkError/TimedOut,
    напр. getaddrinfo failed). BadRequest в PTB -- тоже подкласс NetworkError,
    но это содержательный отказ API (Query is too old, Message is not
    modified) -- НЕ транспорт. InvalidToken/Forbidden -- не NetworkError и
    остаются красными (решение Оператора 4)."""
    from telegram.error import NetworkError
    return isinstance(err, NetworkError) and not isinstance(err, BadRequest)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Без обработчика исключение в команде терялось молча (аудит 06.10: /status
    падал на каждом вызове, Оператор не получал ответа вообще)."""
    if _is_tg_transport_error(context.error):
        # Жёлтый: связь с Telegram пропала/таймаут; ответить всё равно нечем.
        logger.warning("[TG_TRANSPORT] %s: %s", type(context.error).__name__, context.error)
        return
    logger.error("Unhandled exception in handler: %r", context.error, exc_info=context.error)
    msg = update.effective_message if isinstance(update, Update) else None
    if msg is not None:
        try:
            await msg.reply_text("[ERR: INTERNAL] Внутренняя ошибка обработчика, подробности в логе службы.")
        except Exception:
            pass


TELEMETRY_TAIL_BYTES = 256 * 1024


def _telemetry_status_text() -> str:
    """HUD v2 для /status: последние [CTX] main / [DEPTH] / [EPOCH] / P1 / [VECTOR_S] из хвоста
    лога — тот же разбор, что у HUD (hud_state). Только чтение, текста сообщений нет."""
    if config.OKO_LOG_FILE is None:
        return "лог не задан (OKO_LOG_FILE) -> телеметрии нет"
    try:
        import hud_state
        w = hud_state.LogWatcher(config.OKO_LOG_FILE, start_tail=TELEMETRY_TAIL_BYTES)
        if not w.poll():
            return "лог не найден"
        snap = hud_state.telemetry_snapshot(w, time.time())
        return "\n".join(text for text, _ in hud_state.telemetry_rows(snap))
    except Exception as e:
        return f"ошибка чтения лога ({type(e).__name__})"


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ-команда /status — снимок здоровья системы без чтения логов.
    Закрыта по умолчанию: без ADMIN_USER_IDS в .env команда не отвечает
    никому, а не отвечает всем — намеренный fail-closed выбор, т.к. в
    отчёте есть last_error и счётчики, не предназначенные для рядовых
    пользователей.
    """
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        logger.warning("Unauthorized /status attempt from user_id=%s", user_id)
        return  # не подтверждаем и не отрицаем существование команды

    governor: AgentGovernor = context.application.bot_data["governor"]
    input_monitor: ComplianceMonitor = context.application.bot_data["input_monitor"]
    state = governor.registry.get_status()["state"]

    discuss_line = (
        f"active_discuss: {discussion_sessions[user_id]}"
        if user_id in discussion_sessions
        else "active_discuss: none"
    )

    sleep_text = await asyncio.to_thread(_sleep_status_text)
    telemetry_text = await asyncio.to_thread(_telemetry_status_text)

    report = (
        "KERNEL STATUS\n"
        f"status: {state['status']}\n"
        f"current_task: {state['current_task']}\n"
        f"last_error: {state['last_error']}\n"
        f"{discuss_line}\n"
        "\n"
        "COMPLIANCE (cumulative, since boot)\n"
        f"total_requests: {state['total_requests']}\n"
        f"compliance_violations: {state['compliance_violations']}\n"
        f"compliance_check_failures: {state['compliance_check_failures']}\n"
        f"last_compliance_status: {state['last_compliance_status']}\n"
        f"input_blocked_count: {state['input_blocked_count']}\n"
        "\n"
        f"OUTPUT QUALITY (sliding window, last {governor.monitor.window_size} checked)\n"
        f"sample_size: {governor.monitor.get_sample_size()}\n"
        f"error_rate: {governor.monitor.get_error_rate():.2%}\n"
        f"unchecked_rate: {governor.monitor.get_unchecked_rate():.2%}\n"
        f"alert_state: {state['last_alert_state_compliance']}\n"
        "\n"
        "CIRCUIT BREAKER (CLOSED=норма, OPEN=отказ, HALF_OPEN=проба восстановления)\n"
        f"state: {getattr(governor, 'breaker_state', 'не подключён к governor')}\n"
        "\n"
        "TRANSPORT GATE (связь с Ollama; OPEN=запросы не отправляются)\n"
        f"state: {getattr(governor, 'transport_gate_state', 'не подключён к governor')}\n"
        f"retry_in_s: {_retry_in_text(governor)}\n"
        "\n"
        "SLEEP (M7: сводка лога + дрейф; только наблюдение, порогов нет)\n"
        f"{sleep_text}\n"
        "\n"
        f"TELEMETRY (хвост лога {TELEMETRY_TAIL_BYTES // 1024} КБ; рубеж этап 1 — наблюдение)\n"
        f"{telemetry_text}\n"
        "\n"
        "DEGRADATION MARKERS\n"
        f"transport_failures: {state.get('transport_failures', 0)}\n"
        f"stagnation_breaks: {state.get('stagnation_breaks', 0)}\n"
        f"last_stagnation_reason: {state.get('last_stagnation_reason')}\n"
        "\n"
        f"INPUT FILTERING (sliding window, last {input_monitor.window_size} checked)\n"
        f"sample_size: {input_monitor.get_sample_size()}\n"
        f"error_rate: {input_monitor.get_error_rate():.2%}\n"
        f"alert_state: {state['last_alert_state_input']}\n"
    )
    await update.message.reply_text(report)


# Состояние модуля, не bot_data — это чисто внутренняя дедупликация
# уведомлений о pending_tasks, не разделяемое состояние между компонентами
# системы (см. историю решений). При рестарте процесса очищается, что
# намеренно: сброс при рестарте — приемлемый UX (см. историю решений),
# не баг, который нужно переживать через персистентность.
_notified_uuids: set[str] = set()


async def staging_monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодический опрос StagingRepository для HITL-уведомлений.

    ИЗМЕНЕНО (см. историю решений — миграция pending_tasks в SQL):
    раньше это был Registry.pending_tasks, JSON-файл с read-merge-write
    гонкой между процессами (bot.py / run_system.py), дававшей
    воспроизведённый lost update при конкурентной записи. StagingRepository
    делает то же самое через SQLite-транзакции — get_all_tasks() всегда
    читает актуальное состояние с диска через db.get_connection(), явный
    refresh_state()-подобный шаг больше не нужен: SQL уже единый источник
    правды между процессами, не требующий ручного перечитывания.

    Дедупликация "уже уведомили об этом uuid" по-прежнему живёт в
    _notified_uuids, не в самой репозитории — то же разделение
    ответственности, что было с Registry.

    Клавиатура для новой задачи включает и категорию, и Approve/Reject
    в ОДНОМ сообщении (см. историю решений — Развилка 1, post-hoc выбор
    категории вместо отдельного шага до/после Approve).
    """
    staging_manager: StagingManager = context.application.bot_data["staging_manager"]
    pending = staging_manager.repo.get_all_tasks()

    current_uuids = set(pending.keys())

    # Новые задачи, о которых ещё не уведомляли
    new_uuids = current_uuids - _notified_uuids
    for task_uuid in new_uuids:
        task = pending[task_uuid]
        filename = Path(task["original_path"]).name if "original_path" in task else task_uuid
        keyboard = _category_keyboard(task_uuid)
        await _broadcast_to_admins(
            context,
            f"📄 Новый файл ждёт подтверждения:\n{filename}\nUUID: {task_uuid}\n"
            f"Категория: не выбрана",
            reply_markup=keyboard,
        )
        _notified_uuids.add(task_uuid)

    # Задачи, которые исчезли из pending_tasks (approve/reject произошёл
    # где-то ещё, например через /approve как текстовую команду, не только
    # через callback-кнопку) — убираем из кэша, иначе он растёт бесконечно.
    stale_uuids = _notified_uuids - current_uuids
    _notified_uuids.difference_update(stale_uuids)


def _index_approved_file_in_thread(file_path: Path, override_category: str | None) -> str:
    """Выполняется внутри asyncio.to_thread из handle_staging_callback.

    Собственный sqlite3.Connection обязателен — connection-объекты SQLite
    не переносимы между потоками (тот же паттерн, что уже используется в
    _fetch_history/_save_exchange/_fetch_document_content в этом файле).
    conn.close() в finally гарантирует отсутствие утечки соединения даже
    при исключении внутри process_single.
    """
    conn = db.get_connection()
    try:
        return knowledge_indexer.process_single(
            file_path, conn, override_category=override_category
        )
    finally:
        conn.close()


async def _answer_callback_safely(query, *args, **kwargs) -> None:
    """CALLBACK_ANSWER_EARLY: Telegram принимает answer() только ~15 с после
    нажатия. Просроченный запрос (бот был занят или перезапускался) не должен
    отменять approve/reject: только WARNING в лог.
    CALLBACK_EDGE_GUARD: аргументы (текст, show_alert) пробрасываются как есть."""
    try:
        await query.answer(*args, **kwargs)
    except BadRequest as e:
        logger.warning("Callback answer failed (query expired?): %s", e)


async def _edit_message_safely(query, text: str, **kwargs) -> None:
    """CALLBACK_EDGE_GUARD: повторный клик по той же кнопке даёт тот же текст ->
    BadRequest "Message is not modified" -> раньше необработанное исключение,
    [ERROR] в логе и [ERR: INTERNAL] оператору. Сообщение уже в нужном
    состоянии: DEBUG. Прочие BadRequest (сообщение удалено/слишком старое) --
    WARNING: действие (approve/reject/запись) уже выполнено, падать нельзя."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            logger.debug("Callback message unchanged (repeat click): %s", e)
        else:
            logger.warning("Callback message edit failed: %s", e)


class _IndexingFailed(Exception):
    """M3_INGEST_GUARD: сбой ИНДЕКСАЦИИ после успешного approve. Отдельный тип,
    чтобы ValueError из индексации (UnicodeDecodeError, UnreadableDocumentError
    -- оба подклассы ValueError) не попадал в ветку "SECURITY: hash mismatch",
    а KeyError -- в ветку "task not found"."""

    def __init__(self, path: Path, cause: BaseException) -> None:
        super().__init__(f"{path.name}: {cause}")
        self.path = path
        self.cause = cause


# Статусы process_single, при которых документ НЕ проиндексирован, а
# перемещён в archived/rejected (см. knowledge_indexer._reject_document).
_REJECTED_STATUSES = frozenset({"empty", "unreadable"})


async def handle_staging_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на inline-кнопках из staging_monitor_job:
    выбор категории (post-hoc, см. историю решений — Развилка 1),
    Approve и Reject. callback_data форматов "cat_<key>_<uuid>",
    "approve_<uuid>", "reject_<uuid>" — все три формата зафиксированы
    здесь и в staging_monitor_job/_category_keyboard, должны оставаться
    синхронными при изменении.
    """
    query = update.callback_query
    if query is None or query.from_user is None:
        return

    user_id = query.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        logger.warning("Unauthorized staging callback attempt from user_id=%s", user_id)
        await _answer_callback_safely(query, "Not authorized.", show_alert=True)
        return

    data = query.data or ""
    governor: AgentGovernor = context.application.bot_data["governor"]
    if data.startswith("doc_"):        # 28.10: выбор судьбы документа (doc_intake)
        await _handle_doc_choice(query, data)
        return
    if data.startswith("amb:"):        # AMB_MUSIC (04.11): ползунки подсветки под музыку
        await _handle_amb_choice(query, data)
        return
    staging_manager: StagingManager = context.application.bot_data["staging_manager"]

    # --- Выбор категории (ИЗМЕНЕНО — миграция pending_tasks в SQL): не
    # читаем и не переписываем весь словарь задач, точечный UPDATE по
    # uuid через StagingRepository.update_category — тот же lost-update
    # риск, что раньше существовал в Registry.update_state("pending_tasks",
    # весь_словарь), для этого поля больше структурно невозможен: SQL
    # UPDATE ... WHERE uuid = ? не может задеть чужую запись. ---
    for cat_key, cat_label in CATEGORY_LABELS.items():
        prefix = f"cat_{cat_key}_"
        if not data.startswith(prefix):
            continue
        task_uuid = data[len(prefix):]

        task = staging_manager.repo.get_task(task_uuid)
        if task is None:
            await _answer_callback_safely(
                query, "Задача не найдена (уже обработана?).", show_alert=True
            )
            return

        staging_manager.repo.update_category(task_uuid, cat_label)

        # CALLBACK_EDGE_GUARD: ответ на нажатие ДО правки сообщения -- если
        # правка упадёт, кнопка у оператора не останется "крутиться".
        await _answer_callback_safely(query, f"Категория: {cat_label}")
        filename = Path(task["original_path"]).name if "original_path" in task else task_uuid
        await _edit_message_safely(
            query,
            f"📄 Новый файл ждёт подтверждения:\n{filename}\nUUID: {task_uuid}\n"
            f"Категория: {cat_label}",
            reply_markup=_category_keyboard(task_uuid),
        )
        return

    # --- Game watcher candidate approval (games.txt) ---
    # callback_data format: "game_add_<process_name>" / "game_ignore_<process_name>".
    # Added here, not as a separate CallbackQueryHandler, because the
    # existing handler is registered without a pattern filter in main()
    # (app.add_handler(CallbackQueryHandler(handle_staging_callback))) and
    # would intercept every callback query before a second handler could
    # see it. games.txt is read by game_watcher.ps1 (PowerShell, separate
    # NSSM service OkoGameWatcher) every poll cycle - no IPC needed beyond
    # the shared file, same pattern as .env being read by both bot.py and
    # game_watcher.ps1 already.
    if data.startswith("game_add_") or data.startswith("game_ignore_"):
        is_add = data.startswith("game_add_")
        prefix_len = len("game_add_") if is_add else len("game_ignore_")
        process_name = data[prefix_len:]

        # CALLBACK_EDGE_GUARD: ответ ДО записи/правки (было: после -- при сбое
        # edit_message_text answer() не вызывался вовсе).
        await _answer_callback_safely(query)

        if is_add:
            games_file = Path(config.BASE_DIR).parent / "games.txt"
            try:
                existing_lines = []
                if games_file.exists():
                    existing_lines = games_file.read_text(encoding="ascii").splitlines()
                already_present = any(
                    line.strip() == process_name for line in existing_lines
                    if line.strip() and not line.strip().startswith("#")
                )
                if already_present:
                    await _edit_message_safely(query, f"Already in games.txt: {process_name}")
                else:
                    with open(games_file, "a", encoding="ascii") as f:
                        f.write(f"{process_name}\n")
                    await _edit_message_safely(query, f"Added to games.txt: {process_name}")
            except Exception as e:
                logger.error("Failed to add %s to games.txt: %s", process_name, e)
                await _edit_message_safely(query, f"Error adding {process_name}: {e}")
        else:
            await _edit_message_safely(query, f"Ignored: {process_name}")

        return

    # --- Approve / Reject ---
    if data.startswith("approve_"):
        action, task_uuid = "approve", data[len("approve_"):]
    elif data.startswith("reject_"):
        action, task_uuid = "reject", data[len("reject_"):]
    else:
        await _answer_callback_safely(query, "Unknown action.")
        return

    # Ответ на нажатие ДО индексации (эмбеддинги/OCR идут десятки секунд).
    # Раньше answer() стоял в finally: после успешного approve -> BadRequest
    # "Query is too old" -> [ERR: INTERNAL] оператору.
    await _answer_callback_safely(query)

    try:
        if action == "approve":
            # Категория читается ДО staging_manager.approve() — тот удаляет
            # запись из pending_tasks; approve() ничего не знает про
            # категории и не обязан (см. историю решений, Развилка 1).
            task = staging_manager.repo.get_task(task_uuid)
            override_category = task.get("category") if task else None

            # ИСПРАВЛЕНО (см. историю решений — финальная сверка блока №2):
            # approve() делает staging_path.read_bytes() + hashlib.sha256()
            # синхронно — файловый I/O и CPU-bound хэширование прямо в
            # event loop потоке. Для типичных небольших файлов это
            # миллисекунды, но для крупных staged-файлов или файловой
            # системы под сетевой нагрузкой — потенциальный микро-фриз
            # event loop. Оборачиваем ради полноты асинхронности всего
            # handle_staging_callback, тем же принципом, что и
            # process_single ниже — обработчик Telegram-колбэков не должен
            # содержать НИ ОДНОГО блокирующего вызова, независимо от того,
            # насколько он "быстрый" в типичном случае.
            approved_path = await asyncio.to_thread(staging_manager.approve, task_uuid)

            # Раньше вызов был синхронным прямо в этом async-обработчике —
            # OCR/embedding/HTTP-запросы к Ollama внутри process_single
            # (до минуты на PDF с несколькими чанками) полностью подвешивали
            # Telegram-бота для ВСЕХ пользователей на время индексации одного
            # файла. asyncio.to_thread переносит блокирующую работу в отдельный
            # поток; собственный db.get_connection() внутри потока обязателен —
            # sqlite3.Connection не переносим между потоками (тот же паттерн,
            # что уже используется в _fetch_history/_save_exchange/
            # _fetch_document_content в этом же файле).
            # M3_INGEST_GUARD: исключение индексации заворачивается в
            # _IndexingFailed -- ветки KeyError/ValueError ниже относятся
            # ТОЛЬКО к approve() (нет задачи / hash mismatch).
            try:
                status = await asyncio.to_thread(
                    _index_approved_file_in_thread, approved_path, override_category
                )
            except Exception as e:
                raise _IndexingFailed(approved_path, e) from e

            if status in _REJECTED_STATUSES:
                await _edit_message_safely(
                    query,
                    f"⛔ Not indexed: {approved_path.name} ({status}). "
                    f"Файл перемещён в archived/rejected, база знаний не изменена.",
                )
            else:
                cat_note = f", категория: {override_category}" if override_category else ""
                await _edit_message_safely(
                    query,
                    f"✅ Approved and indexed: {approved_path.name} ({status}{cat_note})",
                )
        else:
            # reject() делает staging_path.unlink() синхронно — тот же
            # принцип, что и для approve() выше: обёрнуто ради полноты,
            # не потому что unlink() сам по себе тяжёлая операция.
            await asyncio.to_thread(staging_manager.reject, task_uuid)
            await _edit_message_safely(query, f"❌ Rejected: uuid={task_uuid}")
    except _IndexingFailed as e:
        logger.error(
            "Indexing failed after approve for uuid=%s (%s): %r",
            task_uuid, e.path.name, e.cause,
        )
        await _edit_message_safely(
            query,
            f"⚠️ Indexing failed: {e.path.name}: {e.cause}\n"
            f"Файл остался в staging, задача снята.",
        )
    except KeyError:
        await _edit_message_safely(query, f"⚠️ Task {task_uuid} not found (already processed?)")
    except ValueError as e:
        # Hash mismatch — файл подменён во время ожидания (Вариант Б).
        await _edit_message_safely(query, f"🚨 SECURITY: {e}")
    except Exception as e:
        logger.error(f"Staging callback failed for uuid={task_uuid}: {e}")
        await _edit_message_safely(query, f"⚠️ Error processing {task_uuid}: {e}")
    finally:
        _notified_uuids.discard(task_uuid)


# PRINT_FOLLOWUP (28.10): последний документ админа -> (объект Document, время). Только память процесса:
# после перезапуска бота «печать» без свежего файла честно отвечает «нет документа».
PRINT_FOLLOWUP_S = 600
_last_document: dict[int, tuple] = {}


async def _download_to_incoming(update: Update, doc):
    """Файл из Telegram -> data/incoming/<id>__<имя> (не наблюдаемая папка). -> (id, путь) или None."""
    name = Path(doc.file_name or f"upload_{doc.file_unique_id}.pdf").name
    doc_id, dest = doc_intake.new_path(name)
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=str(dest))
    except Exception as e:
        logger.error("[DOC] download failed %s: %s", name, e)
        await update.message.reply_text(f"⚠️ Не удалось скачать файл: {e}")
        return None
    logger.info("[DOC] получен через Telegram: %s -> %s", name, dest)
    return doc_id, dest


_doc_busy: set = set()


async def _run_print(path: Path, pages, keep: bool):
    """Печать + судьба файла. -> (отчёт, ok). Неудачная печать файл НЕ трогает: можно повторить или удалить."""
    name = doc_intake.display_name(path)
    try:
        res = await asyncio.to_thread(doc_print.print_file, path, pages=pages, workdir=doc_intake.pages_dir(path))
    except Exception as e:  # сбой рендера/печати не должен ронять обработчик
        logger.error("[PRINT] %s: %s: %s", name, type(e).__name__, e)
        return f"⚠️ Печать {name} сорвалась: {type(e).__name__}: {e}\nФайл сохранён — выбери действие ещё раз.", False
    logger.info("[PRINT] %s ok=%s keep=%s | %s", name, res.ok, keep, res.text.replace("\n", " | "))
    if not res.ok:
        return res.text + "\nФайл сохранён — можно повторить печать или удалить.", False
    if keep:
        dest = await asyncio.to_thread(doc_intake.to_knowledge, path)
        return res.text + f"\n📚 Файл отправлен в базу: {dest.name} — запрос категории придёт следом.", True
    await asyncio.to_thread(doc_intake.remove, path)
    return res.text + "\n🗑 Файл удалён (рабочий процесс, в базу не попал).", True


def _doc_keyboard(doc_id: str, dup) -> InlineKeyboardMarkup:
    acts = ["print", "drop"] if dup else ["print", "printkeep", "keep", "drop"]
    btns = [InlineKeyboardButton(doc_intake.ACTIONS[a], callback_data=f"doc_{a}_{doc_id}") for a in acts]
    return InlineKeyboardMarkup([btns[i:i + 2] for i in range(0, len(btns), 2)])


async def _print_uploaded(update: Update, doc, pages) -> None:
    """Подпись «печать» или «печать» следом: печать сразу, затем удаление (рабочий процесс)."""
    got = await _download_to_incoming(update, doc)
    if got is None:
        return
    doc_id, path = got
    await update.message.reply_text(f"🖨 Принял {doc_intake.display_name(path)}, печатаю…")
    text, ok = await _run_print(path, pages, keep=False)
    await update.message.reply_text(text, reply_markup=None if ok else _doc_keyboard(doc_id, None))


async def _offer_document(update: Update, doc) -> None:
    """Документ без команды: в incoming и кнопки выбора. Дубликат (тот же текст уже в базе) — без «в базу»."""
    await asyncio.to_thread(doc_intake.purge_old)
    got = await _download_to_incoming(update, doc)
    if got is None:
        return
    doc_id, path = got
    dup = await asyncio.to_thread(doc_intake.known_in_knowledge, path)
    note = "\n⚠️ Такой текст уже есть в базе знаний — сохранять повторно незачем." if dup else ""
    logger.info("[DOC] %s ждёт выбора (id=%s, дубликат=%s)", doc_intake.display_name(path), doc_id, dup)
    await update.message.reply_text(f"📄 {doc_intake.display_name(path)}\nЧто сделать?{note}",
                                    reply_markup=_doc_keyboard(doc_id, dup))


async def _handle_doc_choice(query, data: str) -> None:
    """Кнопки doc_<действие>_<id>. Состояние — файл в incoming: после перезапуска бота кнопки работают."""
    action, _, doc_id = data[len("doc_"):].rpartition("_")
    path = doc_intake.find(doc_id)
    if action not in doc_intake.ACTIONS or path is None:
        await _answer_callback_safely(query, "Файл уже обработан или удалён.", show_alert=True)
        await _edit_message_safely(query, "ℹ️ Этот файл уже обработан или удалён.")
        return
    if doc_id in _doc_busy:
        await _answer_callback_safely(query, "Уже выполняется…")
        return
    _doc_busy.add(doc_id)
    try:
        name = doc_intake.display_name(path)
        await _answer_callback_safely(query, doc_intake.ACTIONS[action])
        logger.info("[DOC] %s: выбор %s", name, action)
        if action == "drop":
            await asyncio.to_thread(doc_intake.remove, path)
            await _edit_message_safely(query, f"🗑 {name}: удалён, в базу не попал.")
            return
        if action == "keep":
            dest = await asyncio.to_thread(doc_intake.to_knowledge, path)
            await _edit_message_safely(query, f"📚 {name} -> база знаний ({dest.name}). "
                                              f"Запрос категории придёт следом.")
            return
        await _edit_message_safely(query, f"🖨 {name}: печатаю…")
        text, ok = await _run_print(path, None, keep=(action == "printkeep"))
        await _edit_message_safely(query, text, reply_markup=None if ok else _doc_keyboard(doc_id, None))
    finally:
        _doc_busy.discard(doc_id)


async def handle_pdf_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приём PDF из Telegram. Файл скачивается прямо в input_dir
    (config.BASE_DIR / "knowledge") — тот же путь, что наблюдает
    run_system.py/KnowledgeHandler. bot.py сам НЕ вызывает
    StagingManager.move_to_staging() напрямую: watcher уже debounce'ит,
    бэкапит и ставит файл в staging сам при обнаружении в input_dir —
    дублировать эту логику здесь означало бы второе место, которое
    должно было бы оставаться синхронным с run_system.py. Выбор категории
    происходит позже, post-hoc, через ту же клавиатуру staging_monitor_job
    (см. историю решений — Развилка 1), когда watcher поставит задачу
    в pending_tasks и bot.py её увидит.
    """
    if not update.message or not update.message.document:
        return

    user_id = update.message.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        logger.warning("Unauthorized PDF upload attempt from user_id=%s", user_id)
        return

    doc = update.message.document
    _last_document[user_id] = (doc, time.time())
    want_print, print_pages = doc_print.wants_print(update.message.caption)
    if want_print:
        await _print_uploaded(update, doc, print_pages)
        return
    await _offer_document(update, doc)


async def handle_legacy_office(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Старые двоичные .doc/.xls: office_text их не читает. Честный отказ вместо молчания (28.10)."""
    if not update.message or not update.message.document:
        return
    if update.message.from_user.id not in config.ADMIN_USER_IDS:
        return
    name = update.message.document.file_name or "файл"
    await update.message.reply_text(
        f"⚠️ {name}: старый формат Word/Excel (.doc/.xls) не читаю. "
        f"Пересохраните как .docx/.xlsx (Файл → Сохранить как) и пришлите снова."
    )


MEMORY_TTL_DAYS = float(os.getenv("OKO_MEMORY_TTL_DAYS", "0"))  # 0 = TTL выключен; по сроку удаляет только при явном значении


async def handle_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R1: /forget показывает, что будет удалено; /forget confirm удаляет.
    Удаляется вся память ЭТОГО пользователя: журнал, следы в файлах SQLite
    (WAL, свободные страницы), снапшоты. Необратимо, поэтому два шага."""
    if update.message is None or update.effective_user is None:
        return
    user_id = update.effective_user.id
    if user_id not in config.ADMIN_USER_IDS:  # ADMIN_GATE (25.10), см. handle_memory
        logger.warning("Unauthorized /forget attempt from user_id=%s", user_id)
        return
    core = _get_memory()
    sid = core.resolve("telegram", str(user_id))
    telegram_note = ("Переписку в самом Telegram это не затрагивает: "
                     "её можно удалить только в приложении Telegram.")
    if not context.args or context.args[0].lower() != "confirm":
        n = core.inventory(sid)["events"]
        await update.message.reply_text(
            f"Будет удалено безвозвратно: {n} записей памяти, их копии в снапшотах "
            f"и следы в файлах базы.\n{telegram_note}\nПодтвердить: /forget confirm"
        )
        return
    n = await asyncio.to_thread(core.forget, sid)
    logger.warning("R1 /forget executed: user=%s events=%s", user_id, n)
    await update.message.reply_text(
        f"Удалено записей: {n}. Журнал, снапшоты и файлы базы очищены.\n{telegram_note}"
    )


def _new_epoch(user_id: int) -> tuple:
    """M7.3: граница эпохи для истории. Журнал, индекс, S и дрейф не трогаются."""
    core = _get_memory()
    sid = core.resolve("telegram", str(user_id))
    ts = core.new_epoch(sid)
    return ts, core.inventory(sid)["events"]


async def handle_newepoch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/newepoch (M7.3, R2): история диалога для модели начинается заново.
    Ничего не удаляет. RAG по эпохе не фильтруется (решение Оператора D3).
    Управляющая команда: fail-closed для не-админов, как /discuss."""
    if not update.message or not update.message.from_user:
        return
    user_id = update.message.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        logger.warning("Unauthorized /newepoch attempt from user_id=%s", user_id)
        return
    try:
        ts, n = await asyncio.to_thread(_new_epoch, user_id)
    except Exception as e:
        logger.error("[EPOCH] fail=%s %s", type(e).__name__, e)
        await update.message.reply_text("[ERR: EPOCH_FAULT] Эпоха не сменена, история прежняя.")
        return
    logger.info("[EPOCH] new ts=%.0f", ts)
    await update.message.reply_text(
        f"Новая эпоха с {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}. "
        f"История диалога для модели начинается заново.\n"
        f"Журнал не удалён ({n} записей), /memory и /forget работают как прежде. "
        f"Поиск по базе знаний (RAG) эпохой не ограничен."
    )


async def handle_force_brain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/force_brain [минуты|off] (GAME_MODE v2, 02.11): аварийный клапан -- мозг в игре на время.
    Только админ (посторонний не может просадить FPS). Флаг brain_force.flag читает
    game_watcher.ps1: пока он действует, модель из памяти не выгружается."""
    if not update.message or not update.message.from_user:
        return
    user_id = update.message.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        logger.warning("Unauthorized /force_brain attempt from user_id=%s", user_id)
        return
    arg = context.args[0].strip().lower() if context.args else ""
    now = time.time()
    if arg in ("off", "0", "стоп", "выкл"):
        removed = await asyncio.to_thread(game_mode.clear_force, config.OKO_GAME_FORCE_FLAG)
        logger.info("[GAME_MODE] force=off removed=%d", int(removed))
        await update.message.reply_text(
            "🧠 Принудительное пробуждение снято." if removed else "🧠 Принудительное пробуждение не было включено.")
        return
    if not game_mode.in_game(config, now):
        await update.message.reply_text("🧠 Игровой режим не активен — мозг и так работает.")
        return
    minutes = config.OKO_GAME_FORCE_MIN
    if arg:
        try:
            minutes = int(arg)
        except ValueError:
            await update.message.reply_text("Формат: /force_brain [минуты] или /force_brain off")
            return
    minutes = max(1, min(minutes, config.OKO_GAME_FORCE_MAX_MIN))
    until = now + minutes * 60
    try:
        await asyncio.to_thread(game_mode.write_force, config.OKO_GAME_FORCE_FLAG, until)
    except OSError as e:
        logger.warning("[GAME_MODE] force=fail %s: %s", type(e).__name__, e)
        await update.message.reply_text("⚠️ Мозг не включён: не удалось записать флаг.")
        return
    gpu = config.OKO_GAME_FORCE_NUM_GPU
    where = ("на видеокарте — FPS просядет" if gpu is None
             else "на процессоре — медленно" if gpu == 0 else "частично на видеокарте")
    logger.info("[GAME_MODE] force=on min=%d num_gpu=%s", minutes, "-" if gpu is None else gpu)
    await update.message.reply_text(
        f"🧠 Мозг включён до {time.strftime('%H:%M', time.localtime(until))} ({where}). "
        f"Досрочно: /force_brain off")


async def memory_ttl_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """R1: события старше MEMORY_TTL_DAYS удаляются с физическим стиранием."""
    if MEMORY_TTL_DAYS <= 0:
        return
    try:
        n = await asyncio.to_thread(_get_memory().apply_ttl, MEMORY_TTL_DAYS)
        if n:
            logger.info("R1 TTL: removed %d events older than %g days", n, MEMORY_TTL_DAYS)
    except Exception as e:
        logger.error("R1 TTL job failed: %s", e)


async def device_watch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Техника дома -> Telegram (каркас 28.10). Pull-модель, как alert_checker_job: device_watch не знает
    о Telegram, возвращает готовые тексты событий (переходы состояний), отсюда они рассылаются админам.
    asyncio.to_thread обязателен: внутри блокирующий HTTP к облаку SmartThings."""
    try:
        events = await asyncio.to_thread(device_watch.poll_once)
    except Exception as e:  # сбой наблюдения не должен ронять JobQueue
        logger.error("[DEVICES] poll failed: %s: %s", type(e).__name__, e)
        return
    for text in events:
        logger.info("[DEVICES] event: %s", text)
        _event("devices", text)             # EVENTS (этап C)
        await _broadcast_to_admins(context, text)


async def net_context_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Смена сети ПК -> проверка ТВ/SmartThings/Garmin/принтера и одна сводка (29.10, вариант A).
    asyncio.to_thread обязателен: внутри route/arp, поиск ТВ, HTTP к облакам."""
    try:
        texts = await asyncio.to_thread(net_context.poll_once)
    except Exception as e:  # сбой проверки сети не должен ронять JobQueue
        logger.error("[NET] poll failed: %s: %s", type(e).__name__, e)
        return
    for text in texts:
        logger.info("[NET] summary: %s", text.splitlines()[0])
        _event("net", text)                 # EVENTS (этап C)
        await _broadcast_to_admins(context, text)


async def garmin_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сводка Garmin по расписанию (08:00 утро, 21:00 вечер). Текст строит garmin_report, сеть — в потоке."""
    kind = context.job.data if context.job else "morning"
    try:
        text = await asyncio.to_thread(garmin_report.run, kind)
    except Exception as e:  # сбой сводки не должен ронять JobQueue
        logger.error("[GARMIN] сводка %s: %s: %s", kind, type(e).__name__, e)
        text = f"⚠️ Garmin: сводка не собрана ({type(e).__name__})."
    logger.info("[GARMIN] сводка %s отправлена, символов %d", kind, len(text))
    _event("garmin", text)                  # EVENTS (этап C)
    await _broadcast_to_admins(context, text)


async def handle_garmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/garmin — сводка сейчас: до 15:00 утренняя, после — вечерняя. Только админ, молча для остальных."""
    if not update.message or not update.message.from_user:
        return
    if update.message.from_user.id not in config.ADMIN_USER_IDS:
        return
    import datetime as _dt
    kind = "morning" if _dt.datetime.now(garmin_report.tz()).hour < 15 else "evening"
    await update.message.reply_text("⌚ Собираю данные Garmin…")
    try:
        text = await asyncio.to_thread(garmin_report.run, kind)
    except Exception as e:
        logger.error("[GARMIN] /garmin: %s: %s", type(e).__name__, e)
        text = f"⚠️ Garmin: сводка не собрана ({type(e).__name__})."
    await update.message.reply_text(text)


def _wol_again() -> None:
    """CAM_VERIFY (03.11 17:24): по кадру экран не засветился после «включи телевизор» -- пакет пробуждения ещё раз."""
    from tv_control import TVClient
    TVClient.from_env().wake()


def _calib_auto():
    """CALIB_AUTO (04.11): /cam calib auto -- бот гасит экран через меню ТВ, калибрует, возвращает экран."""
    from tv_control import TVClient
    return camera_watcher.calibrate_auto(TVClient.from_env())


def _cam_before_power_on() -> str:
    """SAFE-06 (04.11, идея Оператора): кадр ДО пробуждения -- ТВ ещё выключен, экран тёмный; snapshot() сам
    пересчитает углы, если камеру сдвинули. Строка -- только при пересчёте."""
    return camera_watcher.snapshot().note


async def handle_cam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cam (03.11) — кадр экрана ТВ с камеры: вырезка экрана + признаки (вкл/выкл, цвет, изменение).
    /cam full — ещё и весь кадр. /cam calib — найти экран на кадре (ТВ выключен) и сохранить его углы. /cam calib auto — экран гасит бот через меню ТВ (04.11). Только админ, молча для остальных. В историю и контекст мозга не пишется."""
    if not update.message or not update.message.from_user:
        return
    if update.message.from_user.id not in config.ADMIN_USER_IDS:
        return
    if not camera_watcher.enabled():
        await update.message.reply_text("📷 Камера не настроена: нет OKO_CAM_HOST / OKO_CAM_USER в .env.")
        return
    if context.args and context.args[0].lower() == "calib":     # 03.11: углы экрана по кадру (ТВ выключен)
        try:
            if len(context.args) > 1 and context.args[1].lower() == "auto":   # CALIB_AUTO (04.11): экран гасит бот
                await update.message.reply_text("⏳ Гашу экран через меню ТВ и калибрую камеру (~1.5 мин). "
                                                "Пульт не трогай.")
                text, path = await asyncio.to_thread(_calib_auto)
            else:
                text, path = await asyncio.to_thread(camera_watcher.calibrate)
        except Exception as e:
            logger.warning("[CAM] /cam calib: %s", type(e).__name__)
            await update.message.reply_text(f"📷 Калибровка не прошла: {e}")
            return
        if path is None:
            await update.message.reply_text(text)
        else:
            with open(path, "rb") as f:
                await update.message.reply_photo(f, caption=text)
        return
    try:
        shot = await asyncio.to_thread(camera_watcher.snapshot)
    except Exception as e:
        logger.warning("[CAM] /cam: %s", type(e).__name__)
        await update.message.reply_text(f"📷 Кадр не снят: {e}")
        return
    with open(shot.crop_path, "rb") as f:
        await update.message.reply_photo(f, caption=shot.text())
    if context.args and context.args[0].lower() == "full":
        with open(shot.frame_path, "rb") as f:
            await update.message.reply_photo(f, caption="📷 Весь кадр (два объектива).")


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обновляет mtime heartbeat-файла — единственное назначение этой
    функции. process_watchdog.py читает mtime этого файла и сравнивает с
    WATCHDOG_HEARTBEAT_TIMEOUT_SEC: если файл не обновлялся дольше
    таймаута, watchdog считает event loop зависшим (deadlock) и шлёт
    SIGTERM. Запись через Path.touch() — O(1) дисковая операция, не
    создаёт значимой нагрузки при интервале 30с.
    """
    heartbeat_file = config.BASE_DIR / "bot.heartbeat"
    heartbeat_file.touch()


# --- M7.1/M7.2 «сон» (SLEEP_CYCLE, 20.10) ---
# Простой = нет апдейтов от админов >= SLEEP_IDLE_S и TransportGate CLOSED;
# не чаще SLEEP_MIN_INTERVAL_S (время последней сводки берётся из ops.db,
# переживает рестарт). Без LLM и без записи в знания/память: читает лог
# (config.OKO_LOG_FILE) и пишет одну строку в ops.db. Сбой -> WARNING
# [SLEEP_FAIL] (не ERROR: наблюдение не должно краснить светофор).
# Состояние модуля, как _notified_uuids: при рестарте простой считается заново.
_sleep_state: dict = {"last_activity": time.time(), "last_attempt": None, "disabled_logged": False}


async def _touch_activity(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """group=-1: отмечает активность только админов (посторонний спам не
    должен откладывать сон). Не останавливает обработку в группе 0."""
    user = getattr(update, "effective_user", None)
    if user is not None and user.id in config.ADMIN_USER_IDS:
        _sleep_state["last_activity"] = time.time()


def _sleep_status_text() -> str:
    try:
        line = sleep_digest.status_line(config.OPS_DB_PATH)
    except Exception as e:
        line = f"SLEEP: ошибка чтения ops.db ({type(e).__name__})"
    idle_min = (time.time() - _sleep_state["last_activity"]) / 60.0
    log_src = "задан" if config.OKO_LOG_FILE else "НЕ ЗАДАН (OKO_LOG_FILE) -> сон выключен"
    return f"{line}\nidle_min: {idle_min:.0f}\nlog: {log_src}"


RAG_HYGIENE_TIMEOUT_S = 120.0
_RAG_KEYS = ("rows", "missing", "partial", "orphans", "orphan_chunks", "no_hash")


def _rag_hygiene_counts() -> Optional[dict]:
    """M7.4: dry-run скан rag_resync (только чтение SQLite + ChromaDB). None --
    oko_rag недоступен. Импорт ленивый: rag_resync нужен только во сне."""
    import rag_resync
    from contextlib import closing
    if not knowledge_indexer.RAG_SYNC_AVAILABLE:
        return None
    with closing(db.get_connection()) as conn:
        collection = knowledge_indexer.oko_rag_indexer.get_collection()
        return rag_resync.hygiene_counts(conn, collection)


async def _log_rag_hygiene() -> None:
    """[RAG_HYGIENE] перед сводкой сна -> попадает в ту же сводку. Только числа.
    Сбой/таймаут -> WARNING (наблюдение не краснит светофор) и сон идёт дальше."""
    if not getattr(config, "OKO_RAG_HYGIENE", True):
        return
    try:
        counts = await asyncio.wait_for(asyncio.to_thread(_rag_hygiene_counts),
                                        timeout=RAG_HYGIENE_TIMEOUT_S)
        if counts is None:
            logger.info("[RAG_HYGIENE] skip=unavailable")
            return
        values = [int(counts[k]) for k in _RAG_KEYS]
    except Exception as e:
        logger.warning("[RAG_HYGIENE] fail=%s", type(e).__name__)
        return
    logger.info("[RAG_HYGIENE] " + " ".join(f"{k}=%d" for k in _RAG_KEYS), *values)


async def sleep_cycle_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if config.OKO_LOG_FILE is None:
        if not _sleep_state["disabled_logged"]:
            logger.info("[SLEEP] disabled: OKO_LOG_FILE not set")
            _sleep_state["disabled_logged"] = True
        return
    if game_mode.in_game(config):   # GAME_MODE v2: сон (RAG_HYGIENE: Chroma/диск) -- после игры
        logger.debug("[SLEEP] skip: game_mode")
        return
    governor = context.application.bot_data["governor"]
    try:
        db_last = await asyncio.to_thread(sleep_digest.last_sleep_ts, config.OPS_DB_PATH)
    except Exception as e:
        logger.warning("[SLEEP_FAIL] ops.db read: %s: %s", type(e).__name__, e)
        return
    marks = [t for t in (db_last, _sleep_state["last_attempt"]) if t is not None]
    ok, reason = sleep_digest.should_sleep(
        time.time(), _sleep_state["last_activity"], max(marks) if marks else None,
        getattr(governor, "transport_gate_state", "UNKNOWN"),
        config.SLEEP_IDLE_S, config.SLEEP_MIN_INTERVAL_S,
    )
    if not ok:
        logger.debug("[SLEEP] skip: %s", reason)
        return
    _sleep_state["last_attempt"] = time.time()  # до await: повторный тик не войдёт
    await _log_rag_hygiene()  # M7.4: до run_once -> строка войдёт в эту же сводку
    try:
        res = await asyncio.to_thread(
            sleep_digest.run_once, config.OKO_LOG_FILE, config.OPS_DB_PATH,
            baseline_since=getattr(config, "OKO_DRIFT_BASELINE_SINCE", None))
        if res.get("status") == "no_new_lines":
            logger.info("[SLEEP_DIGEST] no new log lines (rotated=%s)", res.get("rotated"))
    except Exception as e:
        logger.warning("[SLEEP_FAIL] %s: %s", type(e).__name__, e)


STARTUP_BACKOFF_CAP_S = 30.0


def _startup_delays(max_attempts: int, delay_s: float, cap_s: float = STARTUP_BACKOFF_CAP_S) -> list[float]:
    """STARTUP_BACKOFF (M5, 19.10): паузы МЕЖДУ попытками -- delay_s * 2**i,
    потолок cap_s. 6 попыток, 3 с -> 3, 6, 12, 24, 30 (~75 с ожидания):
    OllamaService (NSSM) после загрузки Windows поднимается не мгновенно,
    прежние 5 x 3 с (~12 с) не дожидались её и бот уходил в рестарт NSSM."""
    return [min(cap_s, delay_s * (2 ** i)) for i in range(max(0, max_attempts - 1))]


async def wait_for_model(governor: AgentGovernor, max_attempts: int = 6, delay_s: float = 3.0) -> None:
    """Ретраи поверх Registry.check_model_loaded (синхронный, single-shot).
    Оборачивается в asyncio.to_thread, чтобы не блокировать event loop на
    время urllib-запроса. Поднимает RuntimeError, если модель не готова
    после всех попыток — вызывающий код (post_init) решает, что делать
    дальше (обычно — не давать боту начать поллинг). Паузы -- нарастающие
    (_startup_delays)."""
    error_msg = "unknown error"
    delays = _startup_delays(max_attempts, delay_s)
    for attempt in range(1, max_attempts + 1):
        is_ok, error_msg = await asyncio.to_thread(
            governor.registry.check_model_loaded, timeout_s=2.0
        )
        if is_ok:
            logger.info("Ollama model check passed (attempt %d/%d).", attempt, max_attempts)
            return
        logger.warning(
            "Ollama not ready (attempt %d/%d): %s", attempt, max_attempts, error_msg
        )
        if attempt < max_attempts:
            await asyncio.sleep(delays[attempt - 1])

    raise RuntimeError(f"Ollama not ready after {max_attempts} attempts: {error_msg}")


def _caps_apply(app: Application) -> None:
    """CAPS_CATALOG (30.10, этап A): реестр подтверждений -> каталог функций -> governor.
    Реестр создаётся один раз (полный проход) и живёт в bot_data; дальше -- только
    refresh() (stat + дельта). В путь LLM-вызова диск не попадает: governor хранит
    готовую строку. Сбой -- WARNING, governor остаётся на чистом identity."""
    try:
        reg = app.bot_data.get("proof_registry")
        if reg is None:
            reg = proof_registry.ProofRegistry()
            app.bot_data["proof_registry"] = reg
        else:
            reg.refresh()
        app.bot_data["governor"].set_capabilities(capabilities.build_catalog(reg.latest()))
    except Exception as e:
        logger.warning("[CAPS] catalog not applied: %s: %s", type(e).__name__, e)


async def post_init(app: Application) -> None:
    """Запускает HTTP-клиент governor ПОСЛЕ того, как event loop поднят —
    httpx.AsyncClient привязан к loop в момент создания, поэтому это не
    может произойти в синхронном main() до app.run_polling()."""
    governor: AgentGovernor = app.bot_data["governor"]
    await wait_for_model(governor)
    await governor.start()
    logger.info("Governor lifecycle: started.")
    await asyncio.to_thread(_caps_apply, app)  # CAPS_CATALOG
    await asyncio.to_thread(event_log.init)    # EVENTS (этап C): хвост журнала событий


async def post_shutdown(app: Application) -> None:
    governor: AgentGovernor = app.bot_data["governor"]
    await governor.shutdown()
    logger.info("Governor lifecycle: shutdown complete.")


def main():
    """Точка входа и инициализации рантайма."""
    # setup_logging() вызывается РОВНО ОДИН РАЗ за жизнь процесса, здесь —
    # до любой другой инициализации. logging.basicConfig() молча игнорирует
    # повторные вызовы с другим форматом (подтверждено тестом при
    # разработке logger.py) — это не было реальным конфликтом между нашими
    # точками входа (каждая в своём процессе), но архитектурно хрупкое
    # свойство, которое logger.py устраняет явным предупреждением на
    # повторный вызов вместо молчаливого no-op.
    oko_logger.setup_logging()

    # PID-файл — единственный способ process_watchdog.py узнать, какой
    # процесс мониторить (см. историю решений: watchdog читает PID из
    # файла, не получает его как аргумент запуска, чтобы не требовать
    # координации аргументов между systemd unit'ом бота и unit'ом watchdog).
    # Записывается ДО любой другой инициализации — если что-то ниже упадёт,
    # PID-файл всё равно отражает, что процесс пытался стартовать.
    pid_file = config.BASE_DIR / "bot.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")


    if not config.ADMIN_USER_IDS:
        # ADMIN_ALLOWLIST_CHAT: без списка бот молчит для всех -- явно, не тихо.
        logger.warning(
            "ADMIN_USER_IDS is empty: bot will ignore ALL chat messages "
            "(fail-closed). Set ADMIN_USER_IDS in .env / service environment."
        )
    logger.info("Старт инициализации защищённого ядра контура...")
    _registry = Registry()

    # AgentGovernor() без аргументов: Registry создаётся внутри него самого
    # (singleton — тот же экземпляр, что и _registry выше). Параметр
    # конструктора (http_client) предназначен ТОЛЬКО для unit-тестов с
    # моком, не для передачи Registry — Registry туда передавать НЕЛЬЗЯ,
    # это вызовет AttributeError при первом обращении к self._http.post()
    # внутри _route_to_llm (Registry не имеет метода .post()).
    governor_kernel = AgentGovernor()

    # Отдельный ComplianceMonitor для входного трафика — НЕ governor.monitor,
    # тот отслеживает качество ответов модели, это — качество фильтрации
    # запросов диспетчером. window_size совпадает с дефолтом governor.monitor
    # (100) для единообразия; если входной/выходной трафик должны иметь
    # разные горизонты наблюдения — это отдельный параметр, не делал такого
    # разделения без явного запроса.
    input_monitor = ComplianceMonitor(window_size=100)

    # StagingManager создаётся здесь, не внутри initialize_guardrail —
    # это другая ответственность (HITL для входящих RAG-документов, не
    # privacy/compliance фильтрация). input_dir совпадает с тем, что
    # наблюдает run_system.py (config.BASE_DIR / "knowledge") — оба
    # процесса должны смотреть на одну и ту же директорию, иначе watcher
    # найдёт файл, которого StagingManager не ожидает.
    #
    # ИЗМЕНЕНО (см. историю решений — миграция pending_tasks в SQL):
    # раньше StagingManager принимал Registry и хранил pending_tasks как
    # JSON-словарь в registry_state.json — read-merge-write без файлового
    # лока давал воспроизведённый lost update между этим процессом и
    # run_system.py. StagingRepository делает то же через SQLite-транзакции
    # (db.get_connection(), тот же WAL-модуль, что использует остальной
    # проект) — lost update для pending_tasks теперь структурно невозможен.
    # setup_database() идемпотентна (CREATE TABLE IF NOT EXISTS) — безопасно
    # вызывать при каждом старте процесса, даже если run_system.py уже
    # создал таблицу первым.
    staging_repo = StagingRepository()
    staging_repo.setup_database()
    staging_manager = StagingManager(
        staging_repo,
        staging_dir=config.STAGING_DIR,
        input_dir=config.BASE_DIR / "knowledge",
    )

    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.bot_data["governor"] = governor_kernel
    app.bot_data["dispatcher"] = DataHygieneFilter()
    logger.info("Memory snapshot: %s", _get_memory().snapshot())
    app.bot_data["input_monitor"] = input_monitor
    app.bot_data["staging_manager"] = staging_manager

    app.add_handler(TypeHandler(Update, _touch_activity), group=-1)
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("memory", handle_memory))
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("discuss", handle_discuss))
    app.add_handler(CommandHandler("enddiscuss", handle_enddiscuss))
    app.add_handler(CommandHandler("forget", handle_forget))
    app.add_handler(CommandHandler("newepoch", handle_newepoch))
    app.add_handler(CommandHandler("tv", handle_tv))
    app.add_handler(CommandHandler("garmin", handle_garmin))
    app.add_handler(CommandHandler("force_brain", handle_force_brain))   # GAME_MODE v2
    app.add_handler(CommandHandler("cam", handle_cam))                   # CAMERA_WATCHER (03.11)
    app.add_handler(CommandHandler("amb", handle_amb))                   # AMB_MUSIC (04.11)
    if camera_watcher.enabled():   # CAM_VERIFY (03.11): «выключи телевизор» -> Standby -> кадр -> погас ли экран
        tv_bot_handler.VERIFY["standby"] = lambda: camera_watcher.verify_screen("off")
        tv_bot_handler.VERIFY["power_on"] = lambda: camera_watcher.verify_screen("on", waits=(4.0, 4.0, 4.0), retry=_wol_again)   # WoL: экран -- через ~5 с; тёмный -- пакет ещё раз
        tv_bot_handler.PRE["power_on"] = _cam_before_power_on   # SAFE-06 (04.11): кадр до WoL
    app.add_handler(CallbackQueryHandler(handle_staging_callback))
    # 28.10: Word/Excel идут тем же путём, что PDF (knowledge/ -> watcher -> staging -> office_text).
    app.add_handler(MessageHandler(
        filters.Document.PDF | filters.Document.FileExtension("docx") | filters.Document.FileExtension("xlsx"),
        handle_pdf_upload,
    ))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("doc") | filters.Document.FileExtension("xls"),
        handle_legacy_office,
    ))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    # JobQueue — pull-модель опроса метрик, не push из Governor (см. docstring
    # alert_checker_job). first=ALERT_CHECK_INTERVAL_S — не запускаем проверку
    # сразу при старте, когда sample_size заведомо 0 и любая проверка была бы
    # бессмысленной.
    app.job_queue.run_repeating(
        alert_checker_job,
        interval=config.ALERT_CHECK_INTERVAL_S,
        first=config.ALERT_CHECK_INTERVAL_S,
    )

    # staging_monitor_job — отдельный, более частый интервал (UX, не
    # здоровье системы, см. config.STAGING_CHECK_INTERVAL_S). first=5.0,
    # не сам интервал — staging-уведомления хотим увидеть быстро даже на
    # первом цикле после старта, в отличие от alert_checker_job, где нет
    # смысла проверять сразу (sample_size гарантированно 0).
    app.job_queue.run_repeating(
        staging_monitor_job,
        interval=config.STAGING_CHECK_INTERVAL_S,
        first=5.0,
    )

    # Heartbeat для process_watchdog.py — без этого watchdog не может
    # отличить "процесс жив и работает" от "процесс завис в дедлоке"
    # (см. историю решений). 30с — компромисс между частотой записи (это
    # дисковая операция на каждый тик) и достаточной детальностью для
    # WATCHDOG_HEARTBEAT_TIMEOUT_SEC=120с по умолчанию (4 пропущенных
    # heartbeat'а подряд — однозначный признак зависания, не просто
    # случайной задержки одного тика).
    app.job_queue.run_repeating(heartbeat_job, interval=30.0, first=0.0)
    app.job_queue.run_repeating(memory_ttl_job, interval=6 * 3600, first=60.0)
    # M7 сон: тик раз в SLEEP_CHECK_INTERVAL_S; решение — в sleep_cycle_job.
    app.job_queue.run_repeating(
        sleep_cycle_job,
        interval=config.SLEEP_CHECK_INTERVAL_S,
        first=config.SLEEP_CHECK_INTERVAL_S,
    )

    # GARMIN (29.10): сводки 08:00 и 21:00 по местному времени; включается наличием токенов Garmin.
    if garmin_report.enabled():
        for _hh, _kind in ((8, "morning"), (21, "evening")):
            app.job_queue.run_daily(garmin_job, time=garmin_report.local_time(_hh), data=_kind, name=f"garmin_{_kind}")
        logger.info("[GARMIN] сводки включены: 08:00 и 21:00 (%s)", garmin_report.tz_name())
    else:
        logger.info("[GARMIN] токенов нет — сводки выключены (garmin_sync.py login)")

    # STT (28.10): прогрев модели распознавания речи через 20 с после старта.
    app.job_queue.run_once(_stt_warmup, when=20.0)

    # Техника дома (28.10): включается OKO_SMARTTHINGS_TOKEN; интервал не меньше 30 с (лимит облака SmartThings).
    if device_watch.enabled():
        app.job_queue.run_repeating(
            device_watch_job,
            interval=device_watch.poll_interval_s(),
            first=30.0,
        )
        logger.info("[DEVICES] наблюдение за техникой включено, интервал %.0f с", device_watch.poll_interval_s())
    else:
        logger.info("[DEVICES] OKO_SMARTTHINGS_TOKEN не задан — наблюдение за техникой выключено")

    # Смена сети (29.10): отпечаток шлюза раз в минуту; при подтверждённой смене — проверка контура и одна сводка.
    app.job_queue.run_repeating(net_context_job, interval=net_context.POLL_S, first=45.0, name="net_context")
    logger.info("[NET] наблюдение за сменой сети включено, интервал %.0f с", net_context.POLL_S)

    logger.info("Все системы верифицированы. Контур исполнения запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()

