import sqlite3
from pathlib import Path
import pytest

from src.staging_manager import StagingManager
from src.staging_repository import StagingRepository
from src.path_guard import SecurityAccessViolation

@pytest.fixture
def tmp_base_dir(tmp_path):
    return tmp_path

class FakeRepoRaising:
    def add_task(self, task_uuid, data):
        raise sqlite3.Error("simulated database failure")
    def get_task(self, task_uuid):
        return None
    def delete_task(self, task_uuid):
        pass

@pytest.fixture
def real_repo(tmp_base_dir, monkeypatch):
    import config
    import db
    db_path = tmp_base_dir / "test_staging.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    repo = StagingRepository()
    repo.setup_database()
    return repo

@pytest.fixture
def staging_setup(real_repo, tmp_base_dir):
    input_dir = tmp_base_dir / "input"
    input_dir.mkdir()
    staging_dir = tmp_base_dir / "staging"
    manager = StagingManager(real_repo, staging_dir=staging_dir, input_dir=input_dir)
    return manager, input_dir, staging_dir, real_repo

def test_move_to_staging_happy_path(staging_setup):
    manager, input_dir, staging_dir, repo = staging_setup
    src_file = input_dir / "note.txt"
    src_file.write_text("hello world", encoding="utf-8")
    task_uuid = manager.move_to_staging(src_file)
    assert not src_file.exists()
    staged_file = staging_dir / "note.txt"
    assert staged_file.exists()
    task = repo.get_task(task_uuid)
    assert task is not None
    assert task["original_path"] == str(src_file)
    assert task["staging_path"] == str(staged_file)

def test_move_to_staging_rejects_path_outside_input_dir(staging_setup, tmp_path):
    manager, input_dir, staging_dir, repo = staging_setup
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("not in input_dir", encoding="utf-8")
    with pytest.raises(SecurityAccessViolation):
        manager.move_to_staging(outside_file)

def test_move_to_staging_rolls_back_on_db_error(tmp_base_dir):
    input_dir = tmp_base_dir / "input"
    input_dir.mkdir()
    staging_dir = tmp_base_dir / "staging"
    manager = StagingManager(FakeRepoRaising(), staging_dir=staging_dir, input_dir=input_dir)
    src_file = input_dir / "note.txt"
    src_file.write_text("hello world", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Database error"):
        manager.move_to_staging(src_file)
    assert src_file.exists()
    assert not (staging_dir / "note.txt").exists()

def test_approve_happy_path_returns_staging_path_and_clears_task(staging_setup):
    manager, input_dir, staging_dir, repo = staging_setup
    src_file = input_dir / "note.txt"
    src_file.write_text("hello world", encoding="utf-8")
    task_uuid = manager.move_to_staging(src_file)
    result_path = manager.approve(task_uuid)
    assert result_path == staging_dir / "note.txt"
    assert repo.get_task(task_uuid) is None

def test_approve_unknown_uuid_raises(staging_setup):
    manager, _input_dir, _staging_dir, _repo = staging_setup
    with pytest.raises(KeyError):
        manager.approve("nonexistent-uuid")

def test_approve_hash_mismatch_raises_and_preserves_task(staging_setup):
    manager, input_dir, staging_dir, repo = staging_setup
    src_file = input_dir / "note.txt"
    src_file.write_text("original content", encoding="utf-8")
    task_uuid = manager.move_to_staging(src_file)
    (staging_dir / "note.txt").write_text("tampered content", encoding="utf-8")
    with pytest.raises(ValueError, match="Hash mismatch"):
        manager.approve(task_uuid)
    assert repo.get_task(task_uuid) is not None

def test_reject_removes_file_and_clears_task(staging_setup):
    manager, input_dir, staging_dir, repo = staging_setup
    src_file = input_dir / "note.txt"
    src_file.write_text("hello world", encoding="utf-8")
    task_uuid = manager.move_to_staging(src_file)
    manager.reject(task_uuid)
    assert not (staging_dir / "note.txt").exists()
    assert repo.get_task(task_uuid) is None

def test_reject_unknown_uuid_raises(staging_setup):
    manager, _input_dir, _staging_dir, _repo = staging_setup
    with pytest.raises(KeyError):
        manager.reject("nonexistent-uuid")

def test_reject_missing_physical_file_still_clears_task(staging_setup):
    manager, input_dir, staging_dir, repo = staging_setup
    src_file = input_dir / "note.txt"
    src_file.write_text("hello world", encoding="utf-8")
    task_uuid = manager.move_to_staging(src_file)
    (staging_dir / "note.txt").unlink()
    manager.reject(task_uuid)
    assert repo.get_task(task_uuid) is None
