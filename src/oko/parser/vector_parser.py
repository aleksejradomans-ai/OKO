"""VectorParser: реплика -> вектор намерения. Детерминированно, без LLM.

pi(N, W_O) = (C_T, C_A, C_P): встроенные словари, затем кристаллизованные псевдонимы Оператора.
Один кандидат — координата определена, ноль — UNKNOWN, несколько — AMBIGUOUS.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from ..home import HomeConfig
from ..settings import Settings
from ..space import (ABSTRACT, BIND, CLARIFY, COLORS, REAL_ACTIONS, SRC_G6, SRC_OPERATOR, Coordinate, Intent)
from . import action_axis, parameter_axis, target_axis
from .action_axis import ActionAxis
from .modality_resolver import ModalityResolver
from .normalization import Tok, morph, normalize
from .parameter_axis import ParameterAxis
from .target_axis import TargetAxis

# lookup(operator_id, template) -> записи G6, допущенные к автоматическому применению
Lookup = Callable[[str, str], list]


def _match(lemmas: list, phrases: dict, used: set) -> dict:
    """Самые длинные словарные фразы; возвращает значение -> позиции токенов."""
    found: dict = {}
    longest = max(len(p) for p in phrases)
    i = 0
    while i < len(lemmas):
        for n in range(min(longest, len(lemmas) - i), 0, -1):
            value = phrases.get(tuple(lemmas[i:i + n]))
            if value is not None:
                found.setdefault(value, []).extend(range(i, i + n))
                used.update(range(i, i + n))
                i += n - 1
                break
        i += 1
    return found


class VectorParser:
    def __init__(self, home: HomeConfig, settings: Optional[Settings] = None,
                 lookup: Optional[Lookup] = None) -> None:
        self.home = home
        self.settings = settings or Settings()
        self.lookup = lookup
        self.targets = TargetAxis(home)
        self.actions = ActionAxis()
        self.params = ParameterAxis(home)
        self.modality = ModalityResolver(home)
        self.forms: dict = {}
        for table in (target_axis.FORMS, action_axis.FORMS, action_axis.CUE_FORMS, action_axis.BIND_FORMS,
                      parameter_axis.COLOR_FORMS, parameter_axis.LEVEL_FORMS, self.params.room_forms):
            for lemma, forms in table.items():
                for form in forms:
                    if self.forms.setdefault(form, lemma) != lemma:
                        raise ValueError(f"word form {form!r} belongs to two lemmas")
        # значение одиночной леммы — для проверки нечётких альтернатив
        self._meaning = {p[0]: ("target", v) for p, v in target_axis.PHRASES.items() if len(p) == 1}
        self._meaning.update({p[0]: ("action", v) for p, v in action_axis.PHRASES.items()})
        self._meaning.update({c: ("color", c) for c in COLORS})

    # ---- разбор -------------------------------------------------------------------------
    def parse(self, raw: str, operator_id: str, context: Optional[dict] = None) -> Intent:
        norm = normalize(raw, self.forms.keys(), self.settings.fuzzy_min_len)
        toks = morph(norm.tokens, self.forms, self.settings.fuzzy_min_len, norm.variants)
        lemmas = [t.lemma for t in toks]

        # 1. Явное сохранение в память
        if toks and toks[0].lemma in action_axis.BIND_FORMS:
            return Intent(raw, norm.text, " ".join(lemmas), modality=BIND,
                          bind_payload=norm.text.split(" ", 1)[1] if " " in norm.text else "")

        # 2. Проекция на оси
        used: set = set()
        params, slots = self.params.extract(toks, used)
        t_pos = _match(lemmas, target_axis.PHRASES, used)
        a_pos = _match(lemmas, action_axis.PHRASES, used)
        t_found = {v: all(toks[i].fuzzy for i in pos) for v, pos in t_pos.items()}
        a_found = {v: all(toks[i].fuzzy for i in pos) for v, pos in a_pos.items()}
        cue_word = False
        negated = False
        for i, t in enumerate(toks):
            if t.lemma in action_axis.CUE_FORMS:
                cue_word = True
                used.add(i)
            elif t.text in action_axis.NEGATIONS:
                negated = True
                used.add(i)
        clash = self._fuzzy_clash(toks)
        template = " ".join(slots.get(i, lemma) for i, lemma in enumerate(lemmas))

        target = self.targets.resolve(t_found, context)
        action = self.actions.resolve(a_found, params.color is not None, negated, REAL_ACTIONS)
        if "target" in clash:
            target = Coordinate.ambiguous(sorted(target_axis.expand(clash["target"] | set(t_found))), "FUZZY")
        if "action" in clash:
            action = Coordinate.ambiguous(sorted(clash["action"] | set(a_found)), "FUZZY")
        if "color" in clash and "color" not in params.invalid:
            params = replace(params, color=None, invalid=params.invalid + ("color",))

        # 3. Персональная память G6
        hits = self.lookup(operator_id, template) if self.lookup and not negated else []
        if hits:
            target = self._merge(target, {self.home.canonical(h.target) for h in hits}, hits)
            action = self._merge(action, {h.action for h in hits}, hits)
            if len(hits) == 1:
                schema = hits[0].parameter_schema
                params = replace(params, room=params.room or schema.get("room"),
                                 device=params.device or schema.get("device"))

        # 4. Модальность
        cue = cue_word or bool(hits) or not action.is_unknown or (
            not target.is_unknown and (params.level is not None or bool(params.invalid)))
        modality, missing = self.modality.resolve(target, action, params, cue)
        residual = len(toks) - len(used)
        if modality == CLARIFY and not hits and residual > self.settings.max_residual:
            modality, missing = ABSTRACT, ()     # длинный свободный текст с обрывком команды
        return Intent(raw, norm.text, template, target, action, params, modality, missing)

    def _fuzzy_clash(self, toks: list[Tok]) -> dict:
        """Слово с несколькими нечёткими кандидатами разных значений: ось -> значения."""
        clash: dict = {}
        for t in toks:
            meanings = {self._meaning[l] for l in t.alternatives if l in self._meaning}
            if len(meanings) > 1:
                for axis, value in meanings:
                    clash.setdefault(axis, set()).add(value)
        return clash

    @staticmethod
    def _merge(coord: Coordinate, values: set, hits: list) -> Coordinate:
        """Память заполняет неизвестное и выбирает среди кандидатов; спор со словарём — AMBIGUOUS."""
        if len(values) > 1:
            return Coordinate.ambiguous(sorted(values | set(coord.candidates)), SRC_G6)
        (value,) = values
        if coord.is_unique:
            return coord if coord.value == value else Coordinate.ambiguous(sorted({coord.value, value}), SRC_G6)
        if coord.is_ambiguous and value not in coord.candidates:
            return Coordinate.ambiguous(sorted(set(coord.candidates) | {value}), SRC_G6)
        return Coordinate.resolved(value, SRC_G6, min(h.confidence for h in hits))

    # ---- R(v, H) = v' -------------------------------------------------------------------
    def fill(self, intent: Intent, answers: dict) -> Intent:
        """Оператор заполняет координаты; исходные данные парсера в остальном сохраняются."""
        target, action, params = intent.target, intent.action, intent.params
        for key, value in answers.items():
            if key == "target":
                if not self.home.is_executable(value):
                    raise ValueError(f"target {value!r} is not registered in the home configuration")
                target = Coordinate.resolved(self.home.canonical(value), SRC_OPERATOR)
            elif key == "action":
                if value not in REAL_ACTIONS:
                    raise ValueError(f"unknown action {value!r}")
                action = Coordinate.resolved(value, SRC_OPERATOR)
            elif key == "color":
                if value not in COLORS:
                    raise ValueError(f"unknown color {value!r}")
                params = replace(params, color=value)
            elif key == "level":
                if not 0.0 <= float(value) <= 100.0:
                    raise ValueError("level must be within 0..100")
                params = replace(params, level=float(value))
            elif key == "room":
                if value not in self.home.rooms:
                    raise ValueError(f"unknown room {value!r}")
                params = replace(params, room=value)
            elif key == "device":
                params = replace(params, device=str(value))
            else:
                raise ValueError(f"unknown coordinate {key!r}")
            params = replace(params, invalid=tuple(f for f in params.invalid if f != key))
        modality, missing = self.modality.resolve(target, action, params, True)
        return replace(intent, target=target, action=action, params=params, modality=modality, missing=missing)
