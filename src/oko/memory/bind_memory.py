"""BindMemory: память связок «фраза Оператора -> штатная команда» (R2c, решение 2026-11-11 13:1x, вариант 1).

Слой 1 (инструменты) — штатный разбор и исполнение бота; здесь его нет. Слой 2 (динамика Оператора) — эта память:
  lookup()    — рабочая связка для ключа фразы или None (неоднозначно / деградировала / нет);
  executed()  — исход исполнения штатной командой (наблюдение, отметка LRU) -> command_id для кнопки;
  remember()  — «Запомнить связку»: только при VERIFIED этой же команды (решение 23:15 п.5, 10:45);
  reject()    — «Не то»: снимает связку ЭТОЙ фразы (не трогает другие фразы той же команды; F03/F04, R2c_FIX2);
  remember()  той же фразы на другую команду снимает прежнюю связку фразы в той же транзакции (F01/F02);
  executed()  пишет наблюдение только для уже связанной фразы — штатные команды память не раздувают (F21);
  resolve() / cancel() — ответ на вопрос «Таблица связок полна … Заменить?» (решение 23:15 п.1).
Хранение, LRU (seq по журналу), лимит 500, вытеснение с согласия, откат — PatternEngine/PatternRepository пакета.
Связка = паттерн (target = штатная команда, action = "cmd") + псевдоним (template = ключ фразы).
Потокобезопасность: один RLock на всё — бот зовёт из asyncio.to_thread (Telegram, окно Ока, голос).
"""
from __future__ import annotations

import collections
import datetime as _dt
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from ..registry import event_registry as ev
from ..registry.event_registry import EventRegistry
from ..registry.pattern_repository import PatternRepository
from ..settings import Settings
from .crystallization import VERIFIED
from .pattern_engine import BindTableFull, Evidence, PatternEngine

ACTION = "cmd"
_KEPT = 256

TEXT_NOT_VERIFIED = "Результат не подтверждён устройством. Связка не сохранена."
TEXT_EVICT = "Таблица связок полна: {n}. Самая старая: «{alias}». Заменить?"


@dataclass(frozen=True)
class BindOutcome:
    # REMEMBERED / ALREADY / NOT_VERIFIED / EVICT / EXPIRED / CANCELLED / REJECTED
    status: str
    command_id: str
    command: str = ""
    key: str = ""
    request: Optional[dict] = None
    phrase: str = ""                     # фраза Оператора, как сказана (для текстов); key — фонетический ключ


@dataclass
class _Done:
    operator_id: str
    key: str
    command: str
    exec_status: str
    session_id: str
    phrase: str = ""
    remembered: bool = False
    rejected: bool = False


@dataclass
class _Ask:
    command_id: str
    template: str
    pattern_id: str
    n: int
    created: _dt.datetime


