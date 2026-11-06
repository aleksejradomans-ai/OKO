"""Тесты для snapshot_manager.py.

Включает регрессионный тест на персистентность метаданных снимков —
найденный и исправленный реальный дефект (in-memory _snapshots ломал
rollback между раздельными процессами CLI).
"""
import tempfile
from pathlib import Path

import pytest

from src.snapshot_manager import SnapshotManager, SnapshotNotFoundError
from src.path_guard import SecurityAccessViolation


@pytest.fixture
def manager_setup():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        project_root = base / "src"
        snapshot_dir = base / "snapshots"
        project_root.mkdir()
        manager = SnapshotManager(snapshot_dir, project_root)
        yield manager, project_root, snapshot_dir


def test_snapshot_and_rollback_existing_file(manager_setup):
    manager, project_root, _ = manager_setup
    existing_file = project_root / "module.py"
    existing_file.write_text("def original(): return 1", encoding="utf-8")

    snap_id = manager.snapshot(existing_file)
    existing_file.write_text("def patched(): return 2", encoding="utf-8")
    manager.rollback(snap_id)

    assert "original" in existing_file.read_text()
    assert "patched" not in existing_file.read_text()


def test_snapshot_and_rollback_nonexistent_file_removes_it(manager_setup):
    """Файл, не существовавший на момент snapshot (патч создаёт новый
    файл) — rollback должен УДАЛИТЬ его, не оставить с пустым содержимым."""
    manager, project_root, _ = manager_setup
    new_file = project_root / "new_module.py"
    assert not new_file.exists()

    snap_id = manager.snapshot(new_file)
    new_file.write_text("def newly_created(): pass", encoding="utf-8")
    manager.rollback(snap_id)

    assert not new_file.exists()


def test_discard_removes_backup(manager_setup):
    manager, project_root, _ = manager_setup
    existing_file = project_root / "module.py"
    existing_file.write_text("def stable(): return 42", encoding="utf-8")

    snap_id = manager.snapshot(existing_file)
    backup_path = manager._snapshots[snap_id]["backup_path"]
    assert backup_path.exists()

    manager.discard(snap_id)
    assert not backup_path.exists()


def test_rollback_unknown_snapshot_id_raises(manager_setup):
    manager, _, _ = manager_setup
    with pytest.raises(SnapshotNotFoundError):
        manager.rollback("nonexistent-id")


def test_snapshot_outside_project_root_rejected(manager_setup):
    manager, _, _ = manager_setup
    with pytest.raises(SecurityAccessViolation):
        manager.snapshot("/etc/passwd")


def test_metadata_persists_across_separate_manager_instances(manager_setup):
    """Регрессионный тест на реальный дефект, найденный в ходе разработки:
    _snapshots был in-memory, что ломало rollback между раздельными
    процессами CLI (apply в одном вызове, rollback в другом). Здесь
    эмулируем "раздельные процессы" через два независимых экземпляра
    SnapshotManager на одном snapshot_dir — пересоздание объекта
    эквивалентно новому процессу с точки зрения in-memory состояния.
    """
    manager, project_root, snapshot_dir = manager_setup
    existing_file = project_root / "module.py"
    existing_file.write_text("def original(): return 1", encoding="utf-8")

    snap_id = manager.snapshot(existing_file)
    existing_file.write_text("def patched(): return 2", encoding="utf-8")

    # "Новый процесс" — независимый экземпляр, не переиспользующий manager
    second_manager = SnapshotManager(snapshot_dir, project_root)
    second_manager.rollback(snap_id)

    assert "original" in existing_file.read_text()
