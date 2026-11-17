"""BIND_LAYER (R2c, 11.11, решение 13:1x вариант 1): связки Оператора поверх штатных правил.

Слой 1 (инструменты) — router.route / tv_commands / tv_bot_handler.handle: исполнение и обратная связь. Не трогается.
Слой 2 (динамика Оператора) — память src/oko.memory.BindMemory: фраза -> готовая ШТАТНАЯ команда.
Связка только подставляет текст команды; исполняет его тот же handle() со всеми подтверждениями (standby не обходится).

Где связка применяется (apply_route): правила вернули модель (BRAIN) или уточнение (clarify:* / lamps:which).
Никогда не перекрывает: запрет («не трогай …»), «запомни …», «резонанс ядра», лампы потолка, вопрос («?»), самоотмену.
Обучение (решение 23:15 п.5, 10:45): только «📌 Запомнить связку» после исполнения с результатом VERIFIED
(PASS и не слепая кнопка) или «запомни: X — Y». Выбор варианта в «❓ Уточни» связку не создаёт.
Тексты — безличные, без советов (роль Ока; role_guard).
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import layout_fix
import router
import tv_bot_handler
import tv_commands

logger = logging.getLogger("oko.bind")

MIN_KEY_CHARS = 3
MAX_X_CHARS = 60
OPTIONS_TTL_S = 90.0
_KEPT_OPTIONS = 64
APPLY_REASONS = ("clarify:", "lamps:which")       # уточнения, которые связка может снять
STOP_WORDS = ("ну", "давай", "сделай", "око", "пожалуйста", "плиз", "please", "nu", "davaj", "sdelaj", "oko",
              "pozhalujsta", "pozhalujsta", "ka")
RESERVED = (set(tv_bot_handler.CONFIRM_WORDS) | set(tv_bot_handler.HELP_WORDS)
            | {"стоп", "отмена", "отмени", "хватит", "статус", "нет", "не надо", "stop", "otmena", "hvatit", "status"})
SELF_CANCEL_RE = re.compile(r"(?<![a-zа-я])(нет|отмена|отмени|не надо|otmena|net)(?![a-zа-я])")
BIND_RE = re.compile(
    r"^((око|oko|ну|nu|пожалуйста|давай|davaj)[\s,]+)*"
    r"(запомни|запиши|зафиксируй|занеси|внеси|сохрани|zapomni|zapishi|zafiksiruj|sohrani|remember)"
    r"\s*[:,]?\s*(?P<x>.+?)\s+(—|–|-|=|->|→|это|eto)\s+(?P<y>.+?)\s*\.?$", re.I)

# R2c fix (13:42, живой ввод «…подсветка красная», затем «врубай красоту»): правая часть -- ОДНА чистая команда.
# Правила узнают команду в начале фразы и молча отбрасывают хвост -> без этой проверки в связку уходит мусор.
JUNK_RE = re.compile(r"[«»\"“”„,;]|(?<![а-яa-z])(затем|потом|после|и|а также|zatem|potom|i)(?![а-яa-z])")
ACK_STATUS = "ACKNOWLEDGED"
_NOTE_RE = re.compile(r"^(Понял как «[^»]*»( \(связка\))?\.\s*)+")

T_SAVED = "Связка сохранена: «{x}» → «{y}»."
T_FORMAT = router.MEM_TEXT
T_BAD_KEY = "Не сохранено: «{x}» — слишком коротко или служебное слово."
T_TAKEN = "Не сохранено: «{x}» уже занято базовой командой."
T_NOT_CMD = "Не сохранено: «{y}» — не команда."
T_NOT_ONE = "Не сохранено: «{y}» — нужна одна команда без лишних слов."
T_CONFIRM_CMD = "Не сохранено: «{y}» — команда с подтверждением, связкой не закрепляется."
T_NOT_DONE = "Не сохранено: команда не выполнена."
T_MEM_DOWN = "Не сохранено: память связок недоступна."
T_TOO_LONG = "Не сохранено: фраза длиннее {n} знаков."
T_APPLIED = "Понял как «{y}» (связка). "
T_REJECTED = "Связка «{x}» → «{y}» отключена: автоматически больше не применяется."
T_KEPT = "Не сохранено: таблица связок не менялась."
T_EXPIRED = "Не выполнялось: вопрос устарел."
B_REMEMBER = "📌 Запомнить связку"
B_REJECT = "✖ Не то"
B_REPLACE = "Заменить"
B_KEEP = "Оставить"


@dataclass
class Reply:
    """Ответ модуля: текст + кнопки [(подпись, callback_data)] по рядам. Без кнопок — как строка."""
    text: str
    buttons: Optional[list] = None


# ---- ключ фразы -------------------------------------------------------------------------
_STOP_KEYS = None


def _stop_keys() -> set:
    global _STOP_KEYS
    if _STOP_KEYS is None:
        _STOP_KEYS = {layout_fix.phonetic_key(w) for w in STOP_WORDS}
    return _STOP_KEYS


def phrase_key(text: str) -> Optional[str]:
    """Фонетический ключ фразы (ТЗ v2 §3): латиница = кириллица, без стоп-слов, целиком. None — не годится."""
    raw = (text or "").strip().lower()
    if not raw or "?" in raw:
        return None
    if raw in RESERVED or tv_commands.normalize(raw) in RESERVED:
        return None
    words = [w for w in re.findall(r"[a-zа-яё0-9]+", raw) if w not in STOP_WORDS]   # F13: стоп-слова по исходному слову
    words = [re.sub(r"w", "sh", w) if re.fullmatch(r"[a-z0-9]+", w) else w for w in words]   # F14: w = ш у Оператора
    key = layout_fix.phonetic_key(" ".join(words))
    if len(re.sub(r"[^а-я0-9]", "", key)) < MIN_KEY_CHARS:
        return None
    if key in {layout_fix.phonetic_key(w) for w in RESERVED}:
        return None
    return key


def can_apply(r: "router.Route", raw: str) -> bool:
    """Может ли связка снять этот исход правил (а не перекрыть запрет / вопрос / честный отказ)."""
    if "?" in (raw or ""):
        return False
    t = tv_commands.normalize(raw or "")
    if SELF_CANCEL_RE.search(t) or router.NEG_ACT_RE.search(t):
        return False
    if r.kind == router.BRAIN:
        return True
    return r.kind == router.CLARIFY and any(r.reason.startswith(p) for p in APPLY_REASONS)


# ---- исход исполнения ---------------------------------------------------------------------
def exec_status(reply: str, outcome: Optional[str]) -> Optional[str]:
    """Исход штатного контура -> статус памяти. VERIFIED — только PASS проверяемой команды."""
    if outcome is None:
        return None
    if outcome == "FAIL":
        return "FAILED"
    if outcome == "WARN":
        return "MISMATCH"
    body = _NOTE_RE.sub("", (reply or "").strip())
    if tv_bot_handler.PREFIX_NOTE_BLIND in (reply or "") or body.startswith("Отправил"):
        return ACK_STATUS                  # слепая кнопка: телевизор отвечает 200 на любое нажатие
    return "VERIFIED"


def tv_command(text: str) -> Optional[str]:
    """Каноническая штатная команда ТВ для текста или None."""
    try:
        r = router.route(text, text, None)
    except Exception:                      # noqa: BLE001
        return None
    return r.text if r.kind == router.TV else None


def _cmd_name(text: str) -> Optional[str]:
    try:
        return tv_commands.parse(text).name
    except tv_commands.Unknown:
        return None


def one_command(y: str) -> bool:
    """F06 (Fable 11.11): правая часть — ровно одна команда. Любое начало фразы, которое уже разбирается в
    действие, значит хвост лишний (та же команда) или вторая команда; запрос состояния (*_get) — не действие."""
    toks = tv_commands.normalize(y).split()
    if "не" in toks or "ne" in toks:
        return False
    for k in range(1, len(toks)):
        name = _cmd_name(" ".join(toks[:k]))
        if name is not None and not name.endswith("_get"):
            return False
    return True


def needs_confirm(command: str) -> bool:
    try:
        return bool(tv_commands.parse(command).needs_confirm)
    except tv_commands.Unknown:
        return False


# ---- слой -------------------------------------------------------------------------------
class BindLayer:
    def __init__(self, memory_factory: Callable[[], object]) -> None:
        self._factory = memory_factory
        self._mem = None
        self._lock = threading.Lock()
        self._options: dict = {}          # id -> (operator, key, [команды], создано)

    @property
    def mem(self):
        with self._lock:
            if self._mem is None:
                self._mem = self._factory()
            return self._mem

    # -- применение -------------------------------------------------------------------
    def apply_route(self, user_id: int, r: "router.Route", raw: str) -> Optional["router.Route"]:
        """Route(TV) по рабочей связке или None (правила остаются как есть)."""
        if not can_apply(r, raw):
            return None
        key = phrase_key(raw)
        cmd = self.mem.lookup(str(user_id), key) if key else None
        if not cmd:
            return None
        if JUNK_RE.search(cmd.lower()) or not one_command(cmd):   # связка с хвостом (до 13:42 / F06) -- не применяется
            logger.info("[BIND] user=%s skipped: stored command not clean", user_id)
            return None
        return router.Route(router.TV, cmd, note=T_APPLIED.format(y=cmd), reason="bind")

    def after_tv(self, user_id: int, raw: str, r: "router.Route", answer: str, outcome: Optional[str]):
        """Исход штатного исполнения -> память; кнопки к ответу или None."""
        status = exec_status(answer, outcome)
        key = phrase_key(raw)
        if status is None or key is None:
            return None
        cid = self.mem.executed(str(user_id), key, r.text, status, phrase=_shown(raw))
        if r.reason == "bind":
            return [[(B_REJECT, f"bnd:x:{cid}")]]
        return None

    # -- «❓ Уточни» кнопками -----------------------------------------------------------
    def clarify_buttons(self, user_id: int, r: "router.Route", raw: str) -> Optional[list]:
        if r.kind != router.CLARIFY or not any(r.reason.startswith(p) for p in APPLY_REASONS):
            return None
        cmds = []
        for opt in re.findall(r"«([^»]+)»", r.text or ""):
            if "…" in opt or "..." in opt:
                continue
            cmd = tv_command(opt)
            if cmd and cmd not in cmds:
                cmds.append(cmd)
        if not cmds:
            return None
        oid = uuid.uuid4().hex[:12]
        with self._lock:
            self._sweep()
            self._options[oid] = (str(user_id), phrase_key(raw), cmds, time.monotonic(), _shown(raw))
            while len(self._options) > _KEPT_OPTIONS:
                self._options.pop(next(iter(self._options)))
        return [[(f"▶ {c}", f"bnd:c:{oid}:{i}")] for i, c in enumerate(cmds)]

    def take_option(self, user_id: int, oid: str, i: int) -> Optional[tuple]:
        """(key, команда) выбранного варианта или None (устарел / чужой)."""
        with self._lock:
            self._sweep()
            item = self._options.get(oid)
            if not item or item[0] != str(user_id) or not 0 <= i < len(item[2]):
                return None
            self._options.pop(oid, None)              # F07: повтор доставки нажатия не исполняет второй раз
            return item[1], item[2][i], item[4]

    def option_reply(self, user_id: int, key: Optional[str], cmd: str, answer: str, outcome: Optional[str],
                     phrase: str = ""):
        """После исполнения варианта: кнопка «Запомнить связку» только при VERIFIED и годном ключе."""
        status = exec_status(answer, outcome)
        if status is None or key is None:
            return None
        cid = self.mem.executed(str(user_id), key, cmd, status, phrase=phrase)
        return [[(B_REMEMBER, f"bnd:r:{cid}")]] if status == "VERIFIED" else None

    def _sweep(self) -> None:
        now = time.monotonic()
        for oid in [o for o, v in self._options.items() if now - v[3] > OPTIONS_TTL_S]:
            self._options.pop(oid, None)

    # -- «запомни: X — Y» --------------------------------------------------------------
    def parse_bind(self, raw: str) -> tuple:
        """-> ("error", текст) | ("run", x, key, команда)."""
        m = BIND_RE.match((raw or "").strip())
        if not m:
            return ("error", T_FORMAT)
        x, y = m.group("x").strip(" «»\"'"), m.group("y").strip(" «»\"'.")
        x = re.sub(r"^(что|чтобы|chto)\s+", "", x, flags=re.I).strip()   # F17: «запомни что X это Y»
        if len(x) > MAX_X_CHARS:                                         # F18
            return ("error", T_TOO_LONG.format(n=MAX_X_CHARS))
        key = phrase_key(x)
        if key is None or SELF_CANCEL_RE.search(tv_commands.normalize(x)):
            return ("error", T_BAD_KEY.format(x=x))
        try:
            rx = router.route(x, x, None)
        except Exception:                  # noqa: BLE001
            rx = router.Route(router.BRAIN)
        if not can_apply(rx, x):
            return ("error", T_TAKEN.format(x=x))
        if JUNK_RE.search(x.lower()):
            return ("error", T_BAD_KEY.format(x=x))
        if JUNK_RE.search(y.lower()):
            return ("error", T_NOT_ONE.format(y=y))
        cmd = tv_command(y)
        if not cmd:
            return ("error", T_NOT_CMD.format(y=y))
        if JUNK_RE.search(cmd.lower()) or not one_command(cmd):
            return ("error", T_NOT_ONE.format(y=y))
        if needs_confirm(cmd):
            return ("error", T_CONFIRM_CMD.format(y=y))
        return ("run", x, key, cmd)

    def bind_after_run(self, user_id: int, x: str, key: str, cmd: str, answer: str, outcome: Optional[str]) -> Reply:
        status = exec_status(answer, outcome)
        if status is None:
            return Reply(f"{answer}\n{T_NOT_DONE}")
        cid = self.mem.executed(str(user_id), key, cmd, status, phrase=x)
        return self._remember_reply(cid, answer, x, user_id)

    # -- кнопки ------------------------------------------------------------------------
    def remember(self, command_id: str, prefix: str = "", x: Optional[str] = None,
                 user_id: Optional[int] = None) -> Reply:
        return self._remember_reply(command_id, prefix, x, user_id)

    def _remember_reply(self, command_id: str, prefix: str, x: Optional[str], user_id: Optional[int] = None) -> Reply:
        try:
            out = self.mem.remember(command_id, None if user_id is None else str(user_id))
        except (KeyError, ValueError):
            return Reply(_join(prefix, T_EXPIRED))
        if out.status in ("REMEMBERED", "ALREADY"):
            return Reply(_join(prefix, T_SAVED.format(x=x or out.phrase, y=out.command)))
        if out.status == "EVICT":
            rid = out.request["request_id"]
            return Reply(_join(prefix, out.request["text"]),
                         [[(B_REPLACE, f"bnd:e:{rid}"), (B_KEEP, f"bnd:k:{rid}")]])
        return Reply(_join(prefix, out.request["text"] if out.request else T_NOT_DONE))

    def answer_evict(self, request_id: str, replace: bool, user_id: Optional[int] = None) -> Reply:
        op = None if user_id is None else str(user_id)
        try:
            out = self.mem.resolve(request_id, op) if replace else self.mem.cancel(request_id, op)
        except KeyError:
            return Reply(T_EXPIRED)
        if out.status in ("REMEMBERED", "ALREADY"):
            return Reply(T_SAVED.format(x=out.phrase, y=out.command))
        if out.status == "EVICT":                     # F11: таблица снова полна -- новый вопрос с кнопками
            rid = out.request["request_id"]
            return Reply(out.request["text"], [[(B_REPLACE, f"bnd:e:{rid}"), (B_KEEP, f"bnd:k:{rid}")]])
        if out.status == "CANCELLED":
            return Reply(T_KEPT)
        return Reply(T_EXPIRED)

    def reject(self, command_id: str, user_id: Optional[int] = None) -> Reply:
        try:
            out = self.mem.reject(command_id, None if user_id is None else str(user_id))
        except KeyError:
            return Reply(T_EXPIRED)
        if user_id is not None and self.mem.lookup(str(user_id), out.key) == out.command:
            return Reply(T_NOT_DONE)                  # F03: «отключена» -- только если связка правда снята
        return Reply(T_REJECTED.format(x=out.phrase, y=out.command))


def _shown(raw: str) -> str:
    """Фраза для текстов Оператору: как сказана, без хвостовых знаков, не длиннее 60 символов."""
    return re.sub(r"\s+", " ", (raw or "").strip(" .!,")).strip()[:60]


def _join(prefix: str, text: str) -> str:
    return f"{prefix}\n{text}" if prefix else text


def default_memory():
    """g6.db рядом с memory.db (data/memory/g6.db); тесты подменяют MEMORY_DB_PATH (conftest)."""
    import config
    from oko.memory import BindMemory
    from oko.registry import open_db
    path = Path(config.MEMORY_DB_PATH).parent / "g6.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("[BIND] memory=%s", path.name)
    return BindMemory(open_db(str(path), check_same_thread=False))


LAYER = BindLayer(default_memory)
