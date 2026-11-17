"""Тесты для patch_applier.py.
Использует мок SandboxEnvironment с контролируемым результатом — изоляция
от реальной зависимости pytest-внутри-pytest (вложенный запуск SandboxEnvironment
которая сама вызывает subprocess pytest было бы рекурсией, требующей
реальной установки pytest внутри sandbox-копии; здесь проверяется
оркестрация PatchApplier, не сама работа SandboxEnvironment — для той есть
test_sandbox_environment.py отдельно, с более ограниченным охватом по той
же причине).
"""
import logging
import tempfile
from pathlib import Path

import pytest

from src.patch_applier import PatchApplier
from src.snapshot_manager import SnapshotManager
from src.sandbox_environment import SandboxResult


class FakeSandbox:
    """Мок SandboxEnvironment с заранее заданным результатом."""

    def __init__(self, fake_result):
        self.fake_result = fake_result
        self.cleanup_called_with = []

    def run_tests(self, test_target=None, timeout=120.0, patch_files=None):
        return self.fake_result

    def cleanup(self, sandbox_dir):
        self.cleanup_called_with.append(sandbox_dir)


@pytest.fixture
def project_setup():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        project_root = base / "src"
        snapshot_dir = base / "snapshots"
        project_root.mkdir()
        (project_root / "target_module.py").write_text("def original(): return 1", encoding="utf-8")
        snapshot_manager = SnapshotManager(snapshot_dir, project_root)
        yield project_root, snapshot_manager


def test_static_analysis_failure_never_touches_original(project_setup):
    """eval() в патче -> отклонён на static analysis, sandbox не вызывается,
    оригинал нетронут."""
    project_root, snapshot_manager = project_setup
    fake_sandbox = FakeSandbox(None)  # не должен быть вызван — None упал бы при использовании
    applier = PatchApplier(project_root, snapshot_manager, fake_sandbox)

    outcome = applier.apply("target_module.py", 'x = eval("bad")')

    assert outcome.success is False
    assert outcome.stage_failed == "static_analysis"
    assert outcome.snapshot_id is None
    assert "original" in (project_root / "target_module.py").read_text()


def test_sandbox_failure_never_touches_original_and_keeps_evidence(project_setup, caplog):
    """ARCH01 (решение Оператора 20.10): FAIL -> каталог песочницы остаётся для разбора,
    в лог одна строка WARNING [SANDBOX_KEPT] с путём и кодом возврата."""
    project_root, snapshot_manager = project_setup
    evidence = Path(tempfile.mkdtemp())
    failing_result = SandboxResult(
        success=False, returncode=1, stdout="1 failed", stderr="AssertionError",
        sandbox_dir=evidence,
    )
    fake_sandbox = FakeSandbox(failing_result)
    applier = PatchApplier(project_root, snapshot_manager, fake_sandbox)
    caplog.set_level(logging.INFO, logger="kernel.patch_applier")

    outcome = applier.apply("target_module.py", "def patched(): return 2")

    assert outcome.success is False
    assert outcome.stage_failed == "sandbox_test"
    assert outcome.snapshot_id is None
    assert "original" in (project_root / "target_module.py").read_text()
    assert fake_sandbox.cleanup_called_with == []
    kept = [r for r in caplog.records if "[SANDBOX_KEPT]" in r.getMessage()]
    assert len(kept) == 1 and kept[0].levelno == logging.WARNING
    assert str(evidence) in kept[0].getMessage() and "returncode=1" in kept[0].getMessage()


def test_sandbox_timeout_also_keeps_evidence(project_setup, caplog):
    project_root, snapshot_manager = project_setup
    timeout_result = SandboxResult(
        success=False, returncode=-1, stdout="", stderr="TimeoutExpired after 120.0s",
        sandbox_dir=Path(tempfile.mkdtemp()),
    )
    fake_sandbox = FakeSandbox(timeout_result)
    caplog.set_level(logging.INFO, logger="kernel.patch_applier")
    outcome = PatchApplier(project_root, snapshot_manager, fake_sandbox).apply(
        "target_module.py", "def patched(): return 2")
    assert outcome.stage_failed == "sandbox_test" and fake_sandbox.cleanup_called_with == []
    assert any("[SANDBOX_KEPT]" in r.getMessage() and "returncode=-1" in r.getMessage()
               for r in caplog.records)


def test_full_success_applies_patch_and_allows_rollback(project_setup):
    project_root, snapshot_manager = project_setup
    passing_result = SandboxResult(
        success=True, returncode=0, stdout="5 passed", stderr="",
        sandbox_dir=Path(tempfile.mkdtemp()),
    )
    fake_sandbox = FakeSandbox(passing_result)
    applier = PatchApplier(project_root, snapshot_manager, fake_sandbox)

    new_code = "def patched(): return 2  # CONFIRMED BY TESTS"
    outcome = applier.apply("target_module.py", new_code)

    assert outcome.success is True
    assert outcome.snapshot_id is not None
    assert (project_root / "target_module.py").read_text() == new_code

    applier.rollback(outcome.snapshot_id)
    assert "original" in (project_root / "target_module.py").read_text()


