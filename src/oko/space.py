"""Пространство намерений.

Уровень 1 — дискретное пространство команд C = T x A x P x M (символы, без сложения).
Уровень 2 — числовое представление Phi: C -> R^d: one-hot по категориальным координатам,
нормализованные числа, отдельный признак отсутствия. Склейка координат — конкатенация.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

UNKNOWN = "UNKNOWN"
AMBIGUOUS = "AMBIGUOUS"

TARGETS = ("tv_amb", "tv_screen", "lamps", "audio", "light_any", UNKNOWN, AMBIGUOUS)
ACTIONS = ("on", "off", "up", "down", "color", "status", UNKNOWN, AMBIGUOUS)
MODALITIES = ("EXEC", "CLARIFY", "BIND", "ABSTRACT")
EXEC, CLARIFY, BIND, ABSTRACT = MODALITIES

REAL_TARGETS = TARGETS[:5]
REAL_ACTIONS = ACTIONS[:6]
LIGHT_TARGETS = ("lamps", "tv_amb")
COLORS = ("красный", "оранжевый", "желтый", "зеленый", "бирюзовый", "голубой", "синий",
          "фиолетовый", "сиреневый", "розовый", "белый", "теплый", "холодный")

# Источник определения координаты
SRC_NONE = "NONE"
SRC_BUILTIN = "BUILTIN"      # точное словарное совпадение
SRC_FUZZY = "FUZZY"          # единственный кандидат на расстоянии Левенштейна 1
SRC_G6 = "G6"                # кристаллизованный псевдоним Оператора
SRC_CONFIG = "CONFIG"        # зарегистрированная группа устройств
SRC_CONTEXT = "CONTEXT"      # активное устройство из контекста
SRC_OPERATOR = "OPERATOR"    # ответ Оператора на уточнение
SRC_DEFAULT = "DEFAULT"      # системное значение по умолчанию


@dataclass(frozen=True)
class Coordinate:
    """Значение координаты + состояние определения + источник + уверенность."""
    value: str = UNKNOWN
    source: str = SRC_NONE
    confidence: float = 0.0
    candidates: tuple[str, ...] = ()

    @property
    def is_unknown(self) -> bool:
        return self.value == UNKNOWN

    @property
    def is_ambiguous(self) -> bool:
        return self.value == AMBIGUOUS

    @property
    def is_unique(self) -> bool:
        return self.value not in (UNKNOWN, AMBIGUOUS)

    @property
    def state(self) -> str:
        return "RESOLVED" if self.is_unique else self.value

    @classmethod
    def resolved(cls, value: str, source: str, confidence: float = 1.0) -> "Coordinate":
        return cls(value, source, confidence, (value,))

    @classmethod
    def ambiguous(cls, candidates, source: str = SRC_BUILTIN) -> "Coordinate":
        return cls(AMBIGUOUS, source, 0.0, tuple(candidates))


@dataclass(frozen=True)
class Params:
    """P = P_L x P_C x P_R x P_D. None — параметр не задан; invalid — задан, но непригоден."""
    level: Optional[float] = None      # 0..100: уровень либо величина изменения для up/down
    color: Optional[str] = None
    room: Optional[str] = None
    device: Optional[str] = None
    invalid: tuple[str, ...] = ()

    def schema(self) -> dict:
        """Структура параметров: уровень и цвет — переменные (слоты), помещение и устройство —
        значения: они меняют целевую координату и паттерны по ним не объединяются."""
        s: dict = {}
        if self.level is not None:
            s["level"] = "slot"
        if self.color is not None:
            s["color"] = "slot"
        if self.room is not None:
            s["room"] = self.room
        if self.device is not None:
            s["device"] = self.device
        return s


@dataclass(frozen=True)
class Intent:
    raw: str
    normalized: str
    template: str
    target: Coordinate = field(default_factory=Coordinate)
    action: Coordinate = field(default_factory=Coordinate)
    params: Params = field(default_factory=Params)
    modality: str = ABSTRACT
    missing: tuple[str, ...] = ()        # что должен дозаполнить Оператор
    bind_payload: str = ""

    def vector(self) -> tuple:
        """(t, a, schema(p)) — то, что сравнивается с подтверждённым вектором y*."""
        return (self.target.value, self.action.value, tuple(sorted(self.params.schema().items())))

    def describe(self) -> dict:
        return {
            "target": self.target.value, "target_source": self.target.source,
            "action": self.action.value, "action_source": self.action.source,
            "params": self.params.schema(), "modality": self.modality, "missing": list(self.missing),
            "template": self.template,
        }


class VectorSpace:
    """Phi: C -> R^d. Размерность зависит от конфигурации дома (помещения, устройства)."""

    def __init__(self, rooms=(), devices=()) -> None:
        self.rooms = tuple(rooms)
        self.devices = tuple(devices)

    @property
    def dim(self) -> int:
        return (len(TARGETS) + len(ACTIONS) + 2 + (len(COLORS) + 1)
                + (len(self.rooms) + 1) + (len(self.devices) + 1) + len(MODALITIES))

    @staticmethod
    def _one_hot(value, values) -> list[float]:
        if value not in values:
            raise ValueError(f"value {value!r} is outside the axis {values!r}")
        return [1.0 if v == value else 0.0 for v in values]

    @classmethod
    def _optional(cls, value, values) -> list[float]:
        """one-hot с отдельным последним признаком «не задано»."""
        if value is None:
            return [0.0] * len(values) + [1.0]
        return cls._one_hot(value, values) + [0.0]

    def encode(self, intent: Intent) -> list[float]:
        p = intent.params
        level = [0.0, 1.0] if p.level is None else [max(0.0, min(1.0, p.level / 100.0)), 0.0]
        return (self._one_hot(intent.target.value, TARGETS)
                + self._one_hot(intent.action.value, ACTIONS)
                + level
                + self._optional(p.color, COLORS)
                + self._optional(p.room, self.rooms)
                + self._optional(p.device, self.devices)
                + self._one_hot(intent.modality, MODALITIES))
