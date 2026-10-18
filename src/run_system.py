import glob
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import config
import logger as oko_logger
from src.path_guard import SecurityAccessViolation, guard_path
from src.staging_repository import StagingRepository
from src.staging_manager import StagingManager

logger = oko_logger.get_logger("run_system")

OKO_RAG_DIR = Path(__file__).resolve().parent.parent / "oko_rag"

BACKUP_DIR = str(config.BASE_DIR / "backups")

MAX_BACKUPS_PER_FILE = int(os.getenv("MAX_BACKUPS_PER_FILE", "50"))

DEBOUNCE_SECONDS = config.DEBOUNCE_SECONDS

WATCHED_EXTENSIONS = {".txt", ".py", ".js", ".sql", ".pdf"}


class KnowledgeHandler(FileSystemEventHandler):
    def __init__(self, staging_manager):
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self.staging_manager = staging_manager

    def on_created(self, event):
        self.on_modified(event)

    def on_modified(self, event):
        if event.is_directory:
            return
        _, ext = os.path.splitext(event.src_path)
        if ext.lower() not in WATCHED_EXTENSIONS:
            return
        path = event.src_path
        with self._lock:
            existing = self._timers.get(path)
            if existing:
                existing.cancel()
            timer = threading.Timer(DEBOUNCE_SECONDS, self._run_indexer, args=[path])
            self._timers[path] = timer
            timer.start()

    def _backup_file(self, path: str) -> None:
        watch_dir = str(config.BASE_DIR / "knowledge")
        try:
            guard_path(path, allowed_root=watch_dir)
        except SecurityAccessViolation as e:
            logger.critical("Backup отказан — путь вне разрешённой зоны: %s | %s", path, e)
            return

        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = os.path.basename(path)
        backup_path = os.path.join(BACKUP_DIR, f"{name}.{timestamp}.bak")
        try:
            shutil.copy2(path, backup_path)
            logger.info("Backup created: %s", backup_path)
            self._cleanup_old_backups(name)
        except OSError as e:
            logger.error("Backup failed for %s: %s", path, e)

    def _cleanup_old_backups(self, original_name: str) -> None:
        pattern = os.path.join(BACKUP_DIR, f"{glob.escape(original_name)}.*.bak")
        backups = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        old_backups = backups[MAX_BACKUPS_PER_FILE:]
        for old in old_backups:
            try:
                os.remove(old)
            except OSError as e:
                logger.warning("Could not remove old backup %s: %s", old, e)
        if old_backups:
            logger.info("Removed %d old backups", len(old_backups))

    def _run_indexer(self, path: str) -> None:
        with self._lock:
            self._timers.pop(path, None)

        self._backup_file(path)

        try:
            task_uuid = self.staging_manager.move_to_staging(path)
            logger.info("Staged for approval: %s -> uuid=%s", path, task_uuid)
        except Exception as e:
            logger.error("Failed to move %s to staging: %s", path, e)


class OkoRagDocumentsHandler(FileSystemEventHandler):
    def __init__(self, target_dir: Path):
        self.target_dir = target_dir

    def _maybe_relocate(self, src_path: str) -> None:
        _, ext = os.path.splitext(src_path)
        if ext.lower() not in WATCHED_EXTENSIONS:
            return
        src = Path(src_path)
        if not src.exists():
            return
        dest = self.target_dir / src.name
        try:
            shutil.copy2(src, dest)
            src.unlink()
            logger.info(
                "oko_rag/documents: перенесён в основной input_dir: %s -> %s",
                src, dest,
            )
        except OSError as e:
            logger.error(
                "Не удалось перенести %s из oko_rag/documents в input_dir: %s",
                src, e,
            )

    def on_created(self, event):
        if event.is_directory:
            return
        self._maybe_relocate(event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._maybe_relocate(event.src_path)


def main():
    oko_logger.setup_logging()

    pid_file = config.BASE_DIR / "run_system.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    watch_path = config.BASE_DIR / "knowledge"
    watch_path.mkdir(parents=True, exist_ok=True)

    # ИСПРАВЛЕНО (см. историю решений — миграция pending_tasks в SQL,
    # синхронизация bot.py/run_system.py на StagingRepository): раньше
    # Registry.pending_tasks был JSON-словарём с read-merge-write гонкой
    # между двумя независимыми процессами — воспроизведённый lost update
    # при конкурентной записи. StagingRepository использует SQLite-
    # транзакции через тот же db.get_connection() (WAL), что и остальной
    # проект. setup_database() идемпотентна и обязательна здесь: NSSM
    # запускает bot.py и run_system.py как два независимых процесса без
    # гарантированного порядка старта — если run_system.py стартует
    # первым, первый же repo.add_task() без предварительного
    # setup_database() упадёт с sqlite3.OperationalError: no such table.
    staging_repo = StagingRepository()
    staging_repo.setup_database()
    staging_manager = StagingManager(
        staging_repo,
        staging_dir=config.STAGING_DIR,
        input_dir=watch_path,
    )

    event_handler = KnowledgeHandler(staging_manager)
    observer = Observer()
    observer.schedule(event_handler, str(watch_path), recursive=False)
    observer.start()

    oko_rag_docs_dir = OKO_RAG_DIR / "documents"
    oko_rag_docs_dir.mkdir(parents=True, exist_ok=True)

    oko_rag_handler = OkoRagDocumentsHandler(target_dir=watch_path)
    oko_rag_observer = Observer()
    oko_rag_observer.schedule(oko_rag_handler, str(oko_rag_docs_dir), recursive=False)
    oko_rag_observer.start()

    heartbeat_file = config.BASE_DIR / "run_system.heartbeat"
    HEARTBEAT_INTERVAL_S = 30
    tick = 0
    try:
        while True:
            time.sleep(1)
            tick += 1
            if tick >= HEARTBEAT_INTERVAL_S:
                heartbeat_file.touch()
                tick = 0
    except KeyboardInterrupt:
        observer.stop()
        oko_rag_observer.stop()
    observer.join()
    oko_rag_observer.join()


if __name__ == "__main__":
    main()
