"""SnapshotManager — backup файлов перед применением патча, с откатом.

Часть инфраструктуры Task A (Human-in-the-Loop code modification — см.
историю решений). Снапшотится КОНКРЕТНЫЙ файл, который патчится, не
заранее зафиксированный список "Sacred Areas" — тот список про требование
более строгого impact-analysis перед патчем, не про то, какие файлы вообще
защищены backup'ом. Любой файл, который патчится, должен иметь снапшот,
иначе откат не сработает для патчей за пределами Sacred Areas.

Метаданные снимков (_snapshots) ПЕРСИСТЕНТНЫ через JSON-файл рядом со
снапшотами. Раньше это было in-memory — найдено реальным тестом, что это
ломает ручной rollback оператором после успешного apply(): CLI-инструмент
(cli_patch.py) запускается как отдельный процесс на каждую инвокацию,
apply и последующий rollback почти всегда происходят в РАЗНЫХ процессах
(оператор применяет патч, закрывает терминал, через час решает откатить).
In-memory словарь не переживает это — SnapshotNotFoundError на легитимный
запрос. Backup-файлы (*.bak) уже были персистентны на диске, не хватало
персистентности только метаданных, что какой snapshot_id соответствует
какому файлу.
"""
from __future__ import annotations

import hashlib
import json
import logger as oko_logger
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from src.path_guard import SecurityAccessViolation, guard_path

logger = oko_logger.get_logger("kernel.snapshot")

METADATA_FILENAME = "_snapshot_index.json"


class SnapshotNotFoundError(Exception):
    """snapshot_id не найден — попытка rollback к несуществующему снимку."""


class SnapshotManager:
    def __init__(self, snapshot_dir: Path, project_root: Path) -> None:
        """
        snapshot_dir — куда складываются копии файлов перед патчем.
        project_root — корень, относительно которого файлы для снапшота
        должны лежать (обычно oko/src/) — guard_path не пускает снапшотить
        что-либо за пределами проекта, по тому же принципу, что применялся
        для StagingManager/INPUT_DIR в Task B.

        snapshot_dir сам проверяется через guard_path с allowed_root,
        равным родителю project_root (как и для StagingManager) — если
        snapshot_dir сконфигурирован неверно, инициализация должна упасть
        здесь, не тихо создать директорию где-то ещё.
        """
        self.project_root = Path(project_root).resolve()

        try:
            self.snapshot_dir = guard_path(snapshot_dir, allowed_root=self.project_root.parent)
        except SecurityAccessViolation as e:
            logger.critical("SnapshotManager: snapshot_dir rejected by path_guard: %s", e)
            raise

        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        self._metadata_path = self.snapshot_dir / METADATA_FILENAME
        self._snapshots: dict[str, dict] = self._load_metadata()

    def _load_metadata(self) -> dict[str, dict]:
        if not self._metadata_path.exists():
            return {}
        try:
            raw = json.loads(self._metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Snapshot metadata load failed, starting empty: %s", e)
            return {}
        # original_path/backup_path сериализуются как str в JSON, десериализуем обратно в Path.
        return {
            snap_id: {**meta, "original_path": Path(meta["original_path"]), "backup_path": Path(meta["backup_path"])}
            for snap_id, meta in raw.items()
        }

    def _save_metadata(self) -> None:
        serializable = {
            snap_id: {**meta, "original_path": str(meta["original_path"]), "backup_path": str(meta["backup_path"])}
            for snap_id, meta in self._snapshots.items()
        }
        try:
            tmp_path = self._metadata_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
            tmp_path.replace(self._metadata_path)
        except OSError as e:
            logger.error("Snapshot metadata save failed: %s", e)

    def snapshot(self, file_path: Path | str) -> str:
        """Создаёт снимок file_path. Возвращает snapshot_id.

        file_path проверяется через guard_path с allowed_root=project_root
        ПЕРЕД любой файловой операцией — тот же принцип defense-in-depth,
        что применялся для StagingManager.move_to_staging. Если файл не
        существует (например, патч создаёт новый файл, не модифицирует
        существующий) — snapshot фиксирует это явно (sha256=None,
        existed=False), чтобы rollback знал: "восстановить" означает
        "удалить файл, которого не было", не "записать пустое содержимое".
        """
        validated_path = guard_path(file_path, allowed_root=self.project_root)

        snapshot_id = str(uuid.uuid4())
        existed = validated_path.exists()

        backup_path = self.snapshot_dir / f"{snapshot_id}.bak"
        content_hash = None

        if existed:
            shutil.copy2(validated_path, backup_path)
            content_hash = hashlib.sha256(validated_path.read_bytes()).hexdigest()

        self._snapshots[snapshot_id] = {
            "original_path": validated_path,
            "backup_path": backup_path,
            "existed": existed,
            "hash": content_hash,
            "timestamp": time.time(),
        }
        self._save_metadata()

        logger.info(
            "Snapshot created: %s (existed=%s) -> id=%s",
            validated_path.name, existed, snapshot_id,
        )
        return snapshot_id

    def rollback(self, snapshot_id: str) -> None:
        """Восстанавливает файл к состоянию на момент snapshot().

        Если файл не существовал на момент снимка (existed=False) —
        rollback удаляет файл, если он появился после (патч создал новый
        файл, который теперь нужно откатить), не пытается восстановить
        несуществующее содержимое.
        """
        if snapshot_id not in self._snapshots:
            raise SnapshotNotFoundError(f"No snapshot with id={snapshot_id}")

        meta = self._snapshots[snapshot_id]
        original_path = meta["original_path"]

        if meta["existed"]:
            shutil.copy2(meta["backup_path"], original_path)
            logger.info("Rolled back: %s (restored from snapshot)", original_path.name)
        else:
            if original_path.exists():
                original_path.unlink()
            logger.info("Rolled back: %s (removed, did not exist at snapshot time)", original_path.name)

    def discard(self, snapshot_id: str) -> None:
        """Явно подтверждает успех патча — удаляет backup-файл, снимок
        больше не нужен. Не обязателен к вызову (снимки не растут
        бесконечно опасно при текущем объёме использования — одна
        сессия патчинга за раз), но освобождает место на диске и убирает
        снимок из _snapshots, если вызывающий код хочет явно завершить
        жизненный цикл снапшота.
        """
        if snapshot_id not in self._snapshots:
            raise SnapshotNotFoundError(f"No snapshot with id={snapshot_id}")

        meta = self._snapshots.pop(snapshot_id)
        self._save_metadata()
        if meta["backup_path"].exists():
            meta["backup_path"].unlink()
        logger.info("Snapshot discarded: id=%s", snapshot_id)
