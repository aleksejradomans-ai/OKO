"""PatternEngine G6: множество словесных вариаций -> малое число устойчивых командных структур.

K = Hash(O, t, a, Schema(p)) — ключ паттерна; Template(S) — ключ псевдонима внутри паттерна.
(В спецификации Template входит в K, но тогда «включи свет» и «зажги лампы» дали бы две записи,
что противоречит §6 и правилу 2 §8; шаблон поэтому различает псевдонимы, а не паттерны.)

Состояние G6 — свёртка событий EVIDENCE и BIND_EVICTED: одни и те же функции работают и при записи,
и при пересчёте памяти из журнала.

Обучение только явное (решение Оператора 23:15 п.5): связка (псевдоним) заводится и закрепляется
только свидетельством remember=True — кнопка «Запомнить связку» или «запомни: X — Y» — и только
при подтверждённом результате исполнения (VERIFIED). Наблюдение и выбор варианта в «❓ Уточни»
связку не создают. Лимит связок — Settings.bind_limit; при полной таблице record() отказывает
(BindTableFull), вытеснение — только evict() по согласию Оператора (п.1).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Optional

from ..registry import event_registry as ev
from ..registry.event_registry import EventRegistry
from ..registry.pattern_repository import PatternRecord, PatternRepository
from ..settings import Settings
from .alias_registry import AliasRegistry
from .crystallization import VERIFIED, CrystallizationEngine
from .memory_compressor import MemoryCompressor

_SESSIONS_KEPT = 8
_EXEC_FAILED = ("FAILED", "MISMATCH")


@dataclass(frozen=True)
class Evidence:
    """Одно свидетельство о паттерне.

    observed  — команда прошла через исполнительный контур (наблюдение + результат исполнения);
    validated — метка Оператора о сопоставлении: True / False / None (нет метки);
    exec_status — VERIFIED / ACKNOWLEDGED / FAILED / MISMATCH для связанного исполнения;
    remember  — явная команда Оператора «Запомнить связку» (единственный путь обучения).
    """
    operator_id: str
    target: str
    action: str
    template: str
    schema: dict = field(default_factory=dict)
    session_id: str = ""
    command_id: str = ""
    example: str = ""
    observed: bool = True
    validated: Optional[bool] = None
    exec_status: Optional[str] = None
    context_key: str = "*"
    remember: bool = False


class BindTableFull(Exception):
    """Новая связка не помещается: нужна замена самой старой с согласия Оператора."""

    def __init__(self, n: int, limit: int, template: str, pattern_id: str, phrase: str = "") -> None:
        super().__init__(f"bind table full: {n}/{limit}, oldest {template!r}")
        self.n, self.limit, self.template, self.pattern_id = n, limit, template, pattern_id
        self.phrase = phrase or template      # как Оператор её сказал (для текста вопроса)


class PatternEngine:
    def __init__(self, repo: PatternRepository, registry: EventRegistry,
                 settings: Optional[Settings] = None) -> None:
        self.repo = repo
        self.registry = registry
        self.settings = settings or Settings()
        self.crystallizer = CrystallizationEngine(self.settings)
        self.compressor = MemoryCompressor(self.settings)
        self.aliases = AliasRegistry(repo)

    @staticmethod
    def pattern_id(operator_id: str, target: str, action: str, schema: dict) -> str:
        key = json.dumps([operator_id, target, action, sorted(schema.items())], ensure_ascii=False)
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def canonical_template(target: str, action: str, schema: dict) -> str:
        parts = [f"{target.upper()} + {action.upper()}"]
        parts += [f"{{{k}}}" if v == "slot" else f"{k}={v}" for k, v in sorted(schema.items())]
        return " ".join(parts)

    # ---- запись -------------------------------------------------------------------------
    def record(self, evidence: Evidence, frozen: bool = False) -> PatternRecord:
        if self._adds_bind(evidence):
            records = self.repo.list(evidence.operator_id)
            n = self.compressor.count(records)
            if n >= self.settings.bind_limit:
                template, pid = self.compressor.oldest(records)
                rec = next(r for r in records if r.pattern_id == pid)
                raise BindTableFull(n, self.settings.bind_limit, template, pid,     # в журнал ничего не пишется
                                    rec.alias(template).get("phrase", template))
        ts = self.registry.now()
        event_id = self.registry.log(ev.EVIDENCE, evidence.operator_id, asdict(evidence),
                                     evidence.session_id, evidence.command_id, ts)
        return self._apply(evidence, ts, frozen, event_id)

    def _adds_bind(self, e: Evidence) -> bool:
        if not (e.remember and e.exec_status == VERIFIED):
            return False
        rec = self.repo.get(self.pattern_id(e.operator_id, e.target, e.action, e.schema))
        return rec is None or rec.alias(e.template) is None

    def _apply(self, e: Evidence, ts: str, frozen: bool, event_id: int, replay: bool = False) -> PatternRecord:
        pid = self.pattern_id(e.operator_id, e.target, e.action, e.schema)
        rec = self.repo.get(pid)
        changes = []
        if rec is None:
            rec = PatternRecord(pid, e.operator_id, self.canonical_template(e.target, e.action, e.schema),
                                e.target, e.action, dict(e.schema), first_seen=ts)
            changes.append((ev.PATTERN_CREATED, {"canonical_template": rec.canonical_template}))
        rec.last_seen = ts
        alias = rec.alias(e.template)
        bind = self.crystallizer.learn(e.remember, True, True, e.exec_status == VERIFIED)
        if alias is None and bind:
            alias = {"template": e.template, "count": 0, "confirmed": 0, "last_seen": ts, "seq": event_id,
                     "phrase": e.example or e.template}
            rec.aliases.append(alias)
            changes.append((ev.ALIAS_ADDED, {"template": e.template}))
        if e.observed:
            rec.observations += 1           # повтор увеличивает счётчик, новой записи не создаёт
            if alias is not None:           # использование связки — отметка для LRU
                alias["count"] += 1
                alias["last_seen"] = ts
                alias["seq"] = event_id     # порядок LRU — по журналу, не по разрешению часов
            if e.exec_status == VERIFIED:
                rec.verified_executions += 1
            elif e.exec_status in _EXEC_FAILED:
                rec.execution_failures += 1  # сбой устройства — не ошибка сопоставления
            if e.example and e.example not in rec.examples and len(rec.examples) < self.settings.examples_max:
                rec.examples.append(e.example)
        if e.validated is True:
            rec.confirmed_correct += 1
        elif e.validated is False:
            rec.confirmed_incorrect += 1
        if bind:
            alias["confirmed"] += 1
            alias["last_seen"] = ts
            alias["seq"] = event_id
            rec.learned += 1
            if e.session_id not in rec.sessions:
                rec.sessions = (rec.sessions + [e.session_id])[-_SESSIONS_KEPT:]
                rec.session_count += 1
            self.repo.bump_context(e.operator_id, e.context_key, pid)
        rec.confidence = self.crystallizer.quality(rec.confirmed_correct, rec.confirmed_incorrect)
        status = self.crystallizer.next_status(rec, frozen)
        if status != rec.status:
            changes.append((ev.PATTERN_STATUS, {"from": rec.status, "to": status, "confidence": rec.confidence}))
            rec.status = status
        self.repo.save(rec)
        if changes and not replay:
            version = self.repo.snapshot(e.operator_id, changes[-1][0], event_id, ts)
            for type_, payload in changes:
                self.registry.log(type_, e.operator_id, {**payload, "pattern_id": pid, "memory_version": version},
                                  e.session_id, e.command_id)
        return rec

    def evict(self, operator_id: str, template: str, pattern_id: str,
              session_id: str = "", command_id: str = "", reason: str = "lru") -> int:
        """Снятие связки: lru — замена по согласию Оператора; rebind — фраза переучена на другую команду;
        reject — «Не то» Оператора по этой фразе (R2c_FIX2, F01/F03). Возвращает новую версию памяти."""
        n = self.bind_count(operator_id)
        payload = {"template": template, "pattern_id": pattern_id, "n": n,
                   "limit": self.settings.bind_limit, "reason": reason}
        event_id = self.registry.log(ev.BIND_EVICTED, operator_id, payload, session_id, command_id)
        self._apply_evict(operator_id, template, pattern_id)
        return self.repo.snapshot(operator_id, ev.BIND_EVICTED, event_id, self.registry.now())

    def _apply_evict(self, operator_id: str, template: str, pattern_id: str) -> None:
        rec = self.repo.get(pattern_id)
        if rec is None or rec.operator_id != operator_id:
            return
        rec.aliases = [a for a in rec.aliases if a["template"] != template]
        self.repo.save(rec)

    def bind_count(self, operator_id: str) -> int:
        return self.compressor.count(self.repo.list(operator_id))

    # ---- чтение -------------------------------------------------------------------------
    def lookup(self, operator_id: str, template: str) -> list[PatternRecord]:
        return self.aliases.crystallized(operator_id, template)

    def compression_ratio(self, operator_id: str) -> float:
        return self.compressor.ratio(self.repo.list(operator_id))

    # ---- обслуживание памяти ------------------------------------------------------------
    def reevaluate(self, operator_id: str) -> None:
        """Пересмотр статусов после снятия заморозки."""
        for rec in self.repo.list(operator_id):
            status = self.crystallizer.next_status(rec)
            if status != rec.status:
                old, rec.status = rec.status, status
                self.repo.save(rec)
                version = self.repo.snapshot(operator_id, ev.PATTERN_STATUS, self.registry.last_id(),
                                             self.registry.now())
                self.registry.log(ev.PATTERN_STATUS, operator_id, {
                    "from": old, "to": status, "confidence": rec.confidence,
                    "pattern_id": rec.pattern_id, "memory_version": version})

    def recount(self, operator_id: str) -> int:
        """Пересчитывает память из журнала по подтверждённым данным. Возвращает новую версию."""
        events = self.registry.events(operator_id, (ev.EVIDENCE, ev.BIND_EVICTED, ev.MEMORY_ROLLBACK))
        voided = [(e.payload["void_after"], e.payload["void_until"])
                  for e in events if e.type == ev.MEMORY_ROLLBACK]
        self.repo.clear(operator_id)
        for e in events:
            if e.type == ev.MEMORY_ROLLBACK or any(lo < e.id <= hi for lo, hi in voided):
                continue
            if e.type == ev.BIND_EVICTED:
                self._apply_evict(operator_id, e.payload["template"], e.payload["pattern_id"])
                continue
            self._apply(Evidence(**e.payload), e.ts, False, e.id, replay=True)
        last = self.registry.last_id()
        version = self.repo.snapshot(operator_id, ev.MEMORY_RECOUNT, last, self.registry.now())
        self.registry.log(ev.MEMORY_RECOUNT, operator_id, {"memory_version": version})
        return version

    def rollback(self, operator_id: str, version: int) -> int:
        """Откат к прежней версии памяти; свидетельства, накопленные после неё, аннулируются."""
        void_until = self.registry.last_id()
        void_after = self.repo.restore(operator_id, version)
        event_id = self.registry.log(ev.MEMORY_ROLLBACK, operator_id, {
            "to_version": version, "void_after": void_after, "void_until": void_until})
        return self.repo.snapshot(operator_id, ev.MEMORY_ROLLBACK, event_id, self.registry.now())
