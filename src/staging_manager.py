"""StagingManager — карантин входящих документов перед индексацией.

Task B (RAG Data Ingestion Staging) из истории решений — отделена от
отложенной Task A (self-modification кода агента). Документы, обнаруженные
watchdog'ом в INPUT_DIR, не индексируются немедленно: они перемещаются в
STAGING_DIR, хешируются, регистрируются в StagingRepository (SQLite) с
UUID, и ждут явного /approve через Telegram, прежде чем knowledge_indexer
их обработает.

ИЗМЕНЕНО (см. историю решений — миграция pending_tasks в SQL): раньше
StagingManager принимал Registry и хранил pending_tasks как JSON-словарь
в registry_state.json — read-merge-write без файлового лока давал
воспроизведённый lost update между этим процессом (bot.py) и run_system.py,
работающими как два независимых NSSM-процесса. StagingRepository делает то
же через SQLite-транзакции (db.get_connection(), тот же WAL-модуль, что
использует остальной проект) — lost update для pending_tasks теперь
структурно невозможен. Эта версия класса НЕ принимает Registry вообще —
ни как зависимость, ни как fallback; смешение двух источников истины было
самой причиной бага, поэтому путь назад намеренно не оставлен.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Any

import logger as oko_logger
from src.path_guard import SecurityAccessViolation, guard_path

logger = oko_logger.get_logger("kernel.staging")


class StagingPathRejected(ValueError):
    """Путь задачи из БД не прошёл повторную проверку (вне карантина, ссылка, не обычный файл).
    Наследник ValueError: вызывающий код (бот) показывает причину так же, как несовпадение хэша."""


def _is_link(p: Path) -> bool:
    """Символическая ссылка или точка повторного разбора Windows (junction и т. п.) -- по самому пути, без перехода."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


