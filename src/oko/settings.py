"""Настраиваемые пороги алгоритма (значения по умолчанию — из спецификации v1.0)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Парсер
    fuzzy_min_len: int = 4            # d_L(x, y) <= 1 только при |x| >= 4 (в спецификации 5; ревью ROLE_R2)
    max_residual: int = 3             # неполная команда с бОльшим числом неузнанных слов -> ABSTRACT
    pending_ttl_s: float = 90.0       # срок жизни запроса CLARIFY / CONFIRM
    # Кристаллизация
    # Решение Оператора 23:15 п.5 (подтверждено 11.11 10:45): одно нажатие «Запомнить связку» после
    # подтверждённого исполнения = рабочая связка. В спецификации v1.0 было 3 исполнения / 2 сессии / q >= 0.80.
    cryst_min_confirmed: int = 1      # явных «Запомнить» с результатом VERIFIED
    cryst_min_sessions: int = 1
    cryst_q: float = 0.60             # q после одного «Запомнить» = 2/3; один отказ Оператора -> 0.5 -> DEGRADED
    degrade_q: float = 0.60           # надёжность паттерна ниже -> DEGRADED
    bind_limit: int = 500             # связок Оператора всего; полна -> вопрос о вытеснении самой старой (LRU)
    examples_max: int = 3
    # Калибровка
    window: int = 20                  # N
    min_valid: int = 10               # N_min
    theta_calibration: float = 0.60   # A_N <= 0.60 -> CALIBRATION_REQUIRED
    recover_n: int = 10
    theta_recover: float = 0.80       # A_10 >= 0.80 -> RESUME
    clarify_is_miss: bool = False     # строгое чтение A_N: любое уточнение считается промахом парсера
    # Потенциальные паттерны
    alpha: float = 1.0                # сглаживание P_O(y | c)