def test_dry_run_passes_checks_but_never_writes(project_setup):
    """dry_run=True: static analysis и sandbox test выполняются как обычно,
    но _commit не вызывается — оригинал гарантированно не тронут даже при
    успешном прохождении всех проверок."""
    project_root, snapshot_manager = project_setup
    passing_result = SandboxResult(
        success=True, returncode=0, stdout="5 passed", stderr="",
        sandbox_dir=Path(tempfile.mkdtemp()),
    )
    fake_sandbox = FakeSandbox(passing_result)
    applier = PatchApplier(project_root, snapshot_manager, fake_sandbox)

    outcome = applier.apply(
        "target_module.py", "def patched(): return 2  # DRY RUN", dry_run=True,
    )

    assert outcome.success is True
    assert outcome.snapshot_id is None  # _commit не вызывался
    assert "DRY_RUN" in outcome.detail
    assert "original" in (project_root / "target_module.py").read_text()
    assert "patched" not in (project_root / "target_module.py").read_text()
    assert len(fake_sandbox.cleanup_called_with) == 1


# --- COMMIT_ROLLBACK_MASK: сбой отката/уборки не подменяет исходную ошибку ---

import logging
import os

_REAL_REPLACE = os.replace
_REAL_UNLINK = os.unlink


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def pa_log():
    lg = logging.getLogger("kernel.patch_applier")
    handler = _LogCapture()
    old_level = lg.level
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    yield handler
    lg.removeHandler(handler)
    lg.setLevel(old_level)


def _disk_full_on_patch_tmp(src, dst):
    """Падает только на финальной записи патча; остальные os.replace
    (метаданные снапшотов) работают как обычно."""
    if ".patch_tmp_" in str(src):
        raise OSError(28, "No space left on device")
    return _REAL_REPLACE(src, dst)


def test_commit_rollback_failure_reraises_original_error(project_setup, monkeypatch, pa_log):
    project_root, snapshot_manager = project_setup
    applier = PatchApplier(project_root, snapshot_manager, FakeSandbox(None))
    monkeypatch.setattr(os, "replace", _disk_full_on_patch_tmp)

    def broken_rollback(snapshot_id):
        raise RuntimeError("rollback broke")

    monkeypatch.setattr(snapshot_manager, "rollback", broken_rollback)

    with pytest.raises(OSError) as ei:
        applier._commit(project_root / "target_module.py", "def patched(): return 2")

    assert ei.value.errno == 28, repr(ei.value)
    notes = getattr(ei.value, "__notes__", [])
    assert any("rollback broke" in n for n in notes), notes
    critical = [r for r in pa_log.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1 and critical[0].exc_info is not None
    assert "original" in (project_root / "target_module.py").read_text()
    assert not list(project_root.glob(".patch_tmp_*"))


def test_commit_successful_rollback_reraises_original_error(project_setup, monkeypatch, pa_log):
    project_root, snapshot_manager = project_setup
    applier = PatchApplier(project_root, snapshot_manager, FakeSandbox(None))
    monkeypatch.setattr(os, "replace", _disk_full_on_patch_tmp)
    calls = []
    real_rollback = snapshot_manager.rollback
    monkeypatch.setattr(snapshot_manager, "rollback",
                        lambda sid: (calls.append(sid), real_rollback(sid)))

    with pytest.raises(OSError) as ei:
        applier._commit(project_root / "target_module.py", "def patched(): return 2")

    assert ei.value.errno == 28
    assert len(calls) == 1
    assert not getattr(ei.value, "__notes__", [])
    assert [r for r in pa_log.records if r.levelno == logging.CRITICAL] == []
    assert "original" in (project_root / "target_module.py").read_text()


def test_commit_tmp_cleanup_failure_still_rolls_back_and_reraises(project_setup, monkeypatch, pa_log):
    project_root, snapshot_manager = project_setup
    applier = PatchApplier(project_root, snapshot_manager, FakeSandbox(None))

    def locked_unlink(path, *args, **kwargs):
        if ".patch_tmp_" in str(path):
            raise PermissionError(13, "file is locked by antivirus")
        return _REAL_UNLINK(path, *args, **kwargs)

    calls = []
    real_rollback = snapshot_manager.rollback
    monkeypatch.setattr(snapshot_manager, "rollback",
                        lambda sid: (calls.append(sid), real_rollback(sid)))

    # Подмены os.* живут ТОЛЬКО внутри context(): autouse-фикстура conftest
    # (_isolate_memory) тоже берёт monkeypatch, поэтому общий откат происходит
    # ПОСЛЕ уборки TemporaryDirectory -- заблокированный os.unlink сломал бы её.
    with monkeypatch.context() as m:
        m.setattr(os, "replace", _disk_full_on_patch_tmp)
        m.setattr(os, "unlink", locked_unlink)
        with pytest.raises(OSError) as ei:
            applier._commit(project_root / "target_module.py", "def patched(): return 2")
    for leftover in project_root.glob(".patch_tmp_*"):
        leftover.unlink()

    assert ei.value.errno == 28, repr(ei.value)
    assert len(calls) == 1
    assert any(r.levelno == logging.WARNING and "temp file" in r.getMessage()
               for r in pa_log.records)
