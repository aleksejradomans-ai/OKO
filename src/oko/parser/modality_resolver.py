"""Автомат состояний: (координаты, командный маркер, конфигурация дома) -> модальность.

BIND разрешается раньше, в VectorParser (маркер сохранения стоит первым словом).
"""
from __future__ import annotations

from ..home import HomeConfig
from ..space import ABSTRACT, CLARIFY, EXEC, Coordinate, Params


class ModalityResolver:
    def __init__(self, home: HomeConfig) -> None:
        self.home = home

    def resolve(self, target: Coordinate, action: Coordinate, params: Params, cue: bool) -> tuple[str, tuple]:
        if target.is_unknown and action.is_unknown:
            return ABSTRACT, ()
        if not cue:
            return ABSTRACT, ()
        missing = []
        if not action.is_unique:
            missing.append("action")
        if not target.is_unique or not self.home.is_executable(target.value):
            missing.append("target")
        elif action.is_unique and not self.home.supports(target.value, action.value):
            missing.append("target")     # устройство зарегистрировано, но этого действия не умеет
        if action.value == "color" and params.color is None and self.home.default_color is None:
            missing.append("color")
        missing += [f for f in params.invalid if f not in missing]
        return (CLARIFY, tuple(missing)) if missing else (EXEC, ())
