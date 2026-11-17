"""DeviceAdapter: отправка команды и чтение фактического состояния устройства.

Реальные устройства сюда пока не подключены. InMemoryDeviceAdapter — эталонная модель
для приёмочных испытаний: умеет отказывать, молчать о состоянии и «принимать, но не делать».
"""
from __future__ import annotations

from typing import Optional, Protocol


class DeviceError(Exception):
    """Сбой исполнительного контура: устройство недоступно либо отвергло команду."""


class DeviceAdapter(Protocol):
    def send(self, target: str, action: str, level: Optional[float], color: Optional[str]) -> None:
        """Отправляет команду. Сбой — DeviceError."""

    def read_state(self, target: str) -> Optional[dict]:
        """Фактическое состояние {"power": bool, "level": float, "color": str|None};
        None — устройство не даёт обратной телеметрии."""


class InMemoryDeviceAdapter:
    def __init__(self, targets=("tv_amb", "tv_screen", "lamps", "audio")) -> None:
        self.state = {t: {"power": False, "level": 50.0, "color": None} for t in targets}
        self.unreachable: set = set()      # send -> DeviceError
        self.no_telemetry: set = set()     # read_state -> None
        self.stuck: set = set()            # команда принята, состояние не меняется
        self.sent: list = []

    def send(self, target, action, level, color) -> None:
        if target in self.unreachable or target not in self.state:
            raise DeviceError(f"{target}: unreachable")
        self.sent.append((target, action, level, color))
        if target in self.stuck:
            return
        st = self.state[target]
        if action == "on":
            st["power"] = True
            if level is not None:
                st["level"] = level
        elif action == "off":
            st["power"] = False
        elif action in ("up", "down"):
            delta = level if action == "up" else -level
            st["level"] = max(0.0, min(100.0, st["level"] + delta))
        if color is not None and action in ("on", "color"):
            st["color"] = color

    def read_state(self, target):
        if target in self.no_telemetry or target not in self.state:
            return None
        return dict(self.state[target])
