"""path_guard_decorators.py — декоратор для функций, принимающих путь как
один из аргументов.

Реализован поверх path_guard.guard_path — не заменяет его, не дублирует
логику резолвинга/сравнения путей (см. историю решений: предложение
заменить это новым "governor.py" с наивной startswith-проверкой было
отклонено — тот код повторял уже найденные и исправленные дефекты,
включая ложное срабатывание C:\\WindowsApps на C:\\Windows через
строковый префикс, не структурную проверку иерархии).

allowed_root ОБЯЗАТЕЛЕН, не опционален с дефолтом на config.BASE_DIR —
найдено при тестировании первой версии этого модуля: если allowed_root
не передан явно, guard_path() лениво импортирует config.py, который
требует TELEGRAM_TOKEN и завершает процесс через sys.exit(1), если его
нет в окружении. Это означает функция, обёрнутая декоратором без явного
allowed_root, могла бы УБИТЬ ВЕСЬ ПРОЦЕСС при вызове в контексте без
TELEGRAM_TOKEN (например, в тесте, не настроившем окружение) — не просто
отклонить операцию. Подтверждено реальным тестом при разработке. Требуя
allowed_root явно, мы переносим это решение на сайт вызова декоратора,
где оно очевидно из контекста (что именно эта функция должна защищать),
не оставляем скрытым дефолтом, который сработает по-разному в зависимости
от того, настроено ли окружение.
"""
from __future__ import annotations

import functools
import inspect
from pathlib import Path
from typing import Callable

from src.path_guard import SecurityAccessViolation, guard_path

__all__ = ["secure_path_access", "SecurityAccessViolation"]


def secure_path_access(arg_name: str, allowed_root: Path) -> Callable:
    """Декоратор: проверяет аргумент arg_name декорируемой функции через
    guard_path(value, allowed_root=allowed_root) перед вызовом самой
    функции.

    allowed_root обязателен (см. docstring модуля) — нет дефолта на
    config.BASE_DIR ни явного, ни неявного.

    Если arg_name не найден среди параметров функции (опечатка в имени)
    — ValueError бросается СРАЗУ при декорировании (на этапе импорта
    модуля с декорированной функцией), не при первом вызове — это
    позволяет поймать ошибку конфигурации декоратора до того, как
    функция реально понадобится в рантайме.
    """

    def decorator(func: Callable) -> Callable:
        sig = inspect.signature(func)
        if arg_name not in sig.parameters:
            raise ValueError(
                f"@secure_path_access('{arg_name}', ...) applied to {func.__name__}, "
                f"but it has no parameter named '{arg_name}'. "
                f"Available parameters: {list(sig.parameters)}"
            )

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()

            path_to_check = bound.arguments[arg_name]
            guard_path(path_to_check, allowed_root=allowed_root)

            return func(*args, **kwargs)

        return wrapper

    return decorator
