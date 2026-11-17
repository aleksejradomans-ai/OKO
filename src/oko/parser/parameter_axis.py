"""Ось P: интенсивность, цвет, помещение. Обязательность параметра определяется действием."""
from __future__ import annotations

from ..home import HomeConfig
from ..space import COLORS, Params

_HARD = ("ый", "ая", "ое", "ые", "ого", "ой", "ую", "ым", "ом", "ыми", "ых", "ому")
_SOFT = ("ий", "яя", "ее", "ие", "его", "ей", "юю", "им", "ем", "ими", "их", "ему")


def adjective(lemma: str) -> tuple:
    stem, ending = lemma[:-2], lemma[-2:]
    forms = {stem + e for e in (_SOFT if ending == "ий" else _HARD)}
    forms.add(lemma)
    return tuple(sorted(forms))


COLOR_FORMS = {c: adjective(c) for c in COLORS}
LEVEL_FORMS = {
    "максимум": ("максимум", "максимума", "максимуме"),
    "минимум": ("минимум", "минимума", "минимуме"),
    "половина": ("половина", "половину", "половине"),
}
LEVEL_VALUES = {"максимум": 100.0, "минимум": 0.0, "половина": 50.0}


class ParameterAxis:
    def __init__(self, home: HomeConfig) -> None:
        self.home = home
        self.room_forms = {forms[0]: tuple(forms) for forms in home.rooms.values() if forms}
        self.room_ids = {forms[0]: room for room, forms in home.rooms.items() if forms}

    def extract(self, toks, used: set) -> tuple[Params, dict]:
        """Возвращает параметры и позиции слотов шаблона: индекс токена -> '{level}' / '{color}'."""
        levels, colors, rooms, slots = [], set(), set(), {}
        for i, t in enumerate(toks):
            if t.is_number:
                levels.append(float(t.text))
                slots[i] = "{level}"
            elif t.lemma in LEVEL_VALUES:
                levels.append(LEVEL_VALUES[t.lemma])
                slots[i] = "{level}"
            elif t.lemma in COLOR_FORMS:
                colors.add(t.lemma)
                slots[i] = "{color}"
            elif t.lemma in self.room_ids:
                rooms.add(self.room_ids[t.lemma])
            else:
                continue
            used.add(i)
        invalid = []
        level = levels[0] if len(levels) == 1 and 0.0 <= levels[0] <= 100.0 else None
        if levels and level is None:
            invalid.append("level")
        if len(colors) > 1:
            invalid.append("color")
        if len(rooms) > 1:
            invalid.append("room")
        return Params(level=level,
                      color=next(iter(colors)) if len(colors) == 1 else None,
                      room=next(iter(rooms)) if len(rooms) == 1 else None,
                      invalid=tuple(invalid)), slots
