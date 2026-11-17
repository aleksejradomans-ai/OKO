"""PatchApplier — оркестратор для Task A (Human-in-the-Loop code patching).

Порядок этапов отличается от наивного "записать патч -> протестировать":
1. StaticAnalyzer проверяет патч ДО какого-либо копирования/применения —
   дёшево отклонить грубую ошибку до того, как тратить время на sandbox.
2. SandboxEnvironment копирует ОРИГИНАЛЬНЫЙ project_root (без патча) во
   временную директорию, применяет патч К КОПИИ, запускает тесты ТАМ.
3. Только при успехе тестов SnapshotManager делает snapshot оригинального
   файла в project_root, и сам файл перезаписывается содержимым из
   sandbox-копии.
4. При сбое на любом этапе — оригинал в project_root никогда не был
   тронут, откатывать нечего (snapshot создаётся только непосредственно
   перед финальной записью, не в начале процесса).

Почему не "записать патч в оригинал -> tест -> rollback при сбое": это
создаёт окно, где production-код изменён, но не подтверждён тестами —
любой другой процесс/перезапуск в это окно получит непроверенный код.
Тестирование на копии устраняет это окно полностью: оригинал либо не
тронут вообще (тест не прошёл), либо тронут один раз, атомарно, с уже
подтверждённым содержимым (тест прошёл).
"""
from __future__ import annotations

import logger as oko_logger
import os
import tempfile
from pathlib import Path
from typing import NamedTuple, Optional

from src.path_guard import SecurityAccessViolation, guard_path
from src.sandbox_environment import SandboxEnvironment
from src.snapshot_manager import SnapshotManager
from src.static_analyzer import StaticAnalyzer

logger = oko_logger.get_logger("kernel.patch_applier")


class PatchOutcome(NamedTuple):
    success: bool
    stage_failed: str  # "static_analysis" | "sandbox_test" | "commit" | ""
    detail: str
    snapshot_id: Optional[str]  # None если файл не был тронут вообще


