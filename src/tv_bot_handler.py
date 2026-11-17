"""Связка разбора команд телевизора с Telegram-ботом.

Слой намеренно тонкий. Разбор текста живёт в `tv_commands`, транспорт — в
`tv_control`; здесь только то, чего нет ни там, ни там: права, подтверждение
опасных команд и перевод исключений в текст для человека.

ПОЧЕМУ ТОЛЬКО ПО ПРЕФИКСУ. Обработчик вызывается ИСКЛЮЧИТЕЛЬНО для команды
`/tv ...`, а не для каждого текстового сообщения. Если пропускать через него
весь поток, то любой вопрос не про телевизор получит отказ разбора, а значит
бот перестанет отвечать вообще; неадмин получит отказ доступа на любую свою
реплику; а висящее ожидание подтверждения перехватит следующую фразу
Оператора, чем бы она ни была. Префикс убирает все три последствия сразу.

ПОЧЕМУ `needs_confirm`, А НЕ ИМЯ КОМАНДЫ. Признак опасности проставляет
парсер. Сверка с конкретным именем (`name == "standby"`) означает, что
следующая команда, помеченная подтверждением, выполнится молча.

ЧЕСТНОСТЬ ОТВЕТА. `Command.kind` различает проверенные команды и слепые
нажатия. Для слепых телевизор отвечает кодом 200 на любое имя кнопки — это
измерено 27.10, — поэтому ответ сообщает об ОТПРАВКЕ, а не об эффекте.
Утверждать «телевизор выключен» на основании кода ответа нельзя.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import menu_syntax   # 05.11 MENU_SYNTAX: имена пунктов меню ТВ -> маршрут по карте
try:                 # (16.11, фаза 3, этап 3d): шлюз Σ перед отправкой команды ТВ (теневой режим по умолчанию).
    import sigma_gate    # Модуль приватен (ALLOWED_GAPS export_public): без него -- прежний путь исполнения.
except ImportError:  # pragma: no cover - публичное зеркало без слоя Σ
    sigma_gate = None
import tv_commands
import tv_intent
import tv_nav
from tv_commands import BLIND, Command, Unknown
from tv_control import (
    TVAuthError,
    TVError,
    TVNotPaired,
    TVTransportError,
    TVUnsupported,
)

logger = logging.getLogger("oko.tv_bot")

# CAM_VERIFY (03.11): проверка исполнения по кадру камеры -- «действие -> пауза -> кадр -> вывод» в одном вызове.
# Ключ -- Command.name, значение -- функция без аргументов, возвращающая строку для ответа. ПУСТО по умолчанию
# (тесты, зеркало без камеры); bot.py регистрирует проверки при старте, только если камера настроена.
# Сбой проверки не ломает ответ о самой команде: пишется «проверить по кадру не удалось».
VERIFY: dict[str, Callable[[], str]] = {}
# CALIB_AUTO/SAFE-06 (04.11): то же ДО команды. power_on: кадр камеры, пока ТВ выключен (экран тёмный) --
# snapshot() сам пересчитает углы, если камеру сдвинули. Пусто по умолчанию; сбой не мешает команде.
PRE: dict[str, Callable[[], str]] = {}

# Слова согласия. «подтвердить» и «подтверждаю» включены оба: подсказка
# предлагает одно слово, человек печатает любое из однокоренных, и
# расхождение между текстом подсказки и принимаемым списком — это дефект,
# а не придирка.
CONFIRM_WORDS = frozenset({
    "да", "ага", "ок", "ok", "окей",
    "подтверждаю", "подтвердить", "подтверждать",
    "confirm", "yes", "y",
})

HELP_WORDS = frozenset({"помощь", "help", "?", "команды"})

# Срок жизни ожидания. Брошенное подтверждение не должно срабатывать через
# час от случайного «да» в другом разговоре.
CONFIRM_TTL_S = 90.0

PREFIX_NOTE_BLIND = (
    # 29.10 (Оператор): коротко и по-человечески. Суть прежняя: телевизор отвечает 200 на любое имя
    # кнопки (замер 27.10), подтверждения нажатия нет — поэтому «отправил», а не «сделал».
    "(телевизор не подтверждает нажатия — смотри на экран)"
)


@dataclass
class _Pending:
    command: Command
    deadline: float


class PendingStore:
    """Ожидания подтверждения по пользователю.

    Отдельный объект, а не модульный словарь: модульное состояние течёт между
    тестами и делает их порядок значимым, а в боте с несколькими админами
    общий словарь смешал бы их ожидания.
    """

    def __init__(self) -> None:
        self._items: dict[int, _Pending] = {}

    def put(self, user_id: int, command: Command, now: float) -> None:
        self._items[user_id] = _Pending(command, now + CONFIRM_TTL_S)

    def take(self, user_id: int, now: float) -> tuple[Command | None, bool]:
        """Возвращает (команда, истекло_ли). Запись снимается в любом случае."""
        item = self._items.pop(user_id, None)
        if item is None:
            return None, False
        if now >= item.deadline:
            return None, True
        return item.command, False

    def pending_for(self, user_id: int) -> bool:
        return user_id in self._items

    def clear(self) -> None:
        self._items.clear()


DEFAULT_PENDING = PendingStore()


# (16.11, решение Оператора 21:54): знаковые команды «+1», «−2», «+», «++».
# Сессия -- пауза ≤ 60 с (как τ_D, решение 12:04) от последней исполненной команды, менявшей параметр. В сессии помнится
# ПАРАМЕТР последней такой команды (яркость / насыщенность / громкость). Исполненная команда без шага (цвет, режим,
# кнопка, выключение) делает «последний параметр» пустым; чтения («громкость», «статус») сессию не трогают.
# Нет сессии / параметр пуст / пауза > 60 с -> переспрос «чего +1?», ничего не исполняется. Текст пользователя не хранится.
SIGN_SESSION_S = 60.0
SIGN_NO_PARAM_TEXT = ("❓ Чего {label}? Ничего не отправлено. Напиши с предметом — «яркость {label}», «громкость {label}» — "
                      "или сначала команду (например «яркость 5»), а потом «{label}» в течение 60 с.")
SIGN_BAD_TEXT = ("❓ Не понял шаг «{label}» — ничего не отправлено. Можно: «+1», «−2» (целое число), «+», «−», «++», «−−».")
_VOL_READ_RE = re.compile(r"^Громкость: (\d+)/")


@dataclass
class _SignState:
    param: "str | None"      # brightness | saturation | volume | None (последняя исполненная команда -- без шага)
    at: float                # момент последней исполненной команды с изменением (часы handle: monotonic или now)
    volume: "int | None"     # громкость по последнему ответу / цели команды (для потолка 15 до отправки); None -- нет


def _answer_ok(answer: Any) -> bool:
    """Команда исполнена (как PASS / WARN у router.tv_outcome): ответ модуля ТВ, не ошибка «📺», не отказ / переспрос."""
    lines = [l.strip() for l in (answer if isinstance(answer, str) else "").splitlines() if l.strip()]
    if not lines:
        return False
    if any(l.startswith("📺") and not l.startswith("📺 Телевизор сменил адрес") for l in lines):
        return False
    head = re.sub(r"^Понял как «[^»]*»\.\s*", "", lines[0])
    return head.startswith(("Подсветка", "Громкость", "Звук", "Отправил"))


class SignSessions:
    """Последний параметр на пользователя для знаковых команд. Отдельный объект (как PendingStore): тесты и админы
    не делят состояние."""

    def __init__(self) -> None:
        self._items: dict[int, _SignState] = {}

    def get(self, user_id: int, now: float) -> "_SignState | None":
        st = self._items.get(user_id)
        if st is None:
            return None
        dt = now - st.at
        if dt < 0 or dt > SIGN_SESSION_S:      # часы назад -- сессии нет (fail-closed); пауза > 60 с -- новая сессия
            return None
        return st

    def note(self, user_id: int, command: Command, answer: Any, now: float) -> None:
        """После исполнения команды: параметр (или пусто) и момент. Чтения, локальные шаги меню и неисполненное --
        без изменений."""
        try:
            name = getattr(command, "name", "")
            if command.kind == tv_nav.LOCAL_KIND or name in tv_commands.SIGN_READS or not _answer_ok(answer):
                return
            param = tv_commands.sign_param(name)
            vol = None
            if param == "volume":
                head = re.sub(r"^Понял как «[^»]*»\.\s*", "", (answer.strip().splitlines() or [""])[0])
                m = _VOL_READ_RE.match(head)
                prev = self._items.get(user_id)
                d = _d_cmd_of(command)
                if m:
                    vol = int(m.group(1))
                elif name == "volume_set" and isinstance(command.meta.get("level"), int):
                    vol = int(command.meta["level"])
                elif prev is not None and prev.param == "volume" and prev.volume is not None and d is not None:
                    vol = max(0, prev.volume + d)
            self._items[user_id] = _SignState(param, now, vol)
        except Exception:  # noqa: BLE001 - сессия знаков не влияет на ответ
            logger.warning("[TV_BOT] сессия знаков: запись не удалась")

    def clear(self) -> None:
        self._items.clear()


DEFAULT_SIGNS = SignSessions()


def _sign_resolve(user_id: int, sign: Any, now: float, signs: SignSessions) -> "tuple[str, str, Any]":
    """-> ("tv", reason, Command) | ("clarify", reason, текст). Решает только разбор и сессия; ТВ не трогается."""
    if sign.delta is None:
        return "clarify", "sign:bad", SIGN_BAD_TEXT.format(label=sign.label)
    st = signs.get(user_id, now)
    if st is None or st.param is None:
        return "clarify", "sign:no_param", SIGN_NO_PARAM_TEXT.format(label=sign.label)
    if (st.param == "volume" and sign.delta > 0 and st.volume is not None
            and st.volume + sign.delta > tv_commands.SIGN_VOLUME_MAX):
        return "clarify", "sign:volume_cap", tv_commands.volume_sign_refusal(st.volume, sign.delta, sign.label)
    try:
        command = tv_commands.sign_command(st.param, sign.delta, sign.label)
    except Unknown:
        return "clarify", "sign:no_param", SIGN_NO_PARAM_TEXT.format(label=sign.label)
    return "tv", f"sign:{command.name}", command


def sign_route(
    user_id: int,
    text: str,
    *,
    admin_ids: Iterable[int] | None = None,
    pending: PendingStore | None = None,
    signs: SignSessions | None = None,
    now: float | None = None,
) -> "tuple[str, str, str, str] | None":
    """«знаковые команды», для bot._route_reply ДО маршрутизатора (router.py голый знак отдаёт модели; router здесь не меняется).
    Не знаковая запись или не админ -> None (всё как раньше). Иначе (kind, reason, text, phrase): kind "tv" -- отдать
    text в handle как команду ТВ (handle разберёт знак сам, с той же сессией); phrase -- та же команда словами
    («яркость плюс 1») для контекста продолжения (CTX_FOLLOW: «ещё 2» -> «яркость плюс 2», а не «яркость 2»);
    "clarify" -- text переспроса, ничего не исполнять. Только разбор: ТВ, журнал, сессия не трогаются."""
    sign = tv_commands.parse_sign(strip_prefix(text))
    if sign is None:
        return None
    ids = frozenset(int(x) for x in admin_ids) if admin_ids is not None else _default_admin_ids()
    if user_id not in ids:
        return None
    store = DEFAULT_PENDING if pending is None else pending
    if store.pending_for(user_id):
        return "tv", "sign:pending", text, ""   # handle снимет ожидание подтверждения, как любой другой текст
    kind, reason, val = _sign_resolve(user_id, sign, time.monotonic() if now is None else now,
                                      DEFAULT_SIGNS if signs is None else signs)
    if kind != "tv":
        return kind, reason, val, ""
    param = tv_commands.sign_param(val.name) or ""
    return kind, reason, text, (tv_commands.sign_phrase(param, sign.delta) if param else "")


def _default_admin_ids() -> frozenset[int]:
    """ID админов из `config`. Пустое множество = никто не допущен.

    Fail-closed намеренно и совпадает с поведением `bot.py`: отсутствие
    настройки не должно открывать управление периферией всем.
    """
    try:
        import config  # noqa: PLC0415  (поздний импорт: модуль тестируется без config)
    except Exception:  # pragma: no cover - в боте config есть всегда
        logger.warning("[TV_BOT] config недоступен, доступ закрыт для всех")
        return frozenset()
    raw = getattr(config, "ADMIN_USER_IDS", None) or ()
    return frozenset(int(x) for x in raw)


def strip_prefix(text: str) -> str:
    """Срезает `/tv`, `/tv@bot` и лишние пробелы. Остальное не трогает."""
    s = (text or "").strip()
    if s.startswith("/"):
        head, _, tail = s.partition(" ")
        head = head.split("@", 1)[0]
        if head.lower() in {"/tv", "/тв"}:
            return tail.strip()
    return s


def describe_command(command: Command) -> str:
    """Короткое описание для подсказки подтверждения, без текста пользователя."""
    key = command.meta.get("key")
    if command.name == "standby":
        return "перевод телевизора в режим ожидания"
    if key:
        return f"нажатие кнопки {key}"
    return command.name


def handle(
    user_id: int,
    text: str,
    tv_factory: Callable[[], Any],
    *,
    pending: PendingStore | None = None,
    admin_ids: Iterable[int] | None = None,
    now: float | None = None,
    signs: "SignSessions | None" = None,
) -> str:
    """Текст команды -> текст ответа. Сеть только внутри `tv_factory`.

    `tv_factory` вызывается ТОЛЬКО когда есть что выполнять: разбор, отказ по
    правам и запрос подтверждения не должны создавать соединение.
    """
    store = DEFAULT_PENDING if pending is None else pending
    ids = frozenset(int(x) for x in admin_ids) if admin_ids is not None else _default_admin_ids()
    moment = time.monotonic() if now is None else now
    sessions = DEFAULT_SIGNS if signs is None else signs   # последний параметр для «+1» / «−2»

    if user_id not in ids:
        logger.warning("[TV_BOT] отказ по правам user=%s", user_id)
        return "Управление телевизором доступно только администратору."

    payload = tv_nav.canonical(strip_prefix(text), extra=menu_syntax.vocabulary())   # 28.10: «ok», «vpravo», «dybp» -> слова словаря

    # --- ветка подтверждения ------------------------------------------------
    if store.pending_for(user_id):
        command, expired = store.take(user_id, moment)
        if expired:
            shadow_event("error", err="cancel")   # только код шага, без текста и id
            prefix = "Предыдущий запрос подтверждения истёк. "
            if not payload:
                return prefix + "Повторите команду."
            # Текст разбирается как новая команда: ожидание уже недействительно.
        elif payload.lower() in CONFIRM_WORDS:
            assert command is not None
            logger.info("[TV_BOT] подтверждено user=%s cmd=%s", user_id, command.name)
            answer = _execute(command, tv_factory)   # подтверждённая опасная команда (правила)
            sessions.note(user_id, command, answer, moment)
            return answer
        else:
            # Любой другой текст отменяет ожидание и НЕ выполняется: при
            # висящем подтверждении опасной команды двусмысленность решается
            # в пользу отказа, а не догадки о намерении.
            logger.info("[TV_BOT] отменено user=%s cmd=%s",
                        user_id, command.name if command else "?")
            shadow_event("error", err="cancel", command=command)
            return "Отменено. Повторите команду, если она ещё нужна."
    else:
        prefix = ""

    # «+1», «−2», «+», «++» -- шаг последнего параметра сессии (≤ 60 с); иначе переспрос, ничего не исполняется.
    # Проверка по исходному тексту ДО help: canonical/normalize стирают «++» и «−» (пустой текст ушёл бы в подсказку).
    sign = tv_commands.parse_sign(strip_prefix(text))
    if sign is not None:
        kind, reason, val = _sign_resolve(user_id, sign, moment, sessions)
        if kind != "tv":
            logger.info("[TV_BOT] знак: переспрос user=%s reason=%s", user_id, reason)
            return prefix + val
        command = val
        logger.info("[TV_BOT] знак: выполняется user=%s cmd=%s", user_id, command.name)
        param = tv_commands.sign_param(command.name) or ""
        understood = f"Понял как «{tv_commands.sign_phrase(param, sign.delta)}». " if param else ""
        _INTENT.src = "rule"      # разбор правилами (Σ: класс оценки намерения)
        try:
            answer = _execute(command, tv_factory)
        finally:
            _INTENT.src = "rule"
        sessions.note(user_id, command, answer, moment)
        if isinstance(answer, str) and answer.startswith("📺"):
            return prefix + answer        # ошибка контура -- первой строкой, как у прочих команд (router.tv_outcome: FAIL)
        return prefix + understood + answer

    if not payload or payload.lower() in HELP_WORDS:
        return tv_commands.help_text()

    understood = ""
    try:
        command = tv_nav.parse(payload) or menu_syntax.parse(payload) or tv_commands.parse(payload)
    except Unknown as exc:
        # 30.10 (Оператор: «читать суть, а не слова»): правила не узнали фразу — смысл извлекает локальная
        # модель, выбирая из закрытого каталога; её выбор снова проходит обычный разбор (tv_intent).
        # Только «не понял». Сознательный отказ (tv_commands.Refusal) и отказ навигации (tv_nav.NavError,
        # напр. «вниз 99» — слишком много нажатий) — это ПОНЯТЫЕ команды: модель их не переписывает
        # (30.10: модель превратила «вниз 99» во «вниз 9» и обошла предел).
        plain = type(exc) is Unknown
        phrase = tv_intent.understand(payload) if plain else None
        command = None
        if phrase:
            try:
                command = tv_nav.parse(phrase) or menu_syntax.parse(phrase) or tv_commands.parse(phrase)
                understood = f"Понял как «{phrase}». "
            except Unknown:
                command = None
        if command is None:
            logger.info("[TV_BOT] не разобрано user=%s", user_id)
            if plain:
                shadow_event("error", err="parse_fail")   # осознанный отказ (Refusal/NavError) -- не сбой разбора
            return prefix + f"Не выполнено: {exc}."
        logger.info("[TV_BOT] понято моделью user=%s cmd=%s", user_id, command.name)
    prefix += understood

    if command.needs_confirm:
        store.put(user_id, command, moment)
        logger.info("[TV_BOT] ожидание подтверждения user=%s cmd=%s", user_id, command.name)
        return (
            prefix
            + f"Требуется подтверждение: {describe_command(command)}. "
            + f"Отправьте «/tv да» в течение {int(CONFIRM_TTL_S)} секунд, "
            + "любой другой текст отменит."
        )

    logger.info("[TV_BOT] выполняется user=%s cmd=%s kind=%s",
                user_id, command.name, command.kind)
    _INTENT.src = "model" if understood else "rule"   # класс оценки намерения (сигнатура _execute прежняя)
    try:
        answer = _execute(command, tv_factory)
    finally:
        _INTENT.src = "rule"
    sessions.note(user_id, command, answer, moment)   # последний параметр сессии
    return prefix + answer


def transport_text(exc: BaseException, host: str | None) -> str:
    """29.10 (Оператор: «понятные значения выводов»): сетевой сбой — человеческими словами, по причине.
    Технический текст requests уходит в журнал, а не пользователю. Причина — НЕПОСРЕДСТВЕННОЕ исключение
    requests (__cause__ у TVTransportError), по именам классов: модуль не зависит от requests при импорте.
    Глубже по цепочке не идём: у urllib3 NewConnectionError наследует ConnectTimeoutError даже при отказе
    соединения — смешение дало бы неверную причину."""
    cause = exc.__cause__ or exc
    names = {c.__name__ for c in type(cause).__mro__}
    where = f" по адресу {host}" if host else ""
    if "ConnectTimeout" in names:
        return (f"📺 Телевизор не отвечает{where}. Скорее всего, он выключен или подключён к другой сети, "
                "чем ПК. Включи его или подключи к той же сети и повтори.")
    if "ReadTimeout" in names:
        return "📺 Телевизор принял команду, но не ответил вовремя (занят или загружается). Повтори через пару секунд."
    if "SSLError" in names:
        return f"📺 Телевизор{where} ответил, но защищённое соединение не установилось. Повтори; если не пройдёт — перезагрузи телевизор."
    if "ConnectionError" in names or "ConnectionRefusedError" in names:
        return (f"📺 Телевизор{where} отказал в соединении: включён, но управление ещё не готово "
                "(загружается) или это уже другое устройство. Подожди минуту и повтори.")
    return f"📺 Телевизор{where} недоступен по сети. Проверь, что он включён и в одной сети с ПК."


def _net24(ip: str) -> str:
    return ip.rsplit(".", 1)[0] + ".x"


def _local_ips() -> list[str]:
    from tv_control import local_ipv4
    return local_ipv4()


LOCAL_IPS: Callable[[], list[str]] = _local_ips   # подменяется в тестах: текст не должен зависеть от сети машины


def _other_network(host: str | None, local: Callable[[], list[str]] | None = None) -> tuple[str, str] | None:
    """(сеть ТВ, сеть ПК), если последний адрес ТВ — не в сетях ПК (/24). Иначе None. Не бросает."""
    if not host or host.count(".") != 3:
        return None
    try:
        nets = {_net24(ip) for ip in (local or LOCAL_IPS)()}
    except Exception:  # noqa: BLE001 - подсказка, не повод для сбоя
        return None
    if not nets or _net24(host) in nets:
        return None
    return _net24(host), ", ".join(sorted(nets))


def network_hint(exc: BaseException, host: str | None) -> str:
    """30.10: при НЕустановленном соединении — факт о сетях: ПК знает свою сеть и последний адрес ТВ.
    Отдельно от transport_text: тот текст не зависит от сети машины (его тесты детерминированы)."""
    cause = exc.__cause__ or exc
    if "ConnectTimeout" not in {c.__name__ for c in type(cause).__mro__}:
        return ""
    other = _other_network(host)
    if not other:
        return ""
    return (f"\nСеть ПК сейчас {other[1]}, а ТВ последний раз был в {other[0]} — это разные сети: "
            "ПК его оттуда не достанет.")


def _pre(name: str) -> str:
    """PRE-проверка до команды: строка для ответа или "". Исключение не мешает команде."""
    hook = PRE.get(name)
    if hook is None:
        return ""
    try:
        return hook() or ""
    except Exception as exc:  # noqa: BLE001 - кадр до команды -- подсказка, не условие
        logger.warning("[TV_BOT] проверка до команды cmd=%s: %s", name, type(exc).__name__)
        return ""


# AMB_MUSIC (04.11): команды, которые сами ставят стиль подсветки. Мост «под музыку» пишет лампы поштучно 7 раз в
# секунду -- без остановки он перебил бы такую команду, а потом вернул бы свой «прежний» стиль поверх неё.
# Яркость/насыщенность меню (710/720) и чтения стиль не меняют -- мост не трогают.
STOPS_MUSIC = frozenset({"amb_off", "amb_on", "amb_audio", "amb_video", "amb_colour", "amb_ai"})


def _stop_music(name: str) -> str:
    """Остановить мост до команды, меняющей стиль. Строка для ответа или "". Сбой остановки команде не мешает."""
    if name not in STOPS_MUSIC:
        return ""
    try:
        import amb_music  # noqa: PLC0415
        return amb_music.stop()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TV_BOT] остановка моста под музыку: %s", type(exc).__name__)
        return ""


# --- (15.11, фаза 2b Σ): теневой журнал исполнения команд ТВ -------------------------------------------------
# ТОЛЬКО ЗАПИСЬ. Решения, порядок и условия команд те же: при OKO_SIGMA_SHADOW != "1" журнал не участвует вовсе
# (клиент ТВ передаётся команде как раньше), при "1" -- клиент обёрнут в _ShadowTV, которая каждый вызов делает ровно
# один раз и только отмечает время/числа. Сбой записи глушится и на команду не влияет.
# Строка -- белый список SHADOW_FIELDS; текст ввода/ответа, id пользователя/чата, ключи, сырой ответ API -- запрещены.
# Путь: OKO_SIGMA_SHADOW_PATH или <корень>/logs/sigma_shadow.jsonl. Режим сбора (OKO_COLLECTOR=1) путь НЕ перенаправляет:
# путь внутри временной папки сбора (oko_collector_*, config.COLLECTOR_DIR) отвергается -> путь по умолчанию
# (решение Оператора 15.11 11:5x: данные 2c не должны уходить в TEMP и стираться при перезапуске).
SHADOW_FLAG_ENV = "OKO_SIGMA_SHADOW"
SHADOW_PATH_ENV = "OKO_SIGMA_SHADOW_PATH"
SHADOW_MAX_ENV = "OKO_SIGMA_SHADOW_MAX_BYTES"
SHADOW_MAX_BYTES = 5 * 1024 * 1024          # ротация: один предыдущий файл <имя>.1, итого <= ~10 МиБ
SHADOW_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "logs" / "sigma_shadow.jsonl"
SHADOW_FIELDS = frozenset({"ts", "step", "cmd_kind", "action", "feature", "s_exp", "s_obs",
                           "node_from", "node_to", "outcome", "err", "latency_ms", "seq",
                           "d_cmd", "s_raw", "sigma", "sigma_p", "sigma_why", "a_t", "sigma_live"})
# (16.11, фаза 3, этап 3d п.3–4): вердикт Σ перед отправкой -- только коды и флаги:
#   sigma / sigma_p -- EXECUTE|OBSERVE|CLARIFY|HALT по params / с предложением «Ока» по оценке намерения;
#   sigma_why -- первая причина (код); a_t -- автомат тревоги 0/1; sigma_live -- режим OKO_SIGMA_LIVE 0/1.
#   Шаг "sigma" -- строка боевого режима без исполнения (блокировка / Active Probing); в теневом режиме не пишется.
# (16.11, фаза 3, этап 3c; решение Оператора 15:36 п.3 -- поля белого списка; остаток SET/STEP, наблюдение вне шкалы):
#   d_cmd -- Δ_cmd, величина шага команды STEP (целое со знаком) из разбора команды (meta: delta, или step со знаком
#            по имени команды); у SET-команд и прочих поля нет (NO_DATA);
#   s_raw -- сырое значение признака из ответа ТВ до приведения к числу/шкале (до int(), без обрезания): то же
#            чтение, что дало s_obs; не число -> поля нет. Классификация INVALID_OBSERVATION -- в ядре
#            (abstraction_layer.check_observation), журнал только пишет. Вызовы ТВ, порядок, ответы -- прежние.
SHADOW_STEPS = frozenset({"send", "ack", "readback", "outcome", "menu_step", "error", "sigma"})
SIGMA_OUTCOMES = frozenset({"EXECUTE", "OBSERVE", "CLARIFY", "HALT"})
SHADOW_OUTCOMES = frozenset({"PASS", "WARN", "FAIL"})
SHADOW_ERRS = frozenset({"net", "parse_fail", "cancel", "verify_fail", "warn"})
SHADOW_KINDS = frozenset({BLIND, "verified"})
_CODE_RE = re.compile(r"^[A-Za-z0-9_]{1,48}$")
_NODE_RE = re.compile(r"^[^\r\n\t\"\\]{1,96}$")
_SHADOW_LOCK = threading.Lock()
class _GateSeq:
    """(п.5, R-09 + C5 ): строго растущий seq = max(последний + 1, мс) из состояния службы Σ
    (переживает перезапуск, перевод часов назад, ротацию журнала). Сбой состояния -> запасной счётчик, тоже строго
    больше последнего выданного."""

    def __init__(self) -> None:
        self._last = -1
        self._lock = threading.Lock()

    def __iter__(self) -> "_GateSeq":
        return self

    def __next__(self) -> int:
        try:
            s = sigma_gate.get_gate(shadow_path()).next_seq()
        except Exception:  # noqa: BLE001
            s = int(time.time() * 1000)
        with self._lock:
            s = max(s, self._last + 1)
            self._last = s
        return s


_SHADOW_SEQ = _GateSeq()
_SHADOW_WARNED: set[str] = set()


def shadow_enabled() -> bool:
    return os.getenv(SHADOW_FLAG_ENV, "").strip() == "1"


def _collector_dirs() -> list[Path]:
    out: list[Path] = []
    cfg = __import__("sys").modules.get("config")
    d = getattr(cfg, "COLLECTOR_DIR", None) if cfg is not None else None
    if d:
        try:
            out.append(Path(d).resolve())
        except Exception:  # noqa: BLE001
            pass
    return out


def _in_collector(p: Path) -> bool:
    try:
        rp = p.resolve()
    except Exception:  # noqa: BLE001
        rp = p
    if any(part.lower().startswith("oko_collector_") for part in rp.parts):
        return True
    for d in _collector_dirs():
        if rp == d or d in rp.parents:
            return True
    return False


def shadow_path() -> Path:
    """Путь журнала. Режим сбора его не перенаправляет (явный запрет, см. шапку раздела)."""
    raw = os.getenv(SHADOW_PATH_ENV, "").strip()
    p = Path(raw) if raw else SHADOW_DEFAULT_PATH
    if _in_collector(p):
        if "collector" not in _SHADOW_WARNED:
            _SHADOW_WARNED.add("collector")
            logger.warning("[SIGMA_SHADOW] путь журнала во временной папке режима сбора отвергнут -> %s",
                           SHADOW_DEFAULT_PATH.name)
        return SHADOW_DEFAULT_PATH
    return p


def _shadow_max() -> int:
    try:
        v = int(os.getenv(SHADOW_MAX_ENV, "") or SHADOW_MAX_BYTES)
    except ValueError:
        return SHADOW_MAX_BYTES
    return v if v > 0 else SHADOW_MAX_BYTES


def _num(v: Any) -> "int | float | None":
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)) and v == v:   # NaN -> None
        return v
    return None


def shadow_validate(row: Mapping[str, Any]) -> "dict[str, Any] | None":
    """Строка по белому списку или None. Лишнее поле, текст вместо кода, не-число в s_* -- строка отвергается целиком."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if v is None:
            continue
        if k not in SHADOW_FIELDS:
            return None
        if k in ("ts", "seq", "latency_ms"):
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                return None
        elif k in ("s_exp", "s_obs"):
            if _num(v) is None:
                return None
            v = _num(v)
        elif k == "s_raw":
            if _raw_num(v) is None or isinstance(v, str):
                return None
        elif k == "d_cmd":
            if isinstance(v, bool) or not isinstance(v, int):
                return None
        elif k in ("sigma", "sigma_p") and v not in SIGMA_OUTCOMES:
            return None
        elif k == "sigma_why" and not (isinstance(v, str) and _CODE_RE.match(v)):
            return None
        elif k in ("a_t", "sigma_live") and (isinstance(v, bool) or v not in (0, 1)):
            return None
        elif k == "step" and v not in SHADOW_STEPS:
            return None
        elif k == "outcome" and v not in SHADOW_OUTCOMES:
            return None
        elif k == "err" and v not in SHADOW_ERRS:
            return None
        elif k == "cmd_kind" and v not in SHADOW_KINDS:
            return None
        elif k in ("action", "feature") and not (isinstance(v, str) and _CODE_RE.match(v)):
            return None
        elif k in ("node_from", "node_to") and not (isinstance(v, str) and _NODE_RE.match(v)):
            return None
        out[k] = v
    if "step" not in out or "ts" not in out:
        return None
    return out


