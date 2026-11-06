"""Тесты для sandbox_environment.py.

Покрывает _copy_filtered — фильтрацию production-данных при копировании
в sandbox. Не тестирует сам вызов pytest как subprocess внутри
sandbox-копии — это требует pytest установленного и доступного дочернему
процессу, что является отдельным, более тяжёлым сценарием (см. Шаг 1
dogfooding в истории решений).
"""
import tempfile
from pathlib import Path

from src.sandbox_environment import SandboxEnvironment


def test_copy_filtered_excludes_production_data():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        src = base / "project_src"
        src.mkdir()
        (src / "config.py").write_text("X = 1", encoding="utf-8")
        (src / "path_guard.py").write_text("Y = 2", encoding="utf-8")

        (src / "data").mkdir()
        (src / "data" / "secret.txt").write_text("production secret", encoding="utf-8")
        (src / "snapshots").mkdir()
        (src / "snapshots" / "old.bak").write_text("snapshot data", encoding="utf-8")
        (src / "staging").mkdir()

        dst = base / "sandbox_copy"
        dst.mkdir()

        sandbox = SandboxEnvironment(src)
        sandbox._copy_filtered(src, dst)

        copied = {p.name for p in dst.iterdir()}
        assert "config.py" in copied
        assert "path_guard.py" in copied
        assert "data" not in copied
        assert "snapshots" not in copied
        assert "staging" not in copied


def test_copy_filtered_preserves_real_code():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        src = base / "project_src"
        src.mkdir()
        original_content = "def real_function(): return 42"
        (src / "module.py").write_text(original_content, encoding="utf-8")

        dst = base / "sandbox_copy"
        dst.mkdir()

        sandbox = SandboxEnvironment(src)
        sandbox._copy_filtered(src, dst)

        assert (dst / "module.py").read_text(encoding="utf-8") == original_content


# --- ARCH01_DELTA (25.10.2026): пути патча и уборка песочницы ---------------------
import logging
import os
import stat
import subprocess
from unittest.mock import MagicMock

import pytest

import src.sandbox_environment as se


@pytest.fixture
def sb(tmp_path, monkeypatch):
    """Песочница внутри tmp_path; pytest-подпроцесс подменён (успех без запуска)."""
    project = tmp_path / "project_src"
    project.mkdir()
    (project / "module.py").write_text("X = 1", encoding="utf-8")
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(se.tempfile, "gettempdir", lambda: str(temp_root))
    runs = []

    def fake_run(cmd, **kw):
        runs.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed", stderr="")

    monkeypatch.setattr(se.subprocess, "run", fake_run)
    return SandboxEnvironment(project), temp_root, tmp_path, runs


# Абсолютные пути строятся внутри tmp_path ({ABS}): даже на СТАРОМ коде (до ARCH01)
# тест не может перезаписать файлы проекта. "Q:" — заведомо несуществующий диск.
@pytest.mark.parametrize("bad", [
    "../evil.py", "..\\evil.py", "sub/../../evil.py", "sub\\..\\..\\evil.py",
    "{ABS}", "{ABS_POSIX}", "Q:evil.py", "Q:\\evil.py", "\\\\nonexistent.invalid\\share\\evil.py",
    "", ".",
])
def test_unsafe_patch_path_rejected_before_any_write(sb, bad):
    sandbox, temp_root, tmp_path, runs = sb
    bad = bad.replace("{ABS_POSIX}", (tmp_path / "evil.py").as_posix()).replace("{ABS}", str(tmp_path / "evil.py"))
    with pytest.raises(ValueError):
        sandbox.run_tests(patch_files={bad: "PWNED = True"})
    assert list(temp_root.iterdir()) == []          # каталог песочницы даже не создан
    assert runs == []                                # pytest не запускался
    assert not (tmp_path / "evil.py").exists()


def test_one_bad_path_rejects_whole_patch(sb):
    sandbox, temp_root, _, runs = sb
    with pytest.raises(ValueError):
        sandbox.run_tests(patch_files={"good.py": "A = 1", "../evil.py": "B = 2"})
    assert list(temp_root.iterdir()) == [] and runs == []