class BindMemory:
    def __init__(self, conn: sqlite3.Connection, settings: Optional[Settings] = None,
                 clock: Optional[Callable[[], _dt.datetime]] = None, ttl_s: Optional[float] = None) -> None:
        self.conn = conn
        self.settings = settings or Settings()
        self.ttl_s = self.settings.pending_ttl_s if ttl_s is None else ttl_s
        self.registry = EventRegistry(conn, clock)
        self.repo = PatternRepository(conn)
        self.engine = PatternEngine(self.repo, self.registry, self.settings)
        self.lock = threading.RLock()
        self._done: collections.OrderedDict = collections.OrderedDict()
        self._asks: dict = {}

    # ---- чтение -------------------------------------------------------------------------
    def lookup(self, operator_id: str, key: str) -> Optional[str]:
        """Штатная команда рабочей связки или None. Несколько кристаллизованных — неоднозначно, None."""
        if not key:
            return None
        with self.lock:
            recs = [r for r in self.engine.lookup(operator_id, key) if r.action == ACTION]
            return recs[0].target if len(recs) == 1 else None

    def bind_count(self, operator_id: str) -> int:
        with self.lock:
            return self.engine.bind_count(operator_id)

    def _pid(self, operator_id: str, command: str) -> str:
        return self.engine.pattern_id(operator_id, command, ACTION, {})

    def _has_alias(self, operator_id: str, command: str, key: str) -> bool:
        rec = self.repo.get(self._pid(operator_id, command))
        return rec is not None and rec.alias(key) is not None

    def _bound_elsewhere(self, operator_id: str, key: str, command: str) -> list:
        """pattern_id связок этой фразы на ДРУГИЕ команды (любой статус)."""
        return [r.pattern_id for r in self.repo.list(operator_id)
                if r.action == ACTION and r.target != command and r.alias(key) is not None]

    def _check_owner(self, done: "_Done", operator_id: Optional[str]) -> None:
        if operator_id is not None and str(operator_id) != done.operator_id:
            raise KeyError("command belongs to another operator")     # F08: чужая кнопка

    # ---- исполнение ---------------------------------------------------------------------
    def executed(self, operator_id: str, key: str, command: str, exec_status: str, session_id: str = "",
                 phrase: str = "") -> str:
        """Исход штатного исполнения -> command_id. Наблюдение не создаёт связку (решение 23:15 п.5)."""
        command_id = uuid.uuid4().hex
        phrase = (phrase or key).strip()
        with self.lock:
            if self._has_alias(operator_id, command, key):     # отметка LRU только для связанной фразы (F21)
                try:
                    self.engine.record(Evidence(operator_id, command, ACTION, key, {}, session_id, command_id,
                                                phrase, observed=True, exec_status=exec_status))
                    self.conn.commit()
                except BaseException:
                    self.conn.rollback()
                    raise
            self._done[command_id] = _Done(operator_id, key, command, exec_status, session_id, phrase)
            while len(self._done) > _KEPT:
                self._done.popitem(last=False)
        return command_id

    # ---- «Запомнить связку» -------------------------------------------------------------
    def remember(self, command_id: str, operator_id: Optional[str] = None) -> BindOutcome:
        with self.lock:
            self._sweep()
            done = self._done.get(command_id)
            if done is None:
                raise KeyError(f"unknown command {command_id!r}")
            self._check_owner(done, operator_id)
            if done.remembered:
                return BindOutcome("ALREADY", command_id, done.command, done.key, phrase=done.phrase)
            if done.rejected:
                raise ValueError("command was rejected by the Operator")
            try:
                if done.exec_status != VERIFIED:
                    self.registry.log(ev.BIND_REFUSED, done.operator_id, {
                        "template": done.key, "command": done.command, "exec_status": done.exec_status},
                        done.session_id, command_id)
                    self.conn.commit()
                    return BindOutcome("NOT_VERIFIED", command_id, done.command, done.key,
                                       {"text": TEXT_NOT_VERIFIED}, done.phrase)
                out = self._bind(command_id, done)
                self.conn.commit()
                return out
            except BaseException:
                self.conn.rollback()
                raise

    def _bind(self, command_id: str, done: _Done) -> BindOutcome:
        for pid in self._bound_elsewhere(done.operator_id, done.key, done.command):   # фраза переучена (F01/F02)
            self.engine.evict(done.operator_id, done.key, pid, done.session_id, command_id, reason="rebind")
        evidence = Evidence(done.operator_id, done.command, ACTION, done.key, {}, done.session_id, command_id,
                            done.phrase, observed=False, validated=True, exec_status=done.exec_status, remember=True)
        try:
            self.engine.record(evidence)
        except BindTableFull as full:
            request_id = uuid.uuid4().hex
            self._asks[request_id] = _Ask(command_id, full.template, full.pattern_id, full.n, self.registry.clock())
            request = {"event": ev.EVICT_REQUEST, "request_id": request_id, "n": full.n, "oldest": full.template,
                       "oldest_pattern_id": full.pattern_id, "limit": self.settings.bind_limit,
                       "text": TEXT_EVICT.format(n=full.n, alias=full.phrase)}
            self.registry.log(ev.EVICT_REQUEST, done.operator_id, request, done.session_id, command_id)
            return BindOutcome("EVICT", command_id, done.command, done.key, request, done.phrase)
        done.remembered = True
        return BindOutcome("REMEMBERED", command_id, done.command, done.key, phrase=done.phrase)

    def resolve(self, request_id: str, operator_id: Optional[str] = None) -> BindOutcome:
        """«Заменить» — вытеснить названную самую старую и сохранить новую, одной транзакцией."""
        with self.lock:
            ask = self._asks.get(request_id)
            if ask is None:
                raise KeyError(f"unknown or already answered request {request_id!r}")
            done = self._done.get(ask.command_id)
            if done is not None:
                self._check_owner(done, operator_id)
            self._asks.pop(request_id, None)
            if self._expired(ask) or done is None:
                if done is not None:
                    self.registry.log(ev.REQUEST_EXPIRED, done.operator_id, {
                        "request_id": request_id, "kind": "EVICT", "ttl_s": self.ttl_s}, done.session_id,
                        ask.command_id)
                    self.conn.commit()
                return BindOutcome("EXPIRED", ask.command_id)
            if done.remembered:
                return BindOutcome("ALREADY", ask.command_id, done.command, done.key, phrase=done.phrase)
            try:
                self.registry.log(ev.OPERATOR_RESPONSE, done.operator_id, {"request_id": request_id, "answers": {}},
                                  done.session_id, ask.command_id)
                rec = self.repo.get(ask.pattern_id)
                if rec is not None and rec.alias(ask.template) is not None:   # уже снята -- журнал не засоряем (F11)
                    self.engine.evict(done.operator_id, ask.template, ask.pattern_id, done.session_id, ask.command_id)
                out = self._bind(ask.command_id, done)
                self.conn.commit()
                return out
            except BaseException:
                self.conn.rollback()             # вытеснение без новой связки не остаётся
                raise

    def cancel(self, request_id: str, operator_id: Optional[str] = None) -> BindOutcome:
        """«Оставить» — таблица не меняется, новая связка не сохраняется."""
        with self.lock:
            ask = self._asks.get(request_id)
            if ask is None:
                raise KeyError(f"unknown or already answered request {request_id!r}")
            done = self._done.get(ask.command_id)
            if done is not None:
                self._check_owner(done, operator_id)
            self._asks.pop(request_id, None)
            if done is not None:
                self.registry.log(ev.REQUEST_CANCELLED, done.operator_id, {"request_id": request_id, "kind": "EVICT"},
                                  done.session_id, ask.command_id)
                self.conn.commit()
            return BindOutcome("CANCELLED", ask.command_id)

    # ---- «Не то» ------------------------------------------------------------------------
    def reject(self, command_id: str, operator_id: Optional[str] = None) -> BindOutcome:
        """«Не то»: снимается связка ЭТОЙ фразы с этой командой; другие фразы той же команды не трогаются (F03/F04)."""
        with self.lock:
            done = self._done.get(command_id)
            if done is None:
                raise KeyError(f"unknown command {command_id!r}")
            self._check_owner(done, operator_id)
            if done.rejected:
                return BindOutcome("ALREADY", command_id, done.command, done.key, phrase=done.phrase)
            try:
                if self._has_alias(done.operator_id, done.command, done.key):
                    self.engine.evict(done.operator_id, done.key, self._pid(done.operator_id, done.command),
                                      done.session_id, command_id, reason="reject")
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            done.rejected = True
            return BindOutcome("REJECTED", command_id, done.command, done.key, phrase=done.phrase)

    # ---- TTL ----------------------------------------------------------------------------
    def _expired(self, ask: _Ask) -> bool:
        return (self.registry.clock() - ask.created).total_seconds() > self.ttl_s

    def _sweep(self) -> None:
        for rid in [r for r, a in self._asks.items() if self._expired(a)]:
            self._asks.pop(rid, None)
