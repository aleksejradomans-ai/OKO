"""CommandRouter: передаёт разрешённую команду адаптеру и сверяет результат: Success = Verify(R, E).

VERIFIED     — фактическое состояние устройства соответствует команде;
ACKNOWLEDGED — адаптер принял команду, но телеметрии нет: доказательством исполнения не считается;
MISMATCH     — телеметрия противоречит команде;
FAILED       — адаптер сообщил о сбое.
MISMATCH и FAILED — инциденты инфраструктуры, а не ошибки парсера.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..home import HomeConfig
from ..space import EXEC, Intent
from .device_adapter import DeviceAdapter, DeviceError

VERIFIED, ACKNOWLEDGED, MISMATCH, FAILED = "VERIFIED", "ACKNOWLEDGED", "MISMATCH", "FAILED"
_SEVERITY = {VERIFIED: 0, ACKNOWLEDGED: 1, MISMATCH: 2, FAILED: 3}


@dataclass(frozen=True)
class Command:
    target: str
    action: str
    level: Optional[float] = None
    color: Optional[str] = None
    room: Optional[str] = None
    device: Optional[str] = None
    needs_confirm: bool = False      # опасная команда: без подтверждения Оператора не исполняется


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    detail: str = ""
    state: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.status in (VERIFIED, ACKNOWLEDGED)


def verify(command: Command, before: Optional[dict], after: Optional[dict]) -> bool:
    """Verify(R, E): соответствует ли фактическое состояние разрешённой команде."""
    a, level = command.action, command.level
    if a == "status":
        return True
    if a == "on":
        return (after.get("power") is True
                and (level is None or after.get("level") == level)
                and (command.color is None or after.get("color") == command.color))
    if a == "off":
        return after.get("power") is False
    if a == "color":
        return after.get("color") == command.color
    if before is None:
        return False
    if a == "up":
        return after["level"] > before["level"] or after["level"] >= 100.0
    return after["level"] < before["level"] or after["level"] <= 0.0


class CommandRouter:
    def __init__(self, home: HomeConfig, adapter: DeviceAdapter) -> None:
        self.home = home
        self.adapter = adapter

    def build(self, intent: Intent) -> Command:
        """Инвариант 1: команда с незаполненными координатами до адаптера не доходит."""
        if intent.modality != EXEC or intent.missing or not intent.target.is_unique or not intent.action.is_unique:
            raise ValueError("intent is not executable: coordinates are missing or ambiguous")
        p, action = intent.params, intent.action.value
        level, color = p.level, p.color
        if action in ("up", "down") and level is None:
            level = self.home.default_step
        if action == "color" and color is None:
            color = self.home.default_color
        return Command(intent.target.value, action, level, color, p.room, p.device,
                       self.home.needs_confirm(intent.target.value, action))

    def execute(self, command: Command, confirmed: bool = False) -> ExecutionResult:
        if command.needs_confirm and not confirmed:
            raise PermissionError(f"{command.target}/{command.action} requires the Operator's confirmation")
        members = self.home.members(command.target)
        if not members or not self.home.supports(command.target, command.action):
            raise ValueError(f"{command.target}/{command.action} is not executable in this home")
        results = [self._one(m, command) for m in members]
        return max(results, key=lambda r: _SEVERITY[r.status])

    def _one(self, device: str, command: Command) -> ExecutionResult:
        try:
            before = self.adapter.read_state(device)
            if command.action != "status":
                self.adapter.send(device, command.action, command.level, command.color)
            after = self.adapter.read_state(device)
        except DeviceError as exc:
            return ExecutionResult(FAILED, f"{device}: {exc}")
        if after is None:
            return ExecutionResult(ACKNOWLEDGED, f"{device}: no telemetry")
        if verify(command, before, after):
            return ExecutionResult(VERIFIED, device, after)
        return ExecutionResult(MISMATCH, f"{device}: state does not match the command", after)
