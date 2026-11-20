"""B27 (T3): наблюдатель -- байт-в-байт дубль (тот же sha256), уже лежащий в archived или ждущий
подтверждения в staging, -> пропуск без ошибки, без задачи и без второй записи в индекс; файл не удаляется, а
переносится в archived/duplicates. Остальное поведение карантина -- прежнее."""
from __future__ import annotations

import hashlib
import logging

import pytest

import run_system as RS
import src.staging_manager as SM
from src.staging_manager import StagingManager
from src.staging_repository import StagingRepository

PDF = b"%PDF-1.4\n% sample document\n" + bytes(range(256)) * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "kb.db")
    repo = StagingRepository()
    repo.setup_database()
    inp = tmp_path / "knowledge"
    (inp / "archived").mkdir(parents=True)
    mgr = StagingManager(repo, staging_dir=tmp_path / "staging", input_dir=inp)
    return mgr, inp, tmp_path / "staging", repo


def _tasks(repo) -> int:
    return len(repo.get_all_tasks())


def test_b27_duplicate_of_archived_skipped_and_moved(env, caplog):
    mgr, inp, stg, repo = env
    (inp / "archived" / "notes_20261118_214435_429161.pdf").write_bytes(PDF)
    src = inp / "notes.pdf"
    src.write_bytes(PDF)
    with caplog.at_level(logging.INFO):
        res = mgr.move_to_staging(src)
    assert res is None and _tasks(repo) == 0
    assert not src.exists() and not (stg / src.name).exists()
    dups = list((inp / "archived" / "duplicates").iterdir())
    assert len(dups) == 1 and dups[0].read_bytes() == PDF and dups[0].name.startswith("notes_")
    assert len([p for p in (inp / "archived").iterdir() if p.is_file()]) == 1      # archived -- ровно один экземпляр
    assert any("[DUPLICATE_SKIP]" in r.getMessage() and hashlib.sha256(PDF).hexdigest()[:12] in r.getMessage()
               for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_b27_duplicate_of_pending_in_staging_no_winerror(env):
    mgr, inp, stg, repo = env
    a = inp / "plan.pdf"
    a.write_bytes(PDF)
    u = mgr.move_to_staging(a)
    assert u and _tasks(repo) == 1
    a.write_bytes(PDF)                               # тот же файл положен повторно, пока первый ждёт approve
    assert mgr.move_to_staging(a) is None
    assert _tasks(repo) == 1 and (stg / "plan.pdf").read_bytes() == PDF
    assert len(list((inp / "archived" / "duplicates").iterdir())) == 1
    assert mgr.approve(u) == stg / "plan.pdf"     # первая задача не тронута


def test_b27_same_name_other_bytes_is_staged(env):
    mgr, inp, stg, repo = env
    (inp / "archived" / "doc_20261118_1.pdf").write_bytes(PDF)
    src = inp / "doc.pdf"
    src.write_bytes(PDF + b"x")                      # другие байты: размер и хэш не совпадают
    assert mgr.move_to_staging(src) is not None and _tasks(repo) == 1
    assert not (inp / "archived" / "duplicates").exists()


def test_b27_same_size_other_hash_is_staged(env):
    mgr, inp, stg, repo = env
    (inp / "archived" / "a_1.txt").write_bytes(b"abcd")
    src = inp / "b.txt"
    src.write_bytes(b"abce")
    assert mgr.move_to_staging(src) is not None


def test_b27_rejected_and_duplicates_subfolders_not_compared(env):
    mgr, inp, stg, repo = env
    for sub in ("rejected", "duplicates"):
        (inp / "archived" / sub).mkdir(parents=True, exist_ok=True)
        (inp / "archived" / sub / "x_1.txt").write_bytes(b"same text")
    src = inp / "x.txt"
    src.write_bytes(b"same text")
    assert mgr.move_to_staging(src) is not None       # не рекурсивно: только archived\ и staging\


def test_b27_no_archive_dir_is_fine(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "kb.db")
    repo = StagingRepository()
    repo.setup_database()
    inp = tmp_path / "knowledge"
    inp.mkdir()
    mgr = StagingManager(repo, staging_dir=tmp_path / "staging", input_dir=inp)
    src = inp / "n.txt"
    src.write_text("hello", encoding="utf-8")
    assert mgr.move_to_staging(src) is not None


def test_b27_move_failure_still_no_error(env, monkeypatch):
    mgr, inp, stg, repo = env
    (inp / "archived" / "d_1.txt").write_bytes(b"dup")
    src = inp / "d.txt"
    src.write_bytes(b"dup")

    def deny(*a, **k):
        raise PermissionError("locked")
    monkeypatch.setattr(SM.shutil, "move", deny)
    assert mgr.move_to_staging(src) is None and _tasks(repo) == 0
    assert src.exists()                               # файл не потерян


def test_b27_watcher_logs_skip_not_error(env, caplog, monkeypatch):
    mgr, inp, stg, repo = env
    (inp / "archived" / "w_1.txt").write_bytes(b"watched")
    src = inp / "w.txt"
    src.write_bytes(b"watched")
    import config
    monkeypatch.setattr(config, "BASE_DIR", inp.parent)
    monkeypatch.setattr(RS, "BACKUP_DIR", str(inp.parent / "backups"))
    h = RS.KnowledgeHandler(mgr)
    with caplog.at_level(logging.INFO):
        h._run_indexer(str(src))
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Duplicate skipped (not staged): w.txt") for m in msgs)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert _tasks(repo) == 0


def test_b27_same_bytes_in_ignores_links_and_dirs(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    (d / "sub").mkdir()
    t = tmp_path / "target.txt"
    t.write_bytes(b"q")
    try:
        (d / "link.txt").symlink_to(t)
    except (OSError, NotImplementedError):
        pass
    assert SM.same_bytes_in(d, 1, hashlib.sha256(b"q").hexdigest()) is None
    assert SM.same_bytes_in(tmp_path / "missing", 1, "0" * 64) is None