def test_safe_nested_patch_path_written_inside_sandbox(sb):
    sandbox, temp_root, _, runs = sb
    res = sandbox.run_tests(patch_files={"sub/new_mod.py": "Y = 2", "module.py": "X = 3"})
    assert res.success and len(runs) == 1
    src = res.sandbox_dir / "src"
    assert (src / "sub" / "new_mod.py").read_text(encoding="utf-8") == "Y = 2"
    assert (src / "module.py").read_text(encoding="utf-8") == "X = 3"


def test_cleanup_removes_readonly_files(tmp_path, caplog):
    d = tmp_path / "sandbox_x"
    (d / "src" / "__pycache__").mkdir(parents=True)
    f = d / "src" / "__pycache__" / "m.cpython-313.pyc"
    f.write_bytes(b"\x00")
    os.chmod(f, stat.S_IREAD)                        # на Windows rmtree без обработчика не удалит
    caplog.set_level(logging.INFO, logger="kernel.sandbox")
    SandboxEnvironment(tmp_path).cleanup(d)
    assert not d.exists()
    assert not any("[SANDBOX_CLEANUP]" in r.getMessage() for r in caplog.records)


def test_rm_error_handler_makes_writable_and_retries(tmp_path):
    f = tmp_path / "ro.txt"
    f.write_text("x", encoding="utf-8")
    os.chmod(f, stat.S_IREAD)
    retry = MagicMock()
    se._on_rm_error(retry, str(f), PermissionError(13, "denied"))
    assert os.stat(f).st_mode & stat.S_IWRITE
    retry.assert_called_once_with(str(f))


def test_cleanup_warns_when_dir_survives(tmp_path, monkeypatch, caplog):
    d = tmp_path / "sandbox_y"
    d.mkdir()
    monkeypatch.setattr(se.shutil, "rmtree", lambda *a, **k: None)   # «не удалилось»
    caplog.set_level(logging.INFO, logger="kernel.sandbox")
    SandboxEnvironment(tmp_path).cleanup(d)           # без исключения
    warns = [r for r in caplog.records if "[SANDBOX_CLEANUP]" in r.getMessage()]
    assert len(warns) == 1 and warns[0].levelno == logging.WARNING
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_cleanup_missing_dir_is_noop(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="kernel.sandbox")
    SandboxEnvironment(tmp_path).cleanup(tmp_path / "nope")
    assert caplog.records == []


# --- SANDBOX_HYGIENE (25.10.2026): интерпретатор и расширенные исключения ---------
import sys


def test_pytest_runs_with_current_interpreter(sb):
    sandbox, _, _, runs = sb
    sandbox.run_tests(test_target="test_x.py")
    cmd = runs[0][0]
    assert cmd[:3] == [sys.executable, "-m", "pytest"] and cmd[-1] == "test_x.py"


def test_copy_filtered_excludes_secrets_caches_and_db_files(tmp_path):
    src = tmp_path / "project_src"
    (src / "pkg" / "data").mkdir(parents=True)
    for name in (".env", "app.log", "memory.db", "memory.db-wal", "k.sqlite3", "keep.py"):
        (src / name).write_text("x", encoding="utf-8")
    for d in (".pytest_cache", "logs", ".venv", "chroma_db", "backups"):
        (src / d).mkdir()
        (src / d / "f.txt").write_text("x", encoding="utf-8")
    (src / "pkg" / "mod.py").write_text("x", encoding="utf-8")
    (src / "pkg" / "cache.db").write_text("x", encoding="utf-8")
    (src / "pkg" / "data" / "fixture.json").write_text("{}", encoding="utf-8")
    dst = tmp_path / "dst"
    dst.mkdir()
    SandboxEnvironment(src)._copy_filtered(src, dst)
    assert sorted(p.name for p in dst.iterdir()) == ["keep.py", "pkg"]
    assert (dst / "pkg" / "mod.py").exists() and not (dst / "pkg" / "cache.db").exists()
    assert (dst / "pkg" / "data" / "fixture.json").exists()   # вложенный data — код/фикстуры
