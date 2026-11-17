"""Контракт выполнения: S -P-> V -H-> V' -E-> R -G6-> W'_O.

OPERATOR_INPUT -> NORMALIZATION -> ALIAS_LOOKUP -> VECTOR_PROJECTION -> MODALITY_RESOLUTION
  EXEC     -> COMMAND_ROUTER -> DEVICE_ADAPTER -> STATE_VERIFICATION -> PATTERN_ENGINE -> REGISTRY
  CLARIFY  -> запрос недостающей координаты у Оператора -> RESOLVED -> как EXEC
  ABSTRACT -> совет LLM (только текст; исполнительных адаптеров LLM не касается)
  BIND     -> запрос на сохранение (сам диалог привязки в эту версию не входит)

Обучение (решения Оператора 23:15 п.1 и п.5):
  выбор варианта в CLARIFY, подтверждение CONFIRM и метка confirm() связку НЕ создают;
  remember(command_id) — кнопка «Запомнить связку»: только после исполнения с результатом VERIFIED;
  таблица связок полна (Settings.bind_limit) -> EVICT: вопрос Оператору о замене самой старой;
  молча ничего не вытесняется, каждое вытеснение — событие BIND_EVICTED в журнале.
"""
from __future__ import annotations

import collections
import datetime as _dt
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from .calibration import AccuracyMonitor, CalibrationEngine, ConfidenceModel
from .execution import CommandRouter, DeviceAdapter, ExecutionResult
from .home import HomeConfig
from .memory import BindTableFull, Evidence, PatternEngine
from .memory.crystallization import VERIFIED
from .parser import VectorParser
from .registry import EventRegistry, PatternRepository
from .registry import event_registry as ev
from .settings import Settings
from .space import ABSTRACT, BIND, CLARIFY, COLORS, EXEC, Intent

_AXIS = {"target": "T", "action": "A"}
_KEPT_COMMANDS = 256
EVICT = "EVICT"

TEXT_NOT_VERIFIED = "Результат не подтверждён устройством. Связка не сохранена."
TEXT_EVICT = "Таблица связок полна: {n}. Самая старая: «{alias}». Заменить?"


@dataclass(frozen=True)
class Outcome:
    # EXECUTED / FAILED / CLARIFY / CONFIRM / ABSTRACT / BIND / EXPIRED / CANCELLED
    # REMEMBERED / ALREADY / NOT_VERIFIED / EVICT (remember)
    status: str
    intent: Intent
    command_id: str = ""
    request: Optional[dict] = None           # CLARIFY_REQUEST / CONFIRM_REQUEST для Оператора
    execution: Optional[ExecutionResult] = None
    advisory: Optional[str] = None           # текст LLM в ветке ABSTRACT


@dataclass
class _Pending:
    kind: str                                # CLARIFY / CONFIRM / EVICT
    initial: Intent                          # V: что выдал парсер
    intent: Intent                           # текущее состояние вектора
    operator_id: str
    session_id: str
    context_key: str
    command_id: str
    corrected: bool = False                  # Оператор менял то, что парсер утверждал
    confirmed: bool = False                  # Оператор подтвердил именно эти координаты
    sample: bool = True                      # писать ли отсчёт точности после исполнения
    reason: str = ""                         # почему CONFIRM: needs_confirm / calibration
    created: Optional[_dt.datetime] = None   # когда задан вопрос (для TTL)
    evict: Optional[tuple] = None            # EVICT: (template, pattern_id) предложенной к замене


@dataclass
class _Executed:
    intent: Intent
    operator_id: str
    session_id: str
    context_key: str
    exec_status: str
    validated: bool                          # Оператор уже участвовал (уточнение / подтверждение / метка)
    labelled: Optional[bool] = None          # метка confirm(): True / False / None
    remembered: bool = False                 # связка по этой команде сохранена


