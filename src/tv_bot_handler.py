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

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

import menu_syntax   # 05.11 MENU_SYNTAX: имена пунктов меню ТВ -> маршрут по карте
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
) -> str:
    """Текст команды -> текст ответа. Сеть только внутри `tv_factory`.

    `tv_factory` вызывается ТОЛЬКО когда есть что выполнять: разбор, отказ по
    правам и запрос подтверждения не должны создавать соединение.
    """
    store = DEFAULT_PENDING if pending is None else pending
    ids = frozenset(int(x) for x in admin_ids) if admin_ids is not None else _default_admin_ids()
    moment = time.monotonic() if now is None else now

    if user_id not in ids:
        logger.warning("[TV_BOT] отказ по правам user=%s", user_id)
        return "Управление телевизором доступно только администратору."

    payload = tv_nav.canonical(strip_prefix(text), extra=menu_syntax.vocabulary())   # 28.10: «ok», «vpravo», «dybp» -> слова словаря

    # --- ветка подтверждения ------------------------------------------------
    if store.pending_for(user_id):
        command, expired = store.take(user_id, moment)
        if expired:
            prefix = "Предыдущий запрос подтверждения истёк. "
            if not payload:
                return prefix + "Повторите команду."
            # Текст разбирается как новая команда: ожидание уже недействительно.
        elif payload.lower() in CONFIRM_WORDS:
            assert command is not None
            logger.info("[TV_BOT] подтверждено user=%s cmd=%s", user_id, command.name)
            return _execute(command, tv_factory)
        else:
            # Любой другой текст отменяет ожидание и НЕ выполняется: при
            # висящем подтверждении опасной команды двусмысленность решается
            # в пользу отказа, а не догадки о намерении.
            logger.info("[TV_BOT] отменено user=%s cmd=%s",
                        user_id, command.name if command else "?")
            return "Отменено. Повторите команду, если она ещё нужна."
    else:
        prefix = ""

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
    return prefix + _execute(command, tv_factory)


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


def _execute(command: Command, tv_factory: Callable[[], Any]) -> str:
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
        answer = command.run(tv)
    except TVNotPaired as exc:
        logger.warning("[TV_BOT] нет пары: %s", exc)
        return "📺 Бот ещё не связан с телевизором — нужен разовый пайринг на ПК (tv_pair.py)."
    except TVAuthError as exc:
        logger.warning("[TV_BOT] пара отклонена: %s", exc)
        return "📺 Телевизор отклонил связь с ботом — нужен повторный пайринг на ПК (tv_pair.py)."
    except TVUnsupported as exc:
        logger.info("[TV_BOT] не поддерживается: %s", exc)
        return f"📺 Этот телевизор такое не умеет ({describe_command(command)})."
    except TVTransportError as exc:
        logger.warning("[TV_BOT] сеть cmd=%s: %s", command.name, exc)
        host = getattr(tv, "host", None)
        host = host if isinstance(host, str) else None
        return transport_text(exc, host) + network_hint(exc, host)
    except TVError as exc:
        logger.warning("[TV_BOT] ошибка телевизора cmd=%s: %s", command.name, exc)
        return "📺 Телевизор ответил ошибкой — команда не выполнена. Повтори; если повторится — сообщи администратору."
    except ValueError as exc:
        if tv is None:                  # TVClient.from_env: TV_HOST не задан — настройка, а не сбой
            logger.warning("[TV_BOT] не настроен: %s", exc)
            return "📺 Телевизор не настроен: не задан его адрес (TV_HOST в .env на ПК)."
        return f"📺 Команда не выполнена: {exc}."   # значение вне допустимого; тексты tv_control русские
    except Exception as exc:  # noqa: BLE001 - в бот не должно утечь исключение
        logger.exception("[TV_BOT] непредвиденная ошибка cmd=%s", command.name)
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
