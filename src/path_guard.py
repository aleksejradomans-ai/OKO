"""Защита от выхода за пределы выделенного каталога данных.

Два механизма, не один:
1. БЕЛЫЙ список (основной): путь обязан быть внутри BASE_DIR. Это
   структурно надёжнее чёрного списка — не нужно перечислять все опасные
   директории Windows, достаточно знать одну границу, внутри которой
   агенту разрешено работать.
2. ЧЁРНЫЙ список FORBIDDEN_PATHS (defense-in-depth): срабатывает ДОПОЛНИТЕЛЬНО,
   на случай если BASE_DIR когда-нибудь будет расширен до чего-то широкого
   (например, по ошибке конфигурации AI_BASE_DIR=C:\\) — тогда белый список
   сам по себе пропустил бы запись в C:\\Windows, а чёрный список остановит.

Оба списка проверяются после Path.resolve() — не до. Проверка нерезолвленного
пути бессмысленна: ".." и симлинки позволяют обойти любую строковую
проверку, выполненную до резолвинга.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional


class SecurityAccessViolation(Exception):
    """Попытка доступа к пути за пределами разрешённой зоны."""


# Известные системные директории Windows, доступ к которым запрещён
# безусловно, даже если по ошибке окажутся технически "внутри" BASE_DIR
# (что само по себе уже сигнал неверной конфигурации, а не штатный сценарий).
# Список заведомо неполный — он не основной механизм защиты (см. docstring
# модуля), а второй барьер поверх белого списка.
FORBIDDEN_PATHS: tuple[Path, ...] = (
    Path(r"C:\Windows"),
    Path(r"C:\Program Files"),
    Path(r"C:\Program Files (x86)"),
    Path(r"C:\ProgramData"),
    Path(r"C:\System Volume Information"),
)


def _is_within(path: Path, parent: Path) -> bool:
    """True если path равен parent или лежит внутри него.
    Path.is_relative_to() появился в Python 3.9 — используем явную
    реализацию через relative_to для совместимости и предсказуемости
    поведения на обеих платформах (Windows-сравнение регистронезависимо
    на уровне ОС, но Path.resolve() уже нормализует регистр диска).
    """
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def guard_path(candidate: Path | str, allowed_root: Optional[Path] = None) -> Path:
    """Резолвит candidate и проверяет его против белого + чёрного списка.

    Возвращает резолвленный Path при успехе. Бросает SecurityAccessViolation
    при попытке выйти за пределы allowed_root (по умолчанию config.BASE_DIR)
    или при совпадении с FORBIDDEN_PATHS.

    allowed_root параметризован (не всегда жёстко config.BASE_DIR), потому
    что некоторые легитимные пути в системе лежат ВНЕ BASE_DIR намеренно —
    например, run_system.py резолвит путь к knowledge_indexer.py через
    os.path.dirname(os.path.abspath(__file__)), это src/, не data/. Вызывающий
    код должен явно указать, какая граница применима к конкретной операции,
    а не предполагать, что для агента есть только одна священная директория.

    config импортируется ЛЕНИВО, внутри функции, не на уровне модуля —
    это сознательное архитектурное решение, не недосмотр. config.py при
    импорте требует TELEGRAM_TOKEN и завершает процесс через sys.exit(1)
    при его отсутствии (см. _require_env). Если path_guard.py импортирует
    config жёстко на верхнем уровне, любой unit-тест ЭТОГО модуля — даже
    тест, не использующий config.BASE_DIR вообще (allowed_root передан
    явно) — требовал бы валидного TELEGRAM_TOKEN в окружении. Это случайная
    связанность, не отражающая реальную потребность данной функции.
    """
    if allowed_root is None:
        import config
        allowed_root = config.BASE_DIR

    resolved = Path(candidate).resolve()
    # allowed_root ДОЛЖЕН резолвиться той же функцией, что и candidate — иначе
    # на Windows возможно сравнение двух текстовых представлений одного и того
    # же физического пути (короткое 8.3-имя vs развёрнутое длинное имя),
    # которое relative_to() воспринимает как два разных пути и ложно
    # отклоняет легитимный запрос. Подтверждено реальным сбоем: tempfile
    # возвращает путь в одной форме, .resolve() на существующей части этого
    # же пути разворачивает его в другую форму — обе стороны сравнения
    # обязаны пройти через resolve() одинаково, не только candidate.
    allowed_root = Path(allowed_root).resolve()

    for forbidden in FORBIDDEN_PATHS:
        if _is_within(resolved, forbidden):
            raise SecurityAccessViolation(
                f"Path {resolved} falls within forbidden system directory {forbidden}"
            )

    if not _is_within(resolved, allowed_root):
        raise SecurityAccessViolation(
            f"Path {resolved} is outside allowed root {allowed_root}"
        )

    return resolved