def context_key(context: Optional[dict]) -> str:
    parts = [f"{k}={context[k]}" for k in ("room", "daypart") if context and context.get(k)]
    return "|".join(parts) or "*"


class Vinaigrette:
    def __init__(self, conn: sqlite3.Connection, home: HomeConfig, adapter: DeviceAdapter, *,
                 advisor: Optional[Callable[[str], str]] = None, settings: Optional[Settings] = None,
                 clock: Optional[Callable[[], _dt.datetime]] = None) -> None:
        self.conn = conn
        self.home = home
        self.settings = settings or Settings()
        self.advisor = advisor
        self.registry = EventRegistry(conn, clock)
        self.repo = PatternRepository(conn)
        self.engine = PatternEngine(self.repo, self.registry, self.settings)
        self.monitor = AccuracyMonitor(self.registry)
        self.calibration = CalibrationEngine(self.repo, self.registry, self.monitor, self.engine, self.settings)
        self.confidence = ConfidenceModel(self.repo, self.settings)
        self.parser = VectorParser(home, self.settings, self._lookup)
        self.router = CommandRouter(home, adapter)
        self._pending: dict = {}
        self._commands: collections.OrderedDict = collections.OrderedDict()

    def _lookup(self, operator_id: str, template: str) -> list:
        """Инвариант 9: при DEGRADED обученные псевдонимы автоматически не применяются."""
        if self.calibration.is_degraded(operator_id):
            return []
        return self.engine.lookup(operator_id, template)

    # ---- вход ---------------------------------------------------------------------------
    def handle(self, raw: str, operator_id: str, session_id: str = "", context: Optional[dict] = None) -> Outcome:
        command_id = uuid.uuid4().hex
        self._sweep()
        intent = self.parser.parse(raw, operator_id, context)
        ids = {"session_id": session_id, "command_id": command_id}
        self.registry.log(ev.OPERATOR_INPUT, operator_id, {"normalized": intent.normalized}, **ids)
        self.registry.log(ev.INTENT_PARSED, operator_id, intent.describe(), **ids)
        try:
            if intent.modality == ABSTRACT:
                advisory = self._advise(raw)
                self.registry.log(ev.ABSTRACT_ADVISORY, operator_id, {"advised": advisory is not None}, **ids)
                return Outcome("ABSTRACT", intent, command_id, advisory=advisory)
            if intent.modality == BIND:
                self.registry.log(ev.BIND_REQUESTED, operator_id, {"payload": intent.bind_payload}, **ids)
                return Outcome("BIND", intent, command_id)
            pending = _Pending(CLARIFY, intent, intent, operator_id, session_id, context_key(context), command_id)
            return self._dispatch(pending, validated=False)
        finally:
            self.conn.commit()

    def _advise(self, raw: str) -> Optional[str]:
        if self.advisor is None:
            return None
        try:
            return str(self.advisor(raw))
        except Exception:                        # сбой LLM не должен ронять командный контур
            return None

    # ---- CLARIFY / CONFIRM --------------------------------------------------------------
    def _dispatch(self, p: _Pending, validated: bool) -> Outcome:
        """Неполный вектор — к Оператору; опасная команда и режим калибровки — через CONFIRM."""
        intent = p.intent
        if intent.modality == CLARIFY:
            p.kind = CLARIFY
            return self._ask(p)
        if not p.confirmed:
            if self.home.needs_confirm(intent.target.value, intent.action.value):
                p.reason = "needs_confirm"       # всегда, даже для кристаллизованного паттерна
            elif not validated and self.calibration.is_degraded(p.operator_id):
                p.reason = "calibration"         # калибровка: координаты подтверждаются вручную
            else:
                p.reason = ""
            if p.reason:
                p.kind = "CONFIRM"
                return self._ask(p)
        return self._execute(p, validated)

    def _expired(self, p: _Pending) -> bool:
        return (self.registry.clock() - p.created).total_seconds() > self.settings.pending_ttl_s

    def _expire(self, request_id: str) -> _Pending:
        p = self._pending.pop(request_id)
        self.registry.log(ev.REQUEST_EXPIRED, p.operator_id, {
            "request_id": request_id, "kind": p.kind, "ttl_s": self.settings.pending_ttl_s},
            p.session_id, p.command_id)
        return p

    def _sweep(self) -> None:
        for request_id in [r for r, p in self._pending.items() if self._expired(p)]:
            self._expire(request_id)

    def cancel(self, request_id: str) -> Outcome:
        """Оператор отказался отвечать («нет»): запрос снят, исполнения нет."""
        p = self._pending.pop(request_id, None)
        if p is None:
            raise KeyError(f"unknown or already answered request {request_id!r}")
        self.registry.log(ev.REQUEST_CANCELLED, p.operator_id, {"request_id": request_id, "kind": p.kind},
                          p.session_id, p.command_id)
        self.conn.commit()
        return Outcome("CANCELLED", p.intent, p.command_id)

    def _ask(self, p: _Pending) -> Outcome:
        request_id = uuid.uuid4().hex
        intent = p.intent
        p.created = self.registry.clock()
        if p.kind == CLARIFY:
            field = intent.missing[0]
            request = {"event": ev.CLARIFY_REQUEST, "request_id": request_id, "missing_field": field,
                       "candidates": self._candidates(p, field), "missing": list(intent.missing)}
        elif p.kind == EVICT:
            template, pattern_id, n = p.evict
            request = {"event": ev.EVICT_REQUEST, "request_id": request_id, "n": n, "oldest": template,
                       "oldest_pattern_id": pattern_id, "limit": self.settings.bind_limit,
                       "text": TEXT_EVICT.format(n=n, alias=template)}
        else:
            request = {"event": ev.CONFIRM_REQUEST, "request_id": request_id, "target": intent.target.value,
                       "action": intent.action.value, "params": intent.params.schema(), "reason": p.reason}
        self._pending[request_id] = p
        self.registry.log(request["event"], p.operator_id, request, p.session_id, p.command_id)
        return Outcome(p.kind, intent, p.command_id, request=request)

    def _candidates(self, p: _Pending, field: str) -> list:
        intent = p.intent
        if field == "target":
            action = intent.action.value if intent.action.is_unique else None
            pool = self.home.targets_for(action)
            cands = [c for c in intent.target.candidates if c in pool] or list(pool)
            fixed = {"action": action} if action else {}
        elif field == "action":
            pool = self.home.actions_for(intent.target.value) if intent.target.is_unique else ()
            cands = [c for c in intent.action.candidates if not pool or c in pool] or list(pool)
            fixed = {"target": intent.target.value} if intent.target.is_unique else {}
        else:
            return list(COLORS) if field == "color" else []
        # сначала то, что Оператор уже выбирал для этой формулировки, затем по частоте в контексте
        hinted = [getattr(r, field) for r in self.engine.aliases.suggestions(p.operator_id, intent.template)]
        ranked = self.confidence.rank(p.operator_id, p.context_key, field, cands, fixed)
        return sorted(ranked, key=lambda c: hinted.index(c) if c in hinted else len(hinted))

    def resolve(self, request_id: str, answers: Optional[dict] = None) -> Outcome:
        """R(v, H) = v'. Для CLARIFY Оператор даёт только недостающие координаты; для CONFIRM
        пустой ответ — подтверждение, непустой — исправление."""
        p = self._pending.get(request_id)
        if p is None:
            raise KeyError(f"unknown or already answered request {request_id!r}")
        if self._expired(p):
            self._expire(request_id)             # опоздавший ответ ничего не исполняет
            self.conn.commit()
            return Outcome("EXPIRED", p.intent, p.command_id)
        answers = dict(answers or {})
        if p.kind == EVICT:
            if answers:
                raise ValueError("EVICT accepts only an empty answer (yes); use cancel() for no")
            del self._pending[request_id]
            try:
                self.registry.log(ev.OPERATOR_RESPONSE, p.operator_id, {"request_id": request_id, "answers": {}},
                                  p.session_id, p.command_id)
                return self._replace(p)
            except BaseException:
                self.conn.rollback()             # вытеснение без новой связки не остаётся
                raise
            finally:
                self.conn.commit()
        if p.kind == CLARIFY:
            extra = set(answers) - set(p.intent.missing)
            if extra or not answers:
                raise ValueError(f"expected only missing coordinates {p.intent.missing}, got {sorted(answers)}")
        filled = self.parser.fill(p.intent, answers)     # ValueError не снимает запрос
        del self._pending[request_id]
        try:
            self.registry.log(ev.OPERATOR_RESPONSE, p.operator_id, {"request_id": request_id, "answers": answers},
                              p.session_id, p.command_id)
            if p.kind == "CONFIRM":
                p.corrected = p.corrected or bool(answers)
                p.confirmed = not answers        # исправленные координаты подтверждаются заново
            p.intent = filled
            return self._dispatch(p, validated=True)
        finally:
            self.conn.commit()

    # ---- исполнение и обучение ----------------------------------------------------------
    def _execute(self, p: _Pending, validated: bool) -> Outcome:
        final = p.intent
        ids = {"session_id": p.session_id, "command_id": p.command_id}
        result = self.router.execute(self.router.build(final), confirmed=p.confirmed)
        self.registry.log(ev.EXECUTION, p.operator_id, {
            "target": final.target.value, "action": final.action.value, "status": result.status,
            "detail": result.detail}, **ids)
        if not result.ok:
            # ошибка устройства — инфраструктурный инцидент; оценку парсера она не меняет
            self.registry.log(ev.INFRA_INCIDENT, p.operator_id, {"status": result.status, "detail": result.detail},
                              **ids)
        frozen = self.calibration.is_degraded(p.operator_id)
        if validated and p.corrected and p.initial.modality == EXEC:    # парсер утверждал иное
            self.engine.record(self._evidence(p, p.initial, observed=False, validated=False), frozen)
        # выбор варианта / подтверждение — не обучение: только наблюдение (решение 23:15 п.5)
        self.engine.record(self._evidence(p, final, exec_status=result.status), frozen)
        if validated and p.sample:
            self._sample(p, p.initial, final)
        self._commands[p.command_id] = _Executed(final, p.operator_id, p.session_id, p.context_key,
                                                 result.status, validated)
        while len(self._commands) > _KEPT_COMMANDS:
            self._commands.popitem(last=False)
        return Outcome("EXECUTED" if result.ok else "FAILED", final, p.command_id, execution=result)

    @staticmethod
    def _evidence(p: _Pending, intent: Intent, **kw) -> Evidence:
        return Evidence(p.operator_id, intent.target.value, intent.action.value, intent.template,
                        intent.params.schema(), p.session_id, p.command_id, intent.normalized,
                        context_key=p.context_key, **kw)

    def _sample(self, p: _Pending, initial: Intent, final: Intent) -> None:
        """Проверенная Оператором команда -> отсчёт точности распознавания."""
        axes = set()
        strict = self.settings.clarify_is_miss
        for name, a, b in (("T", initial.target, final.target), ("A", initial.action, final.action)):
            if a.value != b.value and (a.is_unique or strict):
                axes.add(name)               # парсер утверждал одно, Оператор подтвердил другое
        pa, pb = initial.params, final.params
        if any(x != y and (x is not None or strict) for x, y in
               ((pa.level, pb.level), (pa.color, pb.color), (pa.room, pb.room), (pa.device, pb.device))):
            axes.add("P")
        self.monitor.record(p.operator_id, not axes, axes, p.session_id, p.command_id)
        self.calibration.check(p.operator_id)

    def confirm(self, command_id: str, correct: bool, corrected: Optional[dict] = None) -> Optional[Outcome]:
        """Явная метка Оператора по уже исполненной команде. corrected — верные координаты;
        с ними исправленная команда исполняется (или уходит на уточнение)."""
        done = self._commands.get(command_id)
        if done is None:
            raise KeyError(f"unknown command {command_id!r}")
        if done.validated:
            raise ValueError("command is already validated by the Operator")
        p = _Pending(CLARIFY, done.intent, done.intent, done.operator_id, done.session_id, done.context_key,
                     command_id)
        fixed = self.parser.fill(done.intent, corrected or {}) if not correct else done.intent
        done.validated = True
        done.labelled = bool(correct)
        try:
            frozen = self.calibration.is_degraded(done.operator_id)
            self.engine.record(self._evidence(p, done.intent, observed=False, validated=bool(correct),
                                              exec_status=done.exec_status), frozen)
            if correct:
                self._sample(p, done.intent, done.intent)
                return None
            axes = {_AXIS.get(k, "P") for k in (corrected or {})} or {"T", "A"}
            self.monitor.record(done.operator_id, False, axes, done.session_id, command_id)
            self.calibration.check(done.operator_id)
            if not corrected:
                return None
            p.intent, p.command_id, p.sample = fixed, uuid.uuid4().hex, False
            return self._dispatch(p, validated=True)
        finally:
            self.conn.commit()

    # ---- «Запомнить связку» ------------------------------------------------------------
    def remember(self, command_id: str) -> Outcome:
        """Кнопка «Запомнить связку» по исполненной команде: реплика -> её команда.
        Связка — только при результате VERIFIED; при полной таблице — вопрос EVICT."""
        done = self._commands.get(command_id)
        if done is None:
            raise KeyError(f"unknown command {command_id!r}")
        if done.remembered:                      # повтор доставки нажатия (Telegram): ничего не пишем
            return Outcome("ALREADY", done.intent, command_id)
        if done.labelled is False:
            raise ValueError("command was rejected by the Operator")
        p = _Pending(EVICT, done.intent, done.intent, done.operator_id, done.session_id, done.context_key,
                     command_id)
        try:
            if done.exec_status != VERIFIED:
                self.registry.log(ev.BIND_REFUSED, done.operator_id, {
                    "template": done.intent.template, "exec_status": done.exec_status},
                    done.session_id, command_id)
                return Outcome("NOT_VERIFIED", done.intent, command_id, request={"text": TEXT_NOT_VERIFIED})
            return self._bind(p, done)
        except BaseException:
            self.conn.rollback()                 # журнал и память — одной транзакцией
            raise
        finally:
            self.conn.commit()

    def _bind(self, p: _Pending, done: _Executed) -> Outcome:
        frozen = self.calibration.is_degraded(done.operator_id)
        evidence = self._evidence(p, done.intent, observed=False,
                                  validated=None if done.labelled else True,   # метку «верно» не удваивать
                                  exec_status=done.exec_status, remember=True)
        try:
            self.engine.record(evidence, frozen)
        except BindTableFull as full:
            p.kind, p.evict = EVICT, (full.template, full.pattern_id, full.n)
            return self._ask(p)
        done.remembered = True
        done.validated, done.labelled = True, True     # повторная метка confirm() не удвоит s_j
        return Outcome("REMEMBERED", done.intent, p.command_id)

    def _replace(self, p: _Pending) -> Outcome:
        done = self._commands.get(p.command_id)
        if done is None or done.remembered:      # команда вытеснена из окна или уже запомнена
            return Outcome("EXPIRED", p.intent, p.command_id)
        template, pattern_id, _ = p.evict
        self.engine.evict(p.operator_id, template, pattern_id, p.session_id, p.command_id)
        return self._bind(p, done)

    def rollback(self, operator_id: str, version: int) -> int:
        try:
            return self.engine.rollback(operator_id, version)
        finally:
            self.conn.commit()
