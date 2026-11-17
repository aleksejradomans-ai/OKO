"""Фактическая конфигурация дома: исполнимость команды определяется ею, а не догадками модели."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .space import REAL_ACTIONS

_ALL = frozenset(REAL_ACTIONS)
_NO_COLOR = _ALL - {"color"}


def _default_devices() -> dict:
    return {"tv_amb": _ALL, "tv_screen": _NO_COLOR, "lamps": _ALL, "audio": _NO_COLOR}


@dataclass(frozen=True)
class HomeConfig:
    # зарегистрированное устройство -> поддерживаемые действия
    devices: dict = field(default_factory=_default_devices)
    # зарегистрированные группы, например {"light_any": ("lamps",)} — группа ALL_LIGHTS
    groups: dict = field(default_factory=dict)
    # помещение -> словоформы, например {"bedroom": ("спальня", "спальне", "спальню")}
    rooms: dict = field(default_factory=dict)
    default_step: float = 10.0            # шаг up/down, если величина не названа
    default_color: Optional[str] = None   # цвет по умолчанию для действия color
    # опасные команды (цель, действие): исполняются только после явного подтверждения Оператора,
    # даже если паттерн кристаллизован. По умолчанию — выключение телевизора (standby).
    confirm_required: frozenset = frozenset({("tv_screen", "off")})

    def needs_confirm(self, target: str, action: str) -> bool:
        return any((m, action) in self.confirm_required for m in self.members(target) or (target,))

    def canonical(self, target: str) -> str:
        """Группа из одного устройства — это само устройство (свет = лампы)."""
        members = self.groups.get(target, ())
        return members[0] if len(members) == 1 else target

    def members(self, target: str) -> tuple:
        if target in self.devices:
            return (target,)
        return tuple(m for m in self.groups.get(target, ()) if m in self.devices)

    def is_executable(self, target: str) -> bool:
        return bool(self.members(target))

    def supports(self, target: str, action: str) -> bool:
        members = self.members(target)
        return bool(members) and all(action in self.devices[m] for m in members)

    def targets_for(self, action: Optional[str] = None) -> tuple:
        names = list(self.devices) + [g for g in self.groups if self.canonical(g) == g and self.members(g)]
        return tuple(t for t in names if action is None or self.supports(t, action))

    def actions_for(self, target: str) -> tuple:
        return tuple(a for a in REAL_ACTIONS if self.supports(target, a))
