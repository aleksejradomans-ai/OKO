"""shell_channel.py — канал desktop для Oko Shell (S1, 10.11.2026). ТЗ: claude/TZ_oko_shell_2026-11-10.md.

Один мозг — несколько каналов. Окно на ПК (сеанс пользователя) шлёт текст сюда, а этот модуль
отдаёт его ТЕМ ЖЕ обработчикам, что и Telegram (handle_message / handle_tv / handle_status) через
утиные объекты Update/Context. Своего маршрута, своего вызова мозга и своей проверки здесь нет:
вторая ветка без судьи = F2-01 («одна ветка пропускает ответ без проверки»). Ответы, которые
обработчик отправил бы в Telegram (message.reply_text), собираются в список и уходят в окно.

Транспорт: TCP только на 127.0.0.1, одна JSON-строка на запрос:
    -> {"key": "<OKO_SHELL_KEY>", "text": "подсветка синяя"}
    <- {"ok": true, "replies": ["Подсветка: ..."]}     |  {"ok": false, "error": "auth"}
Ключ — OKO_SHELL_KEY в .env; пусто -> канал выключен (fail-closed). Текст запроса в журнал не пишется
(только длина и вид), как у Telegram-обработчиков.
B18 (18.11, голос окна): запрос может нести поле "audio" — путь к временной записи окна. Обработчик "audio"
(bot.py -> shell_voice) распознаёт её общей моделью и удаляет файл; дальше текст идёт тем же путём, что набранный.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from types import SimpleNamespace
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("shell_channel")

MAX_LINE = 16 * 1024          # байт на строку запроса
READ_TIMEOUT_S = 10.0         # клиент обязан прислать строку быстро
HOST = "127.0.0.1"            # наружу не слушаем никогда: адрес не настраивается


class _Message:
    """То, что обработчики бота трогают у update.message: text, from_user.id, reply_text()."""

    def __init__(self, text: str, user_id: int, sink: List[str]):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.chat_id = user_id
        self.voice = None
        self.document = None
        self.caption = None
        self.via_voice = False          # B18d: текст пришёл расшифровкой голоса окна (журнал ТОЛК: kind=voice)
        self._sink = sink

    async def reply_text(self, text, *args, **kwargs):
        self._sink.append(str(text))
        return SimpleNamespace(message_id=len(self._sink))


def _update(message: _Message):
    return SimpleNamespace(message=message, effective_user=message.from_user,
                           effective_chat=SimpleNamespace(id=message.chat_id), callback_query=None)


def _context(app, args: Optional[List[str]] = None):
    return SimpleNamespace(application=app, bot_data=getattr(app, "bot_data", {}), args=args or [],
                           bot=getattr(app, "bot", None))


def operator_id(admin_ids, explicit: str = "") -> Optional[int]:
    """Чей это канал: OKO_SHELL_USER_ID (должен быть админом) или единственный админ. Иначе — None."""
    admins = set(admin_ids or ())
    if explicit.strip():
        try:
            uid = int(explicit.strip())
        except ValueError:
            return None
        return uid if uid in admins else None
    return next(iter(admins)) if len(admins) == 1 else None


async def dispatch(app, user_id: int, text: str, handlers: Dict[str, Callable[..., Awaitable]],
                   audio: str = "") -> List[str]:
    """Текст -> обработчик Telegram -> список ответов. /tv и /status — как команды, остальное — сообщение.
    audio (B18): путь записи окна -> handlers["audio"](путь) -> текст (файл удаляет обработчик) -> как набранный."""
    sink: List[str] = []
    if audio:
        fn = handlers.get("audio")
        if fn is None:
            sink.append("Голос в окне пока недоступен.")
            return sink
        try:
            text = await fn(audio)
        except ValueError as e:                     # shell_voice.VoiceRejected и ошибки записи -- текст для человека
            sink.append(f"🎙 {e}")
            return sink
        sink.append(f"🎙 «{text}»")
    text = (text or "").strip()
    if not text:
        return sink
    msg = _Message(text, user_id, sink)
    msg.via_voice = bool(audio)
    upd = _update(msg)
    touch = handlers.get("touch")
    if touch is not None:
        await touch(upd, _context(app))
    if text.startswith("/"):
        head, _, rest = text[1:].partition(" ")
        cmd = head.split("@", 1)[0].lower()
        fn = handlers.get(cmd)
        if fn is None:
            sink.append("Команда в окне пока недоступна — работает в Telegram.")
            return sink
        await fn(upd, _context(app, rest.split()))
        return sink
    await handlers["text"](upd, _context(app))
    return sink


def _reply(writer, obj) -> None:
    writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


class ShellChannel:
    def __init__(self, app, key: str, user_id: int, handlers: Dict[str, Callable[..., Awaitable]],
                 max_text: int = 4000):
        self.app = app
        self.key = key.encode("utf-8")
        self.user_id = user_id
        self.handlers = handlers
        self.max_text = max_text
        self.server: Optional[asyncio.base_events.Server] = None

    async def start(self, port: int) -> int:
        self.server = await asyncio.start_server(self._serve, HOST, port, limit=MAX_LINE)
        bound = self.server.sockets[0].getsockname()[1]
        logger.info("[SHELL] listening %s:%s", HOST, bound)
        return bound

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _serve(self, reader, writer) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_S)
            except (asyncio.TimeoutError, ValueError, asyncio.LimitOverrunError):
                _reply(writer, {"ok": False, "error": "bad_request"})
                return
            try:
                req = json.loads(line.decode("utf-8"))
                key = str(req.get("key", "")).encode("utf-8")
                text = str(req.get("text", ""))
                audio = str(req.get("audio", "") or "")
            except Exception:
                _reply(writer, {"ok": False, "error": "bad_request"})
                return
            if not hmac.compare_digest(key, self.key):
                logger.warning("[SHELL] auth fail")
                _reply(writer, {"ok": False, "error": "auth"})
                return
            if len(text) > self.max_text:
                _reply(writer, {"ok": False, "error": "too_long", "max": self.max_text})
                return
            logger.info("[SHELL] request len=%d kind=%s", len(text),
                        "audio" if audio else ("cmd" if text.strip().startswith("/") else "text"))
            try:
                replies = await dispatch(self.app, self.user_id, text, self.handlers, audio=audio)
            except Exception as e:
                logger.exception("[SHELL] handler failed: %s", type(e).__name__)
                _reply(writer, {"ok": False, "error": "internal"})
                return
            _reply(writer, {"ok": True, "replies": replies})
        finally:
            try:
                await writer.drain()
            except Exception:
                pass
            writer.close()


async def start_from_config(app, config, handlers) -> Optional[ShellChannel]:
    """Включает канал, если задан OKO_SHELL_KEY и понятно, чей он. Иначе — WARNING и None (бот живёт дальше)."""
    key = getattr(config, "OKO_SHELL_KEY", "")
    if not key:
        logger.info("[SHELL] disabled (OKO_SHELL_KEY empty)")
        return None
    uid = operator_id(config.ADMIN_USER_IDS, getattr(config, "OKO_SHELL_USER_ID", ""))
    if uid is None:
        logger.warning("[SHELL] disabled: operator unknown (set OKO_SHELL_USER_ID to one of ADMIN_USER_IDS)")
        return None
    ch = ShellChannel(app, key, uid, handlers, max_text=getattr(config, "OKO_SHELL_MAX_TEXT", 4000))
    await ch.start(getattr(config, "OKO_SHELL_PORT", 47321))
    return ch