class StagingManager:
    def __init__(self, repo: Any, staging_dir: Path, input_dir: Path) -> None:
        """
        repo — StagingRepository (или совместимый объект с add_task/
        get_task/delete_task поверх SQLite). Не типизирован жёстко как
        StagingRepository, чтобы не создавать циклический импорт и чтобы
        тесты могли подставлять минимальный дублирующий контракт.

        staging_dir и input_dir оба обязаны лежать внутри одного общего
        корня (typically BASE_DIR) — это проверяется здесь же через
        guard_path с allowed_root=общий родитель, не предполагается слепо.
        Если staging_dir сконфигурирован неверно (например, указывает за
        пределы BASE_DIR из-за ошибки в .env), инициализация должна упасть
        здесь, а не тихо создать директорию карантина где-то ещё — тот же
        принцип fail-fast, что применялся для Registry.filepath.
        """
        self.repo = repo
        self.input_dir = Path(input_dir).resolve()

        # allowed_root для самого staging_dir — родитель input_dir (обычно
        # BASE_DIR). Если staging_dir не внутри него, это сигнал неверной
        # конфигурации, не "просто другое легитимное место".
        try:
            self.staging_dir = guard_path(staging_dir, allowed_root=self.input_dir.parent)
        except SecurityAccessViolation as e:
            logger.critical("StagingManager: staging_dir rejected by path_guard: %s", e)
            raise

        self.staging_dir.mkdir(parents=True, exist_ok=True)

    def move_to_staging(self, src_path: Path | str) -> str:
        """Перемещает src_path в staging_dir, хеширует, регистрирует с UUID.

        Возвращает UUID задачи при успехе. Если repo.add_task() падает
        (sqlite3.Error — БД занята, диск полон, схема повреждена и т.п.),
        файл откатывается обратно в input_dir ПЕРЕД тем, как исключение
        пробрасывается вызывающему коду (KnowledgeHandler) — иначе файл
        физически исчезал бы из input_dir без соответствующей записи о
        задаче: ни в очереди на approve, ни на исходном месте для
        повторного обнаружения watchdog'ом.

        src_path проверяется через guard_path с allowed_root=self.input_dir
        ДО любой файловой операции — тот же принцип, что уже применён в
        knowledge_indexer.py для защиты от символических ссылок,
        указывающих за пределы наблюдаемой директории. event.src_path от
        watchdog untrusted в том же смысле — баг библиотеки или симлинк
        внутри input_dir могут дать путь, разрешающийся наружу.
        """
        validated_src = guard_path(src_path, allowed_root=self.input_dir)
        content_hash = hashlib.sha256(validated_src.read_bytes()).hexdigest()
        dest_path = self.staging_dir / validated_src.name

        validated_src.rename(dest_path)

        task_uuid = str(uuid.uuid4())
        try:
            self.repo.add_task(task_uuid, {
                "type": "file_indexing",
                "original_path": str(validated_src),
                "staging_path": str(dest_path),
                "hash": content_hash,
                "timestamp": time.time(),
            })
        except sqlite3.Error as e:
            # Откат ДО проброса исключения — файл возвращается в input_dir,
            # чтобы не потеряться между двумя хранилищами.
            dest_path.rename(validated_src)
            logger.error(
                "Staging registration failed for %s, rolled back: %s",
                validated_src.name, e,
            )
            raise RuntimeError(f"Database error during staging registration: {e}") from e

        logger.info(
            "Staged for approval: %s -> %s (uuid=%s)",
            validated_src.name, dest_path, task_uuid,
        )
        return task_uuid

    def _checked_staging_path(self, raw: str) -> Path:
        """Путь из БД -- недоверенные данные: строка могла быть изменена после постановки в карантин. Перед
        чтением / удалением: сам путь и его каталоги внутри карантина -- не ссылки; путь после разрешения лежит
        внутри staging_dir (guard_path). Возвращает проверенный путь; иначе StagingPathRejected."""
        p = Path(raw)
        if not p.is_absolute():
            p = self.staging_dir / p
        probe = Path(os.path.abspath(p))
        chain = [probe] + [q for q in probe.parents if q != self.staging_dir and self.staging_dir in q.parents]
        if any(_is_link(q) for q in chain):
            raise StagingPathRejected(f"staged path rejected: {probe.name} is a symlink / junction (or lies behind one)")
        try:
            return guard_path(probe, allowed_root=self.staging_dir)
        except SecurityAccessViolation as e:
            raise StagingPathRejected(f"staged path rejected: {probe.name} is outside the staging folder") from e

    def approve(self, task_uuid: str) -> Path:
        """Подтверждает задачу и возвращает путь к файлу для индексации.

        Проверяет хеш перед approve — если файл в staging_dir был изменён
        (подмена, ручное редактирование) пока ждал подтверждения, approve
        должен ОТКАЗАТЬ, а не молча пропустить изменённое содержимое.
        Задача НЕ удаляется из repo при hash-mismatch — файл остаётся
        видимым в очереди pending, вместо того чтобы бесследно исчезнуть
        как "обработанный".
        """
        task = self.repo.get_task(task_uuid)
        if not task:
            raise KeyError(f"No pending task with uuid={task_uuid}")

        # Повторная проверка пути ДО чтения: верный хэш файла вне карантина не делает его одобряемым.
        try:
            staging_path = self._checked_staging_path(task["staging_path"])
        except StagingPathRejected as e:
            logger.critical("Approve refused for uuid=%s: %s", task_uuid, e)
            raise
        if not staging_path.is_file():
            logger.critical("Approve refused for uuid=%s: %s is not a regular file", task_uuid, staging_path.name)
            raise StagingPathRejected(f"staged path rejected: {staging_path.name} is not a regular file")
        current_hash = hashlib.sha256(staging_path.read_bytes()).hexdigest()
        if current_hash != task["hash"]:
            logger.critical(
                "Hash mismatch on approve for uuid=%s (%s): file modified while pending",
                task_uuid, staging_path.name,
            )
            raise ValueError(
                f"Hash mismatch for {staging_path.name}: file was modified "
                f"while pending approval (expected {task['hash']}, got {current_hash})"
            )

        self.repo.delete_task(task_uuid)
        logger.info("Approved: uuid=%s -> %s", task_uuid, staging_path)
        return staging_path

    def reject(self, task_uuid: str) -> None:
        """Отклоняет и удаляет файл из карантина.

        Устойчиво к случаю, когда файл уже физически отсутствует (удалён
        руками помимо StagingManager) — задача всё равно вычищается из
        repo, чтобы не оставлять висящую запись о несуществующем файле.
        """
        task = self.repo.get_task(task_uuid)
        if not task:
            raise KeyError(f"No pending task with uuid={task_uuid}")

        # Повторная проверка пути ДО удаления: по строке из БД вне карантина ничего не удаляется; ссылка и
        # не-файл не трогаются. Задача при отказе остаётся в очереди (видна Оператору), как при несовпадении хэша.
        try:
            staging_path = self._checked_staging_path(task["staging_path"])
        except StagingPathRejected as e:
            logger.critical("Reject refused for uuid=%s: %s -- nothing deleted", task_uuid, e)
            raise
        if staging_path.exists():
            if not staging_path.is_file():
                logger.critical("Reject refused for uuid=%s: %s is not a regular file -- nothing deleted",
                                task_uuid, staging_path.name)
                raise StagingPathRejected(f"staged path rejected: {staging_path.name} is not a regular file")
            staging_path.unlink()

        self.repo.delete_task(task_uuid)
        logger.info("Rejected and removed: uuid=%s (%s)", task_uuid, staging_path.name)