def _shadow_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = line.encode("utf-8")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    if size and size + len(data) > _shadow_max():
        os.replace(path, path.with_name(path.name + ".1"))
    with open(path, "ab") as fh:
        fh.write(data)


def shadow_write(**row: Any) -> None:
    """Одна строка в журнал. Никогда не бросает."""
    if not shadow_enabled():
        return
    try:
        row.setdefault("ts", int(time.time() * 1000))
        clean = shadow_validate(row)
        if clean is None:
            if "schema" not in _SHADOW_WARNED:
                _SHADOW_WARNED.add("schema")
                logger.warning("[SIGMA_SHADOW] строка отвергнута схемой (поля: %s)", ",".join(sorted(row)))
            return
        line = json.dumps(clean, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _SHADOW_LOCK:
            _shadow_append(shadow_path(), line)
    except Exception as exc:  # noqa: BLE001 - журнал не влияет на команду
        if "write" not in _SHADOW_WARNED:
            _SHADOW_WARNED.add("write")
            logger.warning("[SIGMA_SHADOW] запись не удалась: %s", type(exc).__name__)


def _raw_num(v: Any) -> "int | float | None":
    """сырое значение как есть -- int/float (конечное, bool -- нет); строка-число ТВ -> число без округления."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d{1,12}(\.\d{1,12})?\s*", v):
        t = v.strip()
        return float(t) if "." in t else int(t)
    return None


_STEP_SIGN = {"amb_brighter": 1, "amb_dimmer": -1, "volume_up": 1, "volume_down": -1}


def _d_cmd_of(command: Any) -> "int | None":
    """Δ_cmd из разбора команды: meta.delta (со знаком) или meta.step со знаком имени команды; иначе None."""
    try:
        meta = getattr(command, "meta", None)
        if not isinstance(meta, Mapping):
            return None
        d = meta.get("delta")
        if isinstance(d, int) and not isinstance(d, bool):
            return d
        sg = _STEP_SIGN.get(getattr(command, "name", None))
        st = meta.get("step")
        if sg and isinstance(st, int) and not isinstance(st, bool):
            return sg * abs(st)
    except Exception:  # noqa: BLE001
        pass
    return None


def _kind_of(command: Any) -> "str | None":
    k = getattr(command, "kind", None)
    return k if k in SHADOW_KINDS else None


def _action_of(command: Any) -> "str | None":
    n = getattr(command, "name", None)
    return n if isinstance(n, str) and _CODE_RE.match(n) else None


def shadow_event(step: str, *, err: "str | None" = None, command: Any = None) -> None:
    """Шаг без исполнения (parse_fail / cancel): только код, без текста и id."""
    if not shadow_enabled():
        return
    shadow_write(step=step, err=err, seq=next(_SHADOW_SEQ),
                 cmd_kind=_kind_of(command) if command is not None else None,
                 action=_action_of(command) if command is not None else None)


# Имя метода клиента ТВ -> имя признака API. Числа: s_exp -- из аргумента записи (индекс), s_obs -- из чтения.
_FEATURE = {
    "set_volume": "volume", "get_volume": "volume", "volume_step": "volume",
    "set_muted": "muted",
    "set_lounge_color": "lounge_brightness", "get_lounge_color": "lounge_brightness",
    "set_ambilight_power": "ambilight_power", "get_ambilight_power": "ambilight_power",
    "set_ambilight_style": "ambilight_style", "get_ambilight_config": "ambilight_style",
    "send_keys": "keys", "send_key": "keys",
    "get_current_activity": "activity",
    "set_powerstate": "power", "get_powerstate": "power",
}
_ARG_EXP = {"set_volume": 0, "set_muted": 0, "set_lounge_color": 2, "set_ambilight_power": 0,
            "set_setting_value": 1, "set_setting_enum": 1}


def _is_send(name: str) -> bool:
    return name.startswith(("set_", "send_")) or name == "volume_step"


def _is_read(name: str) -> bool:
    return name.startswith("get_")


def _feature(name: str, args: tuple) -> "str | None":
    if name in ("get_setting_value", "set_setting_value", "set_setting_enum") and args and _num(args[0]) is not None:
        return f"setting_{int(args[0])}"
    f = _FEATURE.get(name) or name.split("_", 1)[-1]
    return f if _CODE_RE.match(f) else None


# (16.11, 2c-fix): запись настройки меню ТВ (`set_setting_value` / `set_setting_enum`) сама читает значение
# после записи -- внутри клиента (`TVClient._write_setting` -> `read_settings`), мимо обёртки _ShadowTV. Поэтому у
# amb_step / amb_brighter (setting_710) в outcome было s_exp без s_obs. Точка замера: на время ОДНОГО вызова записи
# у экземпляра клиента подменяется `_write_setting` тонкой прокладкой, которая вызывает оригинал ровно один раз,
# возвращает его результат без изменений и только запоминает считанное поле (value / selected_item).
# Вызовы ТВ, их порядок, аргументы, исключения и ответ команды -- те же. Нет `_write_setting` у клиента -> замера нет.
_INNER_READBACK = {"set_setting_value": "value", "set_setting_enum": "selected_item"}


def _as_num(v: Any) -> "int | float | None":
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return _num(v)


def _hook_readback(tv: Any, key: str, box: dict) -> "Callable[[], None] | None":
    """Прокладка на `_write_setting` экземпляра; возвращает функцию снятия или None (замер не поставлен)."""
    try:
        d = getattr(tv, "__dict__", None)
        orig = getattr(tv, "_write_setting", None)
        if not isinstance(d, dict) or "_write_setting" in d or not callable(orig):
            return None

        def shim(*a: Any, **k: Any) -> Any:
            t = time.monotonic()
            out = orig(*a, **k)            # ровно один вызов; исключение уходит как раньше
            try:
                box["ms"] = int((time.monotonic() - t) * 1000)
                if isinstance(out, Mapping):
                    box["obs"] = _as_num(out.get(key))
                    box["raw"] = _raw_num(out.get(key))      # до _as_num
            except Exception:  # noqa: BLE001
                pass
            return out

        d["_write_setting"] = shim

        def undo() -> None:
            if d.get("_write_setting") is shim:
                del d["_write_setting"]
        return undo
    except Exception:  # noqa: BLE001
        return None


def _hook_setting_raw(tv: Any, box: dict) -> "Callable[[], None] | None":
    """прокладка на `_setting` экземпляра на время ОДНОГО get_setting_value -- запоминает сырое значение
    (data.value / data.selected_item) до int клиента. Вызов оригинала ровно один, результат и исключения -- те же."""
    try:
        d = getattr(tv, "__dict__", None)
        orig = getattr(tv, "_setting", None)
        if not isinstance(d, dict) or "_setting" in d or not callable(orig):
            return None

        def shim(*a: Any, **k: Any) -> Any:
            out = orig(*a, **k)            # ровно один вызов; исключение уходит как раньше
            try:
                data = out.get("data") if isinstance(out, Mapping) else None
                if isinstance(data, Mapping):
                    box["raw"] = _raw_num(data["value"] if "value" in data else data.get("selected_item"))
            except Exception:  # noqa: BLE001
                pass
            return out

        d["_setting"] = shim

        def undo() -> None:
            if d.get("_setting") is shim:
                del d["_setting"]
        return undo
    except Exception:  # noqa: BLE001
        return None


def _raw(name: str, res: Any) -> "int | float | None":
    """сырое значение того же чтения, что `_obs` (до приведения); get_setting_value -- из прокладки."""
    try:
        if not isinstance(res, Mapping):
            return None
        if name == "get_volume":
            return _raw_num(res.get("current"))
        if name == "get_lounge_color":
            c = res.get("color", res)
            return _raw_num(c.get("brightness")) if isinstance(c, Mapping) else None
    except Exception:  # noqa: BLE001
        pass
    return None


def _target_of(name: str, args: tuple) -> "int | float | None":
    """Что клиент реально пишет: для ползунка -- значение, обрезанное до [lo; hi] (как в TVClient)."""
    try:
        if name == "set_setting_value" and len(args) >= 4:
            return max(int(args[2]), min(int(args[3]), int(args[1])))
        return _num(args[1]) if len(args) > 1 else None
    except Exception:  # noqa: BLE001
        return None


def _obs(name: str, res: Any) -> "int | float | None":
    if name == "get_setting_value":
        return _num(res)
    if not isinstance(res, Mapping):
        return None
    if name == "get_volume":
        return _num(res.get("current"))
    if name == "get_lounge_color":
        c = res.get("color", res)
        return _num(c.get("brightness")) if isinstance(c, Mapping) else None
    if name == "get_ambilight_power":
        p = res.get("power")
        return 1 if p == "On" else 0 if p == "Off" else None
    return None


class _ShadowRun:
    """Счётчик одной команды: seq, моменты, последние числа. Не хранит ни текста, ни аргументов-строк."""

    def __init__(self, command: Any) -> None:
        self.seq = next(_SHADOW_SEQ)
        self.kind = _kind_of(command)
        self.action = _action_of(command)
        self.t0 = time.monotonic()
        self.s_exp: "int | float | None" = None     # ожидаемое: последнее записанное число (признак feat)
        self.s_obs: "int | float | None" = None     # считанное ПОСЛЕ записи того же признака (или чтение без записи)
        self.feat: "str | None" = None
        self.outcome: "str | None" = None
        self.err: "str | None" = None
        self.mismatch = False                       # чтение после записи != записанному (запись не принята)
        self.d_cmd = _d_cmd_of(command)             # Δ_cmd (STEP) из разбора команды
        self.s_raw: "int | float | None" = None     # сырое значение того же чтения, что дало s_obs
        self.verdict: Any = None                    # вердикт Σ перед отправкой (sigma_gate.Verdict)
        self.sent = False                           # была запись в ТВ (метрика gate A: SENT)

    def row(self, step: str, **kw: Any) -> None:
        shadow_write(step=step, seq=self.seq, cmd_kind=self.kind, action=self.action, **kw)

    def call(self, name: str, fn: Callable[..., Any], args: tuple, kwargs: dict, tv: Any = None) -> Any:
        send, read = _is_send(name), _is_read(name)
        feat = exp = None
        box: dict = {}
        try:
            feat = _feature(name, args)
            if send:
                self.sent = True
                i = _ARG_EXP.get(name)
                if name in ("send_keys", "send_key"):
                    exp = len(args[0]) if name == "send_keys" and args and isinstance(args[0], (list, tuple)) else 1
                elif i is not None and len(args) > i:
                    exp = _num(args[i])
                self.row("send", feature=feat, s_exp=exp)
        except Exception:  # noqa: BLE001
            pass
        undo = None
        try:
            if tv is not None and name in _INNER_READBACK:
                undo = _hook_readback(tv, _INNER_READBACK[name], box)
            elif tv is not None and name == "get_setting_value":
                undo = _hook_setting_raw(tv, box)      # сырое значение до int
        except Exception:  # noqa: BLE001 - сбой замера не влияет на команду
            undo = None
        t = time.monotonic()
        try:
            res = fn(*args, **kwargs)      # ровно один вызов; исключение уходит как раньше
        except BaseException:
            self._inner(name, args, feat, box, failed=True)
            raise
        finally:
            if undo is not None:
                try:
                    undo()
                except Exception:  # noqa: BLE001
                    pass
        try:
            ms = int((time.monotonic() - t) * 1000)
            if send:
                got = _num(res) if name in ("set_volume", "volume_step", "set_setting_value") else None
                val = got if got is not None else (exp if name not in ("send_keys", "send_key") else None)
                if val is not None:
                    self.s_exp, self.feat, self.s_obs = val, feat, None   # чтения до записи -- не наблюдение итога
                    self.s_raw = None
                self.row("ack", feature=feat, s_exp=got, latency_ms=ms)
                self._inner(name, args, feat, box, failed=False)
            elif read:
                obs = _obs(name, res)
                raw = box.get("raw") if name == "get_setting_value" else _raw(name, res)
                if obs is not None and (self.s_exp is None or feat == self.feat):
                    self.s_obs = obs
                    self.s_raw = raw
                    if self.s_exp is None:
                        self.feat = feat
                self.row("readback", **{"feature": feat, "s_obs": obs, "s_raw": raw, "latency_ms": ms})
        except Exception:  # noqa: BLE001
            pass
        return res

    def _inner(self, name: str, args: tuple, feat: "str | None", box: dict, *, failed: bool) -> None:
        """чтение после записи внутри клиента -> s_obs того же признака (+ строка readback). Никогда не бросает."""
        try:
            obs = box.get("obs")
            if obs is None:
                return
            if failed or self.s_exp is None or self.feat != feat:
                self.s_exp, self.feat = _target_of(name, args), feat
            self.s_obs = obs
            self.s_raw = box.get("raw")
            if failed and self.s_exp is not None and obs != self.s_exp:
                self.mismatch = True
            self.row("readback", **{"feature": feat, "s_obs": obs, "s_raw": box.get("raw"), "latency_ms": box.get("ms")})
        except Exception:  # noqa: BLE001
            pass

    def classify(self, command: Any, answer: Any) -> None:
        """Итог сверки по маркерам ответа модуля (сам текст не пишется)."""
        try:
            if self.kind == BLIND:
                self.outcome = "WARN"            # слепая команда: ТВ не подтверждает -- сверки нет
                return
            a = answer if isinstance(answer, str) else ""
            if "телевизор сообщает" in a or "отправлено " in a:
                self.outcome, self.err = "FAIL", "verify_fail"
            elif "предел" in a or "просили" in a or "запрошено" in a:
                self.outcome, self.err = "WARN", "warn"
            else:
                self.outcome = "PASS"
        except Exception:  # noqa: BLE001
            pass

    def failed(self, err: "str | None") -> None:
        if err is None and self.mismatch:
            err = "verify_fail"           # ТВ не принял запись -- это расхождение сверки, а не «прочее»
        self.outcome, self.err = "FAIL", err
        if err == "net":
            self.row("error", err="net")

    def finish(self, command: Any) -> None:
        try:
            meta = getattr(command, "meta", None) or {}
            target = meta.get("target") if isinstance(meta, Mapping) else None
            if isinstance(target, str) and self.outcome is not None and self.outcome != "FAIL":
                self.row("menu_step", node_from="home", node_to=target)   # маршрут карты меню всегда от Home
            self.row("outcome", outcome=self.outcome or "FAIL", err=self.err, feature=self.feat,
                     **{"s_exp": self.s_exp, "s_obs": self.s_obs, "s_raw": self.s_raw, "d_cmd": self.d_cmd},
                     **_verdict_fields(self.verdict),
                     latency_ms=int((time.monotonic() - self.t0) * 1000))
        except Exception:  # noqa: BLE001
            pass


class _ShadowTV:
    """Обёртка клиента ТВ: те же атрибуты и вызовы, плюс отметки в журнал. Ничего не добавляет и не пропускает."""

    __slots__ = ("_tv", "_run")

    def __init__(self, tv: Any, run: _ShadowRun) -> None:
        object.__setattr__(self, "_tv", tv)
        object.__setattr__(self, "_run", run)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_tv"), name)
        if not callable(attr) or not (_is_send(name) or _is_read(name)):
            return attr
        run = object.__getattribute__(self, "_run")

        tv = object.__getattribute__(self, "_tv")

        def traced(*args: Any, **kwargs: Any) -> Any:
            return run.call(name, attr, args, kwargs, tv)
        return traced

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_tv"), name, value)


def _verdict_fields(v: Any) -> dict:
    """поля вердикта Σ для строки журнала (только коды; нет вердикта -> полей нет)."""
    if v is None:
        return {}
    try:
        return {"sigma": v.outcome, "sigma_p": v.proposal, "sigma_why": v.why, "a_t": 1 if v.alarm else 0,
                "sigma_live": 1 if v.live else 0}
    except Exception:  # noqa: BLE001
        return {}


def _v_set(command: Any) -> "int | float | None":
    """цель SET из разбора команды (meta: value / level / brightness), если есть; иначе None."""
    meta = getattr(command, "meta", None)
    if isinstance(meta, Mapping):
        for k in ("value", "level", "brightness"):
            v = meta.get(k)
            if _num(v) is not None and not isinstance(v, bool):
                return v
    return None


def _sigma_before(command: Command, intent_src: str) -> Any:
    """вердикт Σ перед отправкой. Никогда не бросает (сбой шлюза -> None: в теневом режиме команда идёт
    по-старому; в боевом -- HALT, см. _execute)."""
    try:
        return sigma_gate.get_gate(shadow_path()).before(
            _action_of(command), _kind_of(command), _d_cmd_of(command), _v_set(command), intent_src)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SIGMA_GATE] вердикт не посчитан: %s", type(exc).__name__)
        return None


def _sigma_after(run: "_ShadowRun") -> None:
    try:
        sigma_gate.get_gate(shadow_path()).after(
            run.seq, action=run.action, kind=run.kind, outcome=run.outcome, err=run.err, feature=run.feat,
            expected=run.s_exp, observed=run.s_obs, d_cmd=run.d_cmd, sent=run.sent)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SIGMA_GATE] исход не учтён: %s", type(exc).__name__)


SIGMA_HALT_TEXT = "⏸ Σ: команда не отправлена ({why}). Ничего не изменено."
SIGMA_PROBE_TEXT = ("⏸ Σ: тревога рассинхрона — команда не отправлена, выполнено только контрольное чтение "
                    "({reads}, удачных {ok}). {tail}")


def _sigma_live_block(command: Command, tv_factory: Callable[[], Any], verdict: Any) -> "str | None":
    """«шлюз Σ», боевой режим (OKO_SIGMA_LIVE=1): A_t = 1 -> Active Probing (только чтение, ≤ R_t); иной вердикт, кроме
    EXECUTE, -> текст без отправки. None -> команду исполнять. Любой сбой -> HALT (не отправлять)."""
    try:
        if verdict is None:
            shadow_write(step="sigma", seq=next(_SHADOW_SEQ), action=_action_of(command), sigma="HALT",
                         sigma_why="KERNEL_ERROR", sigma_live=1)
            return SIGMA_HALT_TEXT.format(why="ошибка ядра Σ")
        if verdict.alarm:
            gate = sigma_gate.get_gate(shadow_path())
            tv = tv_factory()
            res = gate.probe(_ProbeTV(tv), verdict.feature, lambda: next(_SHADOW_SEQ))
            shadow_write(step="sigma", seq=next(_SHADOW_SEQ), action=_action_of(command), feature=res.get("feature"),
                         **_verdict_fields(verdict))
            tail = ("Тревога снята — повтори команду." if res.get("cleared")
                    else "Тревога держится — команду пока не выполняю.")
            return SIGMA_PROBE_TEXT.format(reads=res.get("reads", 0), ok=res.get("ok", 0), tail=tail)
        if not verdict.execute:
            shadow_write(step="sigma", seq=next(_SHADOW_SEQ), action=_action_of(command), **_verdict_fields(verdict))
            return SIGMA_HALT_TEXT.format(why=verdict.why or verdict.outcome)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SIGMA_GATE] боевой шлюз: %s", type(exc).__name__)
        return SIGMA_HALT_TEXT.format(why="ошибка шлюза Σ")


class _ProbeTV:
    """клиент ТВ для Active Probing -- только get_*; любая запись -> исключение (не уходит в ТВ)."""

    __slots__ = ("_tv",)

    def __init__(self, tv: Any) -> None:
        object.__setattr__(self, "_tv", tv)

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("get_"):
            raise PermissionError("Active Probing: только чтение")
        return getattr(object.__getattribute__(self, "_tv"), name)


_INTENT = threading.local()   # «rule» / «model» для текущего вызова handle (полигон подменяет _execute(command, factory))


def _execute(command: Command, tv_factory: Callable[[], Any]) -> str:
    """тонкая обёртка. Журнал выключен -> прежний путь без изменений; включён -> тот же путь + отметки.
    шлюз Σ перед отправкой. Теневой режим (OKO_SIGMA_LIVE != 1, по умолчанию): вердикт пишется в строку
    outcome, команда -- по-старому (журнал выключен -> шлюз не вызывается вовсе). Боевой (OKO_SIGMA_LIVE=1): не
    EXECUTE -> не отправлять; A_t = 1 -> Active Probing."""
    live = sigma_gate.live_enabled() if sigma_gate is not None else False
    if command.kind == tv_nav.LOCAL_KIND or (not shadow_enabled() and not live):
        return _execute_core(command, tv_factory, None)
    verdict = _sigma_before(command, getattr(_INTENT, "src", "rule") or "rule")
    if live:
        blocked = _sigma_live_block(command, tv_factory, verdict)
        if blocked is not None:
            return blocked
    if not shadow_enabled():
        return _execute_core(command, tv_factory, None)
    try:
        run = _ShadowRun(command)
        run.verdict = verdict
    except Exception:  # noqa: BLE001
        return _execute_core(command, tv_factory, None)
    answer = _execute_core(command, tv_factory, run)
    run.finish(command)
    _sigma_after(run)
    return answer


def _execute_core(command: Command, tv_factory: Callable[[], Any], run: "_ShadowRun | None") -> str:
    """Ни одно исключение не уходит в бот: и фабрика клиента, и команда под одним перехватом.
    ValueError различается по этапу: при создании клиента — настройка (TV_HOST), при команде — неверное значение."""
    if command.kind == tv_nav.LOCAL_KIND:   # 05.11: уточнение меню / память макросов — телевизор не нужен
        return command.run(None)
    tv = None
    pre_note = music_note = ""
    try:
        tv = tv_factory()
        pre_note = _pre(command.name)
        music_note = _stop_music(command.name)
        answer = command.run(tv if run is None else _ShadowTV(tv, run))
        if run is not None:
            run.classify(command, answer)
    except TVNotPaired as exc:
        logger.warning("[TV_BOT] нет пары: %s", exc)
        if run is not None:
            run.failed(None)
        return "📺 Бот ещё не связан с телевизором — нужен разовый пайринг на ПК (tv_pair.py)."
    except TVAuthError as exc:
        logger.warning("[TV_BOT] пара отклонена: %s", exc)
        if run is not None:
            run.failed(None)
        return "📺 Телевизор отклонил связь с ботом — нужен повторный пайринг на ПК (tv_pair.py)."
    except TVUnsupported as exc:
        logger.info("[TV_BOT] не поддерживается: %s", exc)
        if run is not None:
            run.failed(None)
        return f"📺 Этот телевизор такое не умеет ({describe_command(command)})."
    except TVTransportError as exc:
        logger.warning("[TV_BOT] сеть cmd=%s: %s", command.name, exc)
        if run is not None:
            run.failed("net")
        host = getattr(tv, "host", None)
        host = host if isinstance(host, str) else None
        return transport_text(exc, host) + network_hint(exc, host)
    except TVError as exc:
        logger.warning("[TV_BOT] ошибка телевизора cmd=%s: %s", command.name, exc)
        if run is not None:
            run.failed(None)
        return "📺 Телевизор ответил ошибкой — команда не выполнена. Повтори; если повторится — сообщи администратору."
    except ValueError as exc:
        if run is not None:
            run.failed(None)
        if tv is None:                  # TVClient.from_env: TV_HOST не задан — настройка, а не сбой
            logger.warning("[TV_BOT] не настроен: %s", exc)
            return "📺 Телевизор не настроен: не задан его адрес (TV_HOST в .env на ПК)."
        return f"📺 Команда не выполнена: {exc}."   # значение вне допустимого; тексты tv_control русские
    except Exception as exc:  # noqa: BLE001 - в бот не должно утечь исключение
        logger.exception("[TV_BOT] непредвиденная ошибка cmd=%s", command.name)
        if run is not None:
            run.failed(None)
        return f"📺 Непредвиденная ошибка бота ({type(exc).__name__}) — подробности в журнале."

    moved = getattr(tv, "moved_from", None)
    note = (f"\n📺 Телевизор сменил адрес в сети ({moved} → {tv.host}) — нашёл его по MAC и запомнил."
            if isinstance(moved, str) and moved else "")
    if pre_note:
        note += "\n" + pre_note
    if music_note:
        note += "\n" + music_note
    check = VERIFY.get(command.name)
    if check is not None:
        try:
            note += "\n" + check()
        except Exception as exc:  # noqa: BLE001 - проверка не должна ломать ответ о команде
            logger.warning("[TV_BOT] проверка по кадру cmd=%s: %s", command.name, type(exc).__name__)
            note += f"\n📷 Проверить по кадру камеры не удалось ({type(exc).__name__})."
    if command.kind == BLIND:
        return f"{answer} {PREFIX_NOTE_BLIND}{note}"
    return f"{answer}{note}"


def make_factory(creds: Mapping[str, str] | None = None) -> Callable[[], Any]:
    """Фабрика клиента для бота. Импорт отложен, чтобы модуль грузился без сети."""
    from tv_control import TVClient

    def factory() -> Any:
        if creds is None:
            return TVClient.from_env()
        return TVClient(**dict(creds))

    return factory
