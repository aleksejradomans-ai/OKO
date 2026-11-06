"""cli_patch.py — локальный CLI для применения патчей через PatchApplier.

Не Telegram, не HTTP, не сетевой интерфейс — запускается оператором на той
же машине, где развёрнут проект (см. историю решений: Task A работает по
модели "Оператор — Инструмент", не "удалённое управление кодом"). Это
исключает риски, специфичные для удалённых каналов (переформатирование
текста мессенджером, экспонирование code-execution интерфейса в публичный
API), которые делают Telegram неприемлемым каналом для этой задачи.

Использование:
    python -m cli_patch apply --file governor.py --content patched_governor.py
    python -m cli_patch apply --file governor.py --content patched_governor.py --test-target tests/test_governor.py
    python -m cli_patch rollback --snapshot-id <uuid>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import config
from src.patch_applier import PatchApplier
from src.sandbox_environment import SandboxEnvironment
from src.snapshot_manager import SnapshotManager

# ИСПРАВЛЕНО (см. историю решений — аудит блоков №1-4, находка
# "project_root не зафиксирован в коде"): раньше --project-root по
# умолчанию был "." — текущая рабочая директория shell'а в момент
# запуска, а не константа. SandboxEnvironment.docstring говорит "project_root
# — обычно oko/src/", но это было ОПИСАНИЕМ ПРИВЫЧКИ оператора, не
# ограничением, закреплённым кодом: если оператор запускал команду из
# C:\oko\ (естественно — это корень репозитория), project_root становился
# всем репозиторием, и guard_path пропускал патчи на ЛЮБОЙ файл внутри
# него, не только src/*.py. Реальная граница self-patching зависела от
# того, в какой папке случайно стоял оператор в конкретный день — не от
# кода. EXPECTED_PROJECT_ROOT привязан к расположению ЭТОГО файла
# (Path(__file__).resolve().parent / "src"), не к cwd — единственный
# способ сделать границу детерминированной независимо от того, откуда
# запущена команда.
# ИСПРАВЛЕНО (см. историю решений — подтверждено реальным листингом
# C:\oko\ и C:\oko\src\, 20.07.2026): cli_patch.py САМ физически лежит
# внутри src\ (C:\oko\src\cli_patch.py), не в корне репозитория рядом со
# src\, как предполагалось при первой версии этого фикса. Прежняя формула
# (Path(__file__).parent / "src") вычисляла бы C:\oko\src\src\ —
# несуществующий вложенный путь, что сломало бы весь механизм защиты от
# "гуляющего" project_root по ДРУГОЙ причине, чем исходная (cwd), но с
# тем же результатом — ложным EXPECTED_PROJECT_ROOT. Правильная формула:
# директория, где лежит САМ этот файл, И ЕСТЬ project_root — никакого
# дополнительного /"src" не нужно.
EXPECTED_PROJECT_ROOT = Path(__file__).resolve().parent


def _build_applier(project_root: Path) -> PatchApplier:
    snapshot_manager = SnapshotManager(config.SNAPSHOT_DIR, project_root)
    sandbox_environment = SandboxEnvironment(project_root)
    return PatchApplier(project_root, snapshot_manager, sandbox_environment)


def _resolve_and_confirm_project_root(raw_project_root: str) -> Path:
    """Резолвит --project-root и требует явного подтверждения, если
    итоговый путь отличается от EXPECTED_PROJECT_ROOT.

    Не блокирует нестандартный project_root безусловно — могут быть
    легитимные причины его переопределить (например, тестовое окружение
    с изолированной копией проекта). Но операция, расширяющая периметр
    self-patching за пределы ожидаемого src/, не должна происходить
    молча/по умолчанию из-за того, что оператор забыл про --project-root
    или запустил команду не из той директории — требуется осознанное
    'yes', то же самое человеческое подтверждение, которое уже является
    Root of Trust для всего Task A (см. историю решений, аудит self-
    patching pipeline).
    """
    resolved = Path(raw_project_root).resolve()

    if resolved != EXPECTED_PROJECT_ROOT:
        print(
            f"\n[!] ВНИМАНИЕ: project_root ({resolved}) отличается от "
            f"ожидаемого ({EXPECTED_PROJECT_ROOT}).\n"
            f"    Патчи будут применяться в границах {resolved} — это "
            f"РАСШИРЯЕТ или ПЕРЕМЕЩАЕТ периметр self-patching относительно "
            f"обычного src/. Если это не намеренное решение (например, "
            f"случайный запуск из другой директории) — прервите выполнение.\n",
            file=sys.stderr,
        )
        confirm = input("Продолжить с этим project_root? (yes/no): ")
        if confirm.strip().lower() != "yes":
            print("Отменено оператором.", file=sys.stderr)
            sys.exit(1)

    return resolved


def cmd_apply(args: argparse.Namespace) -> int:
    """Применяет патч: читает новое содержимое из --content (файл на
    диске, написанный оператором локально), прогоняет через все 4 этапа
    PatchApplier, печатает статус каждого этапа в stdout по мере
    прохождения — не одним финальным сообщением после завершения, чтобы
    оператор видел прогресс на длительной sandbox-тестовой фазе, не
    смотрел на пустой терминал несколько минут без обратной связи.

    --dry-run: проходит static analysis и sandbox test как обычно, но НЕ
    применяет патч к project_root — PatchApplier.apply(dry_run=True)
    гарантирует это на уровне самой функции, не на уровне CLI-обёртки
    (см. patch_applier.py — это не дублированная логика, ранний return
    в той же последовательности этапов).
    """
    project_root = _resolve_and_confirm_project_root(args.project_root)
    content_path = Path(args.content)

    if not content_path.exists():
        print(f"ERROR: --content file not found: {content_path}", file=sys.stderr)
        return 1

    new_content = content_path.read_text(encoding="utf-8")

    mode_label = "[DRY RUN]" if args.dry_run else "[APPLY]"
    print(f"{mode_label} [1/4] Static analysis: {args.file}")
    applier = _build_applier(project_root)

    outcome = applier.apply(
        relative_path=args.file,
        new_content=new_content,
        test_target=args.test_target,
        test_timeout=args.timeout,
        dry_run=args.dry_run,
    )

    if outcome.success and args.dry_run:
        print(f"{mode_label} [2-3/4] PASSED — patch would apply cleanly. NOT written to disk.")
        print("Run again without --dry-run to actually apply.")
        return 0

    if outcome.success:
        print(f"{mode_label} [2-4/4] PASSED — patch applied. snapshot_id={outcome.snapshot_id}")
        print(f"To roll back: python -m cli_patch rollback --snapshot-id {outcome.snapshot_id}")
        return 0

    print(f"{mode_label} FAILED at stage: {outcome.stage_failed}", file=sys.stderr)
    print(outcome.detail, file=sys.stderr)
    return 1


def cmd_rollback(args: argparse.Namespace) -> int:
    """Откатывает уже применённый патч по snapshot_id. Требует, чтобы
    PatchApplier этой же сессии CLI-процесса всё ещё держал snapshot в
    памяти (SnapshotManager._snapshots — in-memory, не персистентно, см.
    snapshot_manager.py) — то есть rollback должен вызываться В ТОЙ ЖЕ
    инвокации процесса, где был apply, или snapshot_id будет не найден.
    Это известное ограничение текущей архитектуры, не баг этого CLI:
    если нужен rollback после перезапуска процесса, нужна персистентность
    snapshot-метаданных, которой сейчас сознательно нет (см. историю
    решений в snapshot_manager.py).
    """
    project_root = _resolve_and_confirm_project_root(args.project_root)
    applier = _build_applier(project_root)

    print(f"WARNING: rollback требует snapshot_id из ТЕКУЩЕЙ сессии. "
          f"Если snapshot был создан в предыдущем запуске cli_patch.py, "
          f"эта команда вернёт SnapshotNotFoundError — снимки не "
          f"персистентны между процессами (см. snapshot_manager.py).")

    try:
        applier.rollback(args.snapshot_id)
        print(f"Rolled back: snapshot_id={args.snapshot_id}")
        return 0
    except Exception as e:
        print(f"ERROR: rollback failed: {e}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local CLI for applying code patches through PatchApplier "
                     "(Task A, Human-in-the-Loop code modification)."
    )
    parser.add_argument(
        "--project-root", default=str(EXPECTED_PROJECT_ROOT),
        help=f"Root directory containing the project source "
             f"(default: {EXPECTED_PROJECT_ROOT}, NOT current working directory — "
             f"see history of decisions, 'project_root не зафиксирован' finding)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply", help="Apply a patch to a file")
    apply_parser.add_argument("--file", required=True, help="Path relative to project root")
    apply_parser.add_argument("--content", required=True, help="Path to file containing new content")
    apply_parser.add_argument("--test-target", default=None, help="Specific test file/dir to run")
    apply_parser.add_argument("--timeout", type=float, default=120.0)
    apply_parser.add_argument(
        "--dry-run", action="store_true",
        help="Run static analysis and sandbox tests, but do not write the patch to disk",
    )
    apply_parser.set_defaults(func=cmd_apply)

    rollback_parser = subparsers.add_parser("rollback", help="Roll back a previously applied patch")
    rollback_parser.add_argument("--snapshot-id", required=True)
    rollback_parser.set_defaults(func=cmd_rollback)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
