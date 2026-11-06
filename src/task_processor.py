"""task_processor.py — read-only генератор планов и черновиков патчей.

Архитектурный инвариант (см. историю решений): TaskProcessor — это
Generator, не Executor. Он НЕ имеет импортов patch_applier, snapshot_manager,
sandbox_environment — физически не может вызвать apply(). Он не знает о
существовании PatchApplier. Выходные данные (plan.json, draft_*.py) — это
файлы на диске, которые оператор САМ передаёт в уже существующий
cli_patch.py через ручной ввод команды в терминале. Это и есть Zero-
Integration: между генерацией черновика и его применением всегда есть
минимум один акт осознанного человеческого действия (набор команды), что
делает "approval fatigue" (слепое одобрение серии шагов) структурно
затруднённым, не просто декларируемым как принцип.

Read-only доступ к коду проекта ограничен явным whitelist (task_processor_
config.py): только .py/.md/.txt файлы внутри src/ и tests/, не .env/.json/
.yaml — форматы, где исторически хранятся секреты. Доступ на запись/
удаление отсутствует в самом коде модуля (нет вызовов open(..., "w")
к файлам проекта — только запись plan.json/draft_*.py в отдельную,
не-Sacred директорию вывода).

Промпт-конструкция использует prompt_safety.wrap_untrusted — тот же
механизм защиты от разрыва XML-границ, что governor.py, не отдельная,
менее проверенная копия (см. историю решений: "отношение к TaskProcessor
как к простому редактору — это уязвимость").
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import prompt_safety
import task_processor_config as tpc
from src.path_guard import SecurityAccessViolation, guard_path

# Стандартный logging, не oko_logger (logger.py) — сознательное решение,
# не недосмотр. TaskProcessor изолирован от остального проекта по
# соглашению ("не имеет доступа к реестру проекта" — см. историю решений);
# хотя logger.py легковесен и не тянет config, использование его создавало
# бы зависимость TaskProcessor от внутреннего соглашения проекта об имени
# модуля. Если процесс, который импортирует task_processor.py, уже вызвал
# oko_logger.setup_logging() — формат всё равно будет единообразным
# (root logger общий), просто сам этот модуль не предполагает, что вызывающий
# код обязательно использует нашу инфраструктуру логирования.
logger = logging.getLogger("task_processor")


class TaskProcessorAccessDenied(Exception):
    """Попытка прочитать файл за пределами whitelist (расширение или
    директория) — отдельное от SecurityAccessViolation имя, потому что
    семантика чуть иная: это не "путь вне allowed_root" в смысле
    path_guard (хотя использует тот же механизм под капотом), это
    "путь не входит в whitelist для READ-ONLY анализа", специфичный
    для TaskProcessor, не общий для всего проекта."""


def read_project_file(project_root: Path, relative_path: str) -> str:
    """Читает файл из whitelist-директорий проекта. Логирует каждую
    попытку с явным статусом Разрешено/Отказано (см. историю решений —
    оператор должен видеть, не пытается ли модель "лазать" туда, куда
    не следует, просто потому что её попросили проанализировать весь
    проект целиком).

    Бросает TaskProcessorAccessDenied, если расширение не в
    ALLOWED_EXTENSIONS или директория не в ALLOWED_READ_DIRS —
    отдельно от SecurityAccessViolation, который бросает guard_path
    при выходе за пределы project_root вообще (более фундаментальная
    защита, всегда активна как defense-in-depth под этим whitelist'ом).
    """
    candidate = project_root / relative_path

    try:
        validated = guard_path(candidate, allowed_root=project_root)
    except SecurityAccessViolation as e:
        logger.warning("Запрошено чтение %s -> Статус: Отказано (path_guard): %s", relative_path, e)
        raise

    if validated.suffix not in tpc.ALLOWED_EXTENSIONS:
        logger.warning(
            "Запрошено чтение %s -> Статус: Отказано (расширение %s не в whitelist)",
            relative_path, validated.suffix,
        )
        raise TaskProcessorAccessDenied(
            f"Extension {validated.suffix} not in whitelist {sorted(tpc.ALLOWED_EXTENSIONS)}"
        )

    is_in_allowed_dir = any(
        _is_within(validated, project_root / allowed_dir)
        for allowed_dir in tpc.ALLOWED_READ_DIRS
    )
    if not is_in_allowed_dir:
        logger.warning(
            "Запрошено чтение %s -> Статус: Отказано (директория не в whitelist %s)",
            relative_path, tpc.ALLOWED_READ_DIRS,
        )
        raise TaskProcessorAccessDenied(
            f"Path {validated} is not inside any of {tpc.ALLOWED_READ_DIRS}"
        )

    logger.info("Запрошено чтение %s -> Статус: Разрешено", relative_path)
    return validated.read_text(encoding="utf-8")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def build_task_prompt(task_description: str, file_contents: Optional[dict] = None) -> str:
    """Строит промпт для LLM-вызова декомпозиции задачи в план.

    task_description и содержимое каждого файла оборачиваются через
    prompt_safety.wrap_untrusted — тот же механизм, что governor.py
    использует для user_input/RAG-контекста, не отдельная, менее
    проверенная реализация (см. docstring модуля).
    """
    parts = [prompt_safety.wrap_untrusted(task_description, tag="task_description")]

    if file_contents:
        for path, content in file_contents.items():
            tagged = prompt_safety.wrap_untrusted(content, tag="file_context")
            parts.append(f"File: {path}\n{tagged}")

    parts.append(
        "\nDecompose the task above into a numbered list of atomic, "
        "independently-reviewable steps. Each step should describe ONE "
        "focused change to ONE file. Do not write code yet — only describe "
        "what each step will do and why."
    )
    return "\n\n".join(parts)


def write_plan(output_dir: Path, task_description: str, steps: list) -> Path:
    """Пишет plan.json в output_dir (НЕ в src/ — план не является
    кодом проекта, не должен проходить через path_guard/Sacred Area
    проверки на запись, потому что он никогда не применяется напрямую,
    только читается оператором).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / "plan.json"
    plan_data = {
        "task_description": task_description,
        "created_at": time.time(),
        "steps": [{"index": i + 1, "description": s, "status": "pending"} for i, s in enumerate(steps)],
    }
    plan_path.write_text(json.dumps(plan_data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Plan written: %s (%d steps)", plan_path, len(steps))
    return plan_path


def write_draft(output_dir: Path, step_index: int, target_file: str, content: str) -> Path:
    """Пишет draft_step_N.py в output_dir. Имя файла включает step_index
    и оригинальное имя целевого файла — оператор должен видеть из имени,
    к какому шагу плана и какому файлу проекта относится черновик, не
    угадывать по содержимому.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(target_file).name
    draft_path = output_dir / f"draft_step_{step_index}_{safe_name}"
    draft_path.write_text(content, encoding="utf-8")
    logger.info("Draft written: %s (for target=%s)", draft_path, target_file)
    return draft_path
