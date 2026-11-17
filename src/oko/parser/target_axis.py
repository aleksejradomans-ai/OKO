"""Ось T: целевое устройство."""
from __future__ import annotations

from typing import Optional

from ..home import HomeConfig
from .parameter_axis import adjective
from ..space import (LIGHT_TARGETS, SRC_BUILTIN, SRC_CONFIG, SRC_CONTEXT, SRC_FUZZY, Coordinate)

# «Лампы» в этом доме — и светодиоды подсветки ТВ, и лампы на потолочной штанге:
# голое слово цель не определяет, пока Оператор не уточнит.
LAMP_ANY = "lamp_any"

FORMS = {
    "подсветка": ("подсветка", "подсветку", "подсветки", "подсветке", "подсветкой"),
    "амбилайт": ("амбилайт", "амбилайта", "амбилайту", "амбилайтом", "ambilight"),
    "телевизор": ("телевизор", "телевизора", "телевизору", "телевизором", "телевизоре", "тв", "tv"),
    "экран": ("экран", "экрана", "экрану", "экраном", "экране"),
    "лампа": ("лампа", "лампу", "лампы", "лампе", "лампой", "ламп", "лампам", "лампами", "лампах",
              "лампочка", "лампочку", "лампочки", "лампочек"),
    "люстра": ("люстра", "люстру", "люстры", "люстре", "люстрой"),
    "потолок": ("потолок", "потолка", "потолку", "потолком", "потолке"),
    "потолочный": adjective("потолочный"),
    "штанга": ("штанга", "штангу", "штанги", "штанге", "штангой"),
    "планка": ("планка", "планку", "планки", "планке", "планкой"),
    "верхний": adjective("верхний"),
    "звук": ("звук", "звука", "звуку", "звуком"),
    "музыка": ("музыка", "музыку", "музыки", "музыке", "музыкой"),
    "колонка": ("колонка", "колонку", "колонки", "колонке", "колонкой", "колонок"),
    "аудио": ("аудио", "audio"),
    "громкость": ("громкость", "громкости", "громкостью"),
    "свет": ("свет", "света", "свету", "светом", "свете"),
    "освещение": ("освещение", "освещения", "освещению", "освещением"),
}

PHRASES = {
    ("подсветка", "телевизор"): "tv_amb", ("подсветка", "экран"): "tv_amb",
    ("подсветка",): "tv_amb", ("амбилайт",): "tv_amb",
    ("телевизор",): "tv_screen", ("экран",): "tv_screen",
    ("лампа",): LAMP_ANY,
    ("люстра",): "lamps", ("потолок",): "lamps", ("потолочный",): "lamps", ("штанга",): "lamps",
    ("планка",): "lamps", ("верхний", "свет"): "lamps",
    ("звук",): "audio", ("музыка",): "audio", ("колонка",): "audio", ("аудио",): "audio", ("громкость",): "audio",
    ("свет",): "light_any", ("освещение",): "light_any",
}


def expand(values) -> set:
    """Раскрывает внутреннюю метку «лампы» в реальные цели."""
    out = set(values)
    if LAMP_ANY in out:
        out.discard(LAMP_ANY)
        out.update(LIGHT_TARGETS)
    return out


class TargetAxis:
    def __init__(self, home: HomeConfig) -> None:
        self.home = home

    def resolve(self, found: dict, context: Optional[dict] = None) -> Coordinate:
        """found: значение -> True, если оно получено только нечётким сопоставлением."""
        values = dict(found)
        if any(v in LIGHT_TARGETS for v in values):
            values.pop("light_any", None)    # «свет» и «лампы» поглощаются названным источником света
            values.pop(LAMP_ANY, None)
        if LAMP_ANY in values:
            values.pop("light_any", None)
            lights = [t for t in LIGHT_TARGETS if t in self.home.devices]
            conf = 0.8 if values.pop(LAMP_ANY) else 1.0
            if not values and len(lights) == 1:
                return Coordinate.resolved(lights[0], SRC_CONFIG, conf)    # в доме один источник света
            return Coordinate.ambiguous(sorted(set(values) | set(lights)))
        if len(values) > 1:
            return Coordinate.ambiguous(sorted(values))
        if not values:
            active = (context or {}).get("active_target")
            if active and self.home.is_executable(active):
                return Coordinate.resolved(self.home.canonical(active), SRC_CONTEXT, 0.6)
            return Coordinate()
        (value, fuzzy), = values.items()
        source, conf = (SRC_FUZZY, 0.8) if fuzzy else (SRC_BUILTIN, 1.0)
        if value == "light_any":
            if not self.home.is_executable(value):
                # группы ALL_LIGHTS нет: исполнять нечего, нужен выбор Оператора
                return Coordinate.ambiguous([t for t in LIGHT_TARGETS if t in self.home.devices], source)
            if self.home.canonical(value) != value:
                return Coordinate.resolved(self.home.canonical(value), SRC_CONFIG, conf)
        return Coordinate.resolved(value, source, conf)
