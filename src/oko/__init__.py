"""ОКО — «Алгоритм винегрета»: Intent Vector Space & Adaptive Pattern Memory (спецификация v1.0).

Три независимых контура:
  распознавание (parser/)  — детерминированно определяет команду, без LLM;
  исполнение (execution/)  — отправляет команду и сверяет фактическое состояние устройства;
  обучение (memory/)       — закрепляет только подтверждённые Оператором и проверенные связи.
Над ними — калибровка (calibration/): A_N <= 0.60 -> FREEZE -> CALIBRATE -> VALIDATE -> RESUME.
Журнал событий и память G6 лежат в SQLite (registry/). Реальные устройства сюда не подключены:
исполнительный контур работает через протокол DeviceAdapter.
"""
from .home import HomeConfig
from .pipeline import Outcome, Vinaigrette
from .settings import Settings
from .space import Coordinate, Intent, Params, VectorSpace

__all__ = ["Coordinate", "HomeConfig", "Intent", "Outcome", "Params", "Settings", "VectorSpace", "Vinaigrette"]
