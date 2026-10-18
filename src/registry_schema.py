"""Схема допустимых значений для Registry.state.

Отдельно от ALLOWED_STATE_KEYS в registry.py: тот барьер фильтрует ключи
(защита от опечаток в именах и от записи в несуществующие поля), этот —
фильтрует значения (защита от опечаток в данных, например "READYY"
вместо "READY", или строки вместо числа).

Не каждое поле нуждается в схеме. last_error принимает произвольный текст
исключения — для него схема ограничивается типом (str | None), не набором
констант, потому что набора констант не существует.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Union


class StatusEnum(str, Enum):
    IDLE = "IDLE"
    READY = "READY"
    BUSY = "BUSY"
    ERROR = "ERROR"


class NarrativeStateEnum(str, Enum):
    FACTUAL_MODE = "FACTUAL_MODE"
    REFLECTIVE_MODE = "REFLECTIVE_MODE"


class ComplianceStatusEnum(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCHECKED = "UNCHECKED"
    HALT = "HALT"  # новое — см. Приоритет №2, Вердикт 2.0 (16.07.2026):
    # критическая остановка по HALT_CONDITION, без попытки self-correct.
    # Без этого значения update_state("last_compliance_status", "HALT")
    # в governor.py._handle_halt_condition молча отклонялся бы валидатором.
    PASS_AFTER_CORRECTION = "PASS_AFTER_CORRECTION"
    FAIL_AFTER_CORRECTION = "FAIL_AFTER_CORRECTION"
    UNCHECKED_AFTER_CORRECTION = "UNCHECKED_AFTER_CORRECTION"


class AlertStateEnum(str, Enum):
    NORMAL = "NORMAL"
    ALARM = "ALARM"


# Обязательные ключи внутри каждой записи pending_tasks[uuid]. Облегчённая
# проверка структуры — НЕ полная схема (не проверяет типы значений внутри,
# например что timestamp реально float, не строка) — для пяти полей с
# разными типами полноценный вложенный валидатор был бы избыточен
# относительно того, что эти данные защищают. Решение: лучше частичная
# защита от опечаток в ИМЕНАХ полей (тот же класс ошибки, что вся эта
# схема и существует отлавливать), чем ничего, и не тащить сюда Pydantic
# ради одной структуры.
PENDING_TASK_REQUIRED_KEYS = frozenset({
    "type", "original_path", "staging_path", "hash", "timestamp",
})


def _validate_pending_tasks(value: Any) -> tuple[bool, str]:
    if not isinstance(value, dict):
        return False, f"pending_tasks must be a dict, got {type(value).__name__}"

    for task_uuid, task_data in value.items():
        if not isinstance(task_data, dict):
            return False, f"pending_tasks[{task_uuid!r}] must be a dict, got {type(task_data).__name__}"
        missing = PENDING_TASK_REQUIRED_KEYS - task_data.keys()
        if missing:
            return False, f"pending_tasks[{task_uuid!r}] missing required keys: {sorted(missing)}"

    return True, "ok"


# Значение в схеме — это либо подкласс Enum (проверка "значение входит в набор
# констант"), либо кортеж типов для isinstance (проверка типа, без ограничения
# набора значений), либо callable(value) -> (bool, str) для случаев, требующих
# структурной, а не просто типовой проверки (см. _validate_pending_tasks).
# last_compliance_status дополнительно разрешает None — это начальное
# значение до первой проверки compliance.
STATE_SCHEMA: dict[str, Any] = {
    "status": StatusEnum,
    "narrative_state": NarrativeStateEnum,
    "current_task": (str,),
    "last_error": (str, type(None)),
    "reflection_depth": (int,),
    "compliance_violations": (int,),
    "compliance_check_failures": (int,),
    "total_requests": (int,),
    "last_compliance_status": (ComplianceStatusEnum, type(None)),
    "uptime": (int,),
    "input_blocked_count": (int,),
    "last_alert_state_compliance": AlertStateEnum,
    "last_alert_state_input": AlertStateEnum,
    "pending_tasks": _validate_pending_tasks,
    # PATCH_V2_STAGNATION
    "transport_failures": (int,),
    "stagnation_breaks": (int,),
    "last_stagnation_reason": (str, type(None)),
}


def validate(key: str, value: Any) -> tuple[bool, str]:
    """Проверяет value против схемы для key.
    Возвращает (ok, detail). Если key не описан в схеме — пропускает (ok=True),
    т.к. ALLOWED_STATE_KEYS в registry.py уже отфильтровал неизвестные ключи;
    эта функция не дублирует ту проверку, только проверяет значения известных.
    """
    validator = STATE_SCHEMA.get(key)
    if validator is None:
        return True, "no schema defined for key, value accepted as-is"

    if isinstance(validator, type) and issubclass(validator, Enum):
        try:
            validator(value)
            return True, "ok"
        except ValueError:
            allowed = [e.value for e in validator]
            return False, f"value {value!r} not in allowed set {allowed}"

    if isinstance(validator, tuple):
        # last_compliance_status хранит как ComplianceStatusEnum, так и None —
        # для Enum-членов сравниваем по значению, не строго isinstance(Enum).
        for t in validator:
            if isinstance(t, type) and issubclass(t, Enum):
                try:
                    t(value)
                    return True, "ok"
                except ValueError:
                    continue
            elif isinstance(value, t):
                return True, "ok"
        type_names = [t.__name__ for t in validator]
        return False, f"value {value!r} ({type(value).__name__}) does not match expected types {type_names}"

    if callable(validator):
        # Структурные валидаторы вроде _validate_pending_tasks — проверяют
        # не просто тип, а форму данных (наличие обязательных ключей и т.п.).
        # Отдельная ветка от tuple/Enum, т.к. функция сама возвращает
        # (bool, str), не бросает исключение и не сравнивается через isinstance.
        return validator(value)

    return True, "unrecognized schema entry, value accepted as-is"