class PatchApplier:
    def __init__(
        self,
        project_root: Path,
        snapshot_manager: SnapshotManager,
        sandbox_environment: SandboxEnvironment,
        static_analyzer: Optional[StaticAnalyzer] = None,
    ) -> None:
        """
        Все три зависимости внедряются явно, не создаются внутри __init__ —
        тот же принцип, что у KnowledgeHandler/StagingManager: позволяет
        тестировать PatchApplier с мок-компонентами, не поднимая реальный
        Registry/файловую структуру для каждого теста.
        """
        self.project_root = Path(project_root).resolve()
        self.snapshot_manager = snapshot_manager
        self.sandbox_environment = sandbox_environment
        self.static_analyzer = static_analyzer or StaticAnalyzer()

    def apply(
        self,
        relative_path: str,
        new_content: str,
        test_target: Optional[str] = None,
        test_timeout: float = 120.0,
        dry_run: bool = False,
    ) -> PatchOutcome:
        """relative_path — путь к файлу относительно project_root (не
        абсолютный — это сразу подразумевает "файл внутри проекта", не
        требует отдельной guard_path проверки на входе, хотя финальная
        запись всё равно проходит через guard_path, см. _commit).

        dry_run=True: выполняет этапы 1-2 (static analysis, sandbox test)
        как обычно, но НЕ вызывает _commit — оригинал в project_root
        гарантированно не тронут независимо от результата теста. Это не
        новый путь логики, а ранний return в той же функции — намеренно,
        чтобы dry-run и реальный прогон проверяли ОДНУ И ТУ ЖЕ
        последовательность этапов, не дублированную копию с риском
        рассинхрона между "что проверяет dry-run" и "что реально
        происходит при commit".
        """
        target_path = self.project_root / relative_path

        # Этап 1: статический анализ — дёшево отклонить до копирования/теста.
        analysis = self.static_analyzer.analyze(new_content, filename=Path(relative_path).name)
        if not analysis.ok:
            detail = "; ".join(analysis.violations)
            logger.warning("Patch rejected at static analysis: %s", detail)
            return PatchOutcome(
                success=False, stage_failed="static_analysis", detail=detail, snapshot_id=None,
            )

        # Этап 2: применение К КОПИИ и тест ВНУТРИ sandbox. Оригинал в
        # project_root не тронут на этом этапе — см. docstring модуля.
        sandbox_result = self.sandbox_environment.run_tests(
            test_target=test_target,
            timeout=test_timeout,
            patch_files={relative_path: new_content},
        )

        if not sandbox_result.success:
            detail = f"returncode={sandbox_result.returncode}\nSTDOUT:\n{sandbox_result.stdout}\nSTDERR:\n{sandbox_result.stderr}"
            logger.warning("Patch rejected at sandbox test: %s", relative_path)
            # ARCH01 (решение Оператора 20.10): провал теста -> каталог песочницы
            # остаётся уликой для разбора, не удаляется. Уборка — вручную.
            logger.warning("[SANDBOX_KEPT] %s returncode=%s", sandbox_result.sandbox_dir,
                           sandbox_result.returncode)
            return PatchOutcome(
                success=False, stage_failed="sandbox_test", detail=detail, snapshot_id=None,
            )

        if dry_run:
            # Тест прошёл, но commit не вызывается — оригинал не тронут.
            # success=True здесь означает "патч ПРОШЁЛ БЫ", не "патч применён" —
            # вызывающий код (CLI) должен явно различать эти два смысла при
            # выводе сообщения оператору, иначе dry-run будет выглядеть как
            # реальное применение в логах.
            self.sandbox_environment.cleanup(sandbox_result.sandbox_dir)
            logger.info("Dry-run PASSED (not applied): %s", relative_path)
            return PatchOutcome(
                success=True, stage_failed="", detail="DRY_RUN: all checks passed, not committed", snapshot_id=None,
            )

        # Этап 3: тест прошёл — теперь, и только теперь, трогаем оригинал.
        try:
            snapshot_id = self._commit(target_path, new_content)
        except SecurityAccessViolation as e:
            logger.critical("Patch commit rejected by path_guard: %s", e)
            self.sandbox_environment.cleanup(sandbox_result.sandbox_dir)
            return PatchOutcome(
                success=False, stage_failed="commit", detail=str(e), snapshot_id=None,
            )

        self.sandbox_environment.cleanup(sandbox_result.sandbox_dir)
        logger.info("Patch applied successfully: %s (snapshot_id=%s)", relative_path, snapshot_id)
        return PatchOutcome(success=True, stage_failed="", detail="", snapshot_id=snapshot_id)

    def _commit(self, target_path: Path, new_content: str) -> str:
        """Snapshot оригинала + атомарная запись нового содержимого.

        Snapshot создаётся ЗДЕСЬ, не в начале apply() — он защищает именно
        этот момент (на случай, если после успешного теста потребуется
        откатить уже применённый, протестированный патч по другой причине,
        например оператор решил откатить вручную позже). Не защищает от
        "тест не прошёл" — в этом случае оригинал и не был тронут, откат
        не нужен вообще.

        Запись через tempfile + os.replace — тот же атомарный паттерн, что
        в Registry._save_checkpoint, не голый write_text() (который не
        атомарен и может оставить файл в битом состоянии при сбое
        процесса посередине записи).
        """
        validated_target = guard_path(target_path, allowed_root=self.project_root)
        snapshot_id = self.snapshot_manager.snapshot(validated_target)

        dir_ = validated_target.parent
        fd, tmp_path = tempfile.mkstemp(dir=str(dir_), prefix=".patch_tmp_")
        try:
            # encoding="utf-8" ЯВНО — без этого os.fdopen использует
            # платформенную кодировку по умолчанию. На Windows с русской
            # локалью это cp1251, не UTF-8: любой символ вне cp1251
            # (например, комбинирующее ударение U+0301, встречающееся в
            # обычном русском тексте комментариев патчей) роняет запись с
            # UnicodeEncodeError. Остальной проект везде читает/пишет текст
            # с explicit encoding="utf-8" (см. SnapshotManager,
            # StagingManager) — здесь это было упущено. Подтверждено
            # реальным сбоем при первом боевом применении патча.
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
            os.replace(tmp_path, validated_target)
        except Exception as write_err:
            # COMMIT_ROLLBACK_MASK: наружу всегда уходит ИСХОДНАЯ ошибка записи.
            # Сбой уборки tmp-файла или отката логируется и не подменяет её.
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                logger.warning("Could not remove temp file %s", tmp_path, exc_info=True)
            # Применение не удалось технически (диск, права) — откатываем
            # снимок немедленно, не оставляем его "висящим" для ручного
            # разбора оператором.
            try:
                self.snapshot_manager.rollback(snapshot_id)
            except Exception as rollback_err:
                logger.critical(
                    "Rollback of snapshot %s failed after commit error %r; "
                    "original error is re-raised",
                    snapshot_id, write_err, exc_info=True,
                )
                if hasattr(write_err, "add_note"):
                    write_err.add_note(
                        f"rollback of snapshot {snapshot_id} also failed: {rollback_err!r}"
                    )
            raise

        return snapshot_id

    def rollback(self, snapshot_id: str) -> None:
        """Явный откат уже применённого патча — для случая, когда
        оператор решает отменить изменение ПОСЛЕ успешного apply()
        (например, обнаружил проблему не покрытую тестами)."""
        self.snapshot_manager.rollback(snapshot_id)
        logger.info("Patch rolled back: snapshot_id=%s", snapshot_id)
