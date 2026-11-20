"""Одобрение / отклонение из карантина: путь из БД проверяется повторно (StagingManager._checked_staging_path).

Строка staging_path в БД -- недоверенные данные. До чтения (approve) и до удаления (reject): путь после разрешения
лежит внутри каталога карантина, сам путь и каталоги до него -- не ссылки, цель -- обычный файл. Иначе отказ
(StagingPathRejected, наследник ValueError) с причиной; ничего не читается, не удаляется и не индексируется, задача
остаётся в очереди. Обычный случай -- без изменений. Только временные каталоги.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from src.staging_manager import StagingManager, StagingPathRejected
from src.staging_repository import StagingRepository


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test_staging.db")
    repo = StagingRepository()
    repo.setup_database()
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    staging_dir = tmp_path / "staging"
    return StagingManager(repo, staging_dir=staging_dir, input_dir=input_dir), input_dir, staging_dir, repo, tmp_path


def _task(repo, uuid, path, content: bytes):
    repo.add_task(uuid, {"type": "file_indexing", "original_path": "x", "staging_path": str(path),
                         "hash": hashlib.sha256(content).hexdigest(), "timestamp": 1.0})


def _symlink(monkeypatch, link, target, is_dir=False):
    """Настоящая ссылка, где ОС позволяет (Linux, раннер CI, Windows с правом на ссылки); для каталога на Windows без
    этого права -- junction (mklink /J). Если ссылку создать нельзя -- на её месте обычный файл / каталог, а признак
    «это ссылка» для этого пути подставляется (проверяется решение менеджера, а не способность ОС делать ссылки)."""
    import subprocess
    import src.staging_manager as sm
    try:
        os.symlink(target, link, target_is_directory=is_dir)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if is_dir and os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if r.returncode == 0:
            return "junction"
    if is_dir:
        link.mkdir()
        for p in target.iterdir():
            (link / p.name).write_bytes(p.read_bytes())
    else:
        link.write_bytes(target.read_bytes())
    real = sm._is_link
    monkeypatch.setattr(sm, "_is_link", lambda p: os.path.abspath(p) == os.path.abspath(link) or real(p))
    return "simulated"


def test_is_link_false_for_plain_file_dir_and_missing(tmp_path):
    import src.staging_manager as sm
    (tmp_path / "f.txt").write_bytes(b"x")
    assert not sm._is_link(tmp_path / "f.txt") and not sm._is_link(tmp_path) and not sm._is_link(tmp_path / "none")


def test_is_link_true_for_real_symlink_where_the_os_allows(tmp_path):
    import src.staging_manager as sm
    (tmp_path / "f.txt").write_bytes(b"x")
    try:
        os.symlink(tmp_path / "f.txt", tmp_path / "l.txt")
    except (OSError, NotImplementedError):
        return                                   # нет права на ссылки: решение менеджера покрыто тестами ниже
    assert sm._is_link(tmp_path / "l.txt")


# --- обычный случай ------------------------------------------------------------------------------------------------
def test_normal_approve_returns_checked_path_inside_staging(setup):
    m, input_dir, staging_dir, repo, _ = setup
    f = input_dir / "note.txt"
    f.write_text("hello", encoding="utf-8")
    uid = m.move_to_staging(f)
    p = m.approve(uid)
    assert p == (staging_dir / "note.txt").resolve() and p.read_text(encoding="utf-8") == "hello"
    assert repo.get_task(uid) is None


def test_normal_reject_deletes_file_and_task(setup):
    m, input_dir, staging_dir, repo, _ = setup
    f = input_dir / "note.txt"
    f.write_text("hello", encoding="utf-8")
    uid = m.move_to_staging(f)
    m.reject(uid)
    assert not (staging_dir / "note.txt").exists() and repo.get_task(uid) is None


def test_reject_with_missing_file_still_clears_task(setup):
    m, _, staging_dir, repo, _ = setup
    _task(repo, "u-gone", staging_dir / "gone.txt", b"x")
    m.reject("u-gone")
    assert repo.get_task("u-gone") is None


def test_relative_path_from_db_is_taken_inside_staging(setup):
    m, _, staging_dir, repo, _ = setup
    (staging_dir / "rel.txt").write_bytes(b"data")
    _task(repo, "u-rel", "rel.txt", b"data")
    assert m.approve("u-rel") == (staging_dir / "rel.txt").resolve()


# --- путь вне карантина с ВЕРНЫМ хэшем -------------------------------------------------------------------------------
@pytest.mark.parametrize("where", ["outside", "dotdot", "input"])
def test_approve_refuses_path_outside_staging_even_with_right_hash(setup, where):
    m, input_dir, staging_dir, repo, tmp = setup
    secret = (input_dir if where == "input" else tmp) / "secret.txt"
    secret.write_bytes(b"secret")
    path = staging_dir / ".." / secret.name if where == "dotdot" else secret
    _task(repo, "u-out", path, b"secret")
    with pytest.raises(StagingPathRejected, match="outside the staging folder"):
        m.approve("u-out")
    assert repo.get_task("u-out") is not None and secret.read_bytes() == b"secret"   # задача видна, файл цел


def test_reject_refuses_path_outside_staging_nothing_deleted(setup):
    m, _, _, repo, tmp = setup
    victim = tmp / "victim.txt"
    victim.write_bytes(b"keep me")
    _task(repo, "u-del", victim, b"keep me")
    with pytest.raises(StagingPathRejected):
        m.reject("u-del")
    assert victim.read_bytes() == b"keep me" and repo.get_task("u-del") is not None


def test_rejection_is_a_value_error_for_the_caller(setup):
    assert issubclass(StagingPathRejected, ValueError)


# --- ссылки ------------------------------------------------------------------------------------------------------------
def test_approve_refuses_symlink_to_outside_file(setup, monkeypatch):
    m, _, staging_dir, repo, tmp = setup
    secret = tmp / "secret.txt"
    secret.write_bytes(b"secret")
    link = staging_dir / "doc.txt"
    _symlink(monkeypatch, link, secret)
    _task(repo, "u-link", link, b"secret")
    with pytest.raises(StagingPathRejected, match="symlink"):
        m.approve("u-link")
    assert repo.get_task("u-link") is not None


def test_approve_refuses_symlink_even_to_file_inside_staging(setup, monkeypatch):
    m, _, staging_dir, repo, _ = setup
    real = staging_dir / "real.txt"
    real.write_bytes(b"data")
    link = staging_dir / "alias.txt"
    _symlink(monkeypatch, link, real)
    _task(repo, "u-alias", link, b"data")
    with pytest.raises(StagingPathRejected, match="symlink"):
        m.approve("u-alias")


def test_approve_refuses_file_behind_linked_directory(setup, monkeypatch):
    m, _, staging_dir, repo, tmp = setup
    outside = tmp / "outside_dir"
    outside.mkdir()
    (outside / "doc.txt").write_bytes(b"secret")
    _symlink(monkeypatch, staging_dir / "sub", outside, is_dir=True)
    _task(repo, "u-dir", staging_dir / "sub" / "doc.txt", b"secret")
    with pytest.raises(StagingPathRejected):
        m.approve("u-dir")


def test_reject_refuses_symlink_and_keeps_target(setup, monkeypatch):
    m, _, staging_dir, repo, tmp = setup
    target = tmp / "target.txt"
    target.write_bytes(b"keep")
    link = staging_dir / "doc.txt"
    _symlink(monkeypatch, link, target)
    _task(repo, "u-rl", link, b"keep")
    with pytest.raises(StagingPathRejected):
        m.reject("u-rl")
    assert target.read_bytes() == b"keep" and os.path.lexists(link) and repo.get_task("u-rl") is not None


# --- не обычный файл -----------------------------------------------------------------------------------------------------
def test_approve_and_reject_refuse_directory(setup):
    m, _, staging_dir, repo, _ = setup
    d = staging_dir / "folder"
    d.mkdir()
    _task(repo, "u-d", d, b"")
    with pytest.raises(StagingPathRejected, match="not a regular file"):
        m.approve("u-d")
    with pytest.raises(StagingPathRejected, match="not a regular file"):
        m.reject("u-d")
    assert d.is_dir() and repo.get_task("u-d") is not None


def test_approve_refuses_the_staging_folder_itself(setup):
    m, _, staging_dir, repo, _ = setup
    _task(repo, "u-self", staging_dir, b"")
    with pytest.raises(StagingPathRejected, match="not a regular file"):
        m.approve("u-self")


def test_hash_mismatch_still_reported_as_before(setup):
    m, input_dir, staging_dir, repo, _ = setup
    f = input_dir / "note.txt"
    f.write_text("hello", encoding="utf-8")
    uid = m.move_to_staging(f)
    (staging_dir / "note.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Hash mismatch") as ei:
        m.approve(uid)
    assert not isinstance(ei.value, StagingPathRejected) and repo.get_task(uid) is not None
