"""conftest.py — общие pytest fixture для тестовой сюиты oko.

Centralizes common test setup that was duplicated across inline tests
throughout development. Every fixture here was validated by actual test
runs during this conversation — no speculative utilities.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# --- Защита боевых данных ---
# Тесты НИКОГДА не должны писать в рабочий BASE_DIR (и тем более удалять файлы
# в нём). config.py вызывает load_dotenv(), который не перезаписывает уже
# заданные переменные, а conftest загружается раньше любого тестового модуля.
import os as _guard_os, tempfile as _guard_tempfile
_guard_os.environ['AI_BASE_DIR'] = _guard_tempfile.mkdtemp(prefix='oko_pytest_')
# PUBLISH_PREP: тесты не читают настоящий .env -- ни токен, ни список админов.
# Задаём ДО первого импорта config.py; load_dotenv() заданное не перезаписывает.
_guard_os.environ['TELEGRAM_TOKEN'] = 'test-token-not-real'
_guard_os.environ['ADMIN_USER_IDS'] = ''


# Добавляем src/ в sys.path чтобы тесты импортировали модули без установки пакета
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def tmp_base_dir():
    """Изолированный BASE_DIR для каждого теста — тот же паттерн, что
    AI_BASE_DIR=../data/test_xyz в inline-тестах разработки.
    Автоматически удаляется после теста."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir).resolve()
        base.mkdir(parents=True, exist_ok=True)
        yield base


@pytest.fixture
def tmp_project_root(tmp_base_dir):
    """Изолированный project_root (src/) для тестов SnapshotManager/
    SandboxEnvironment/PatchApplier."""
    root = tmp_base_dir / "src"
    root.mkdir()
    yield root


@pytest.fixture
def tmp_snapshot_dir(tmp_base_dir):
    """Изолированный snapshot_dir внутри tmp_base_dir."""
    snap = tmp_base_dir / "snapshots"
    snap.mkdir()
    yield snap


@pytest.fixture
def env_with_base_dir(tmp_base_dir, monkeypatch):
    """Переменные окружения с AI_BASE_DIR → tmp_base_dir.
    Использует monkeypatch чтобы не загрязнять реальное окружение.
    TELEGRAM_TOKEN=fake нужен для импорта config.py (см. _require_env)."""
    monkeypatch.setenv("AI_BASE_DIR", str(tmp_base_dir))
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake_token_for_tests")
    yield tmp_base_dir


@pytest.fixture
def registry(env_with_base_dir, tmp_base_dir):
    """Свежий Registry на изолированном tmp_base_dir.
    Сбрасывает синглтон между тестами — иначе первый тест загрязнял бы
    все последующие (это реальная проблема, найденная при разработке)."""
    from registry import Registry
    # Сброс синглтона — Registry._instance создаётся один раз на процесс,
    # pytest запускает все тесты в одном процессе
    Registry._instance = None

    reg = Registry(filepath=str(tmp_base_dir / "registry_state.json"))
    yield reg

    # Cleanup: сброс после теста
    Registry._instance = None


@pytest.fixture
def snapshot_manager(tmp_project_root, tmp_snapshot_dir):
    """SnapshotManager с изолированными директориями."""
    from snapshot_manager import SnapshotManager
    return SnapshotManager(tmp_snapshot_dir, tmp_project_root)


@pytest.fixture(autouse=True)
def _isolate_memory(tmp_path, monkeypatch):
    """Тесты не должны писать в боевую memory.db."""
    import sys
    import config
    mem = tmp_path / "memory"
    (mem / "backups").mkdir(parents=True)
    monkeypatch.setattr(config, "MEMORY_DB_PATH", mem / "memory.db")
    monkeypatch.setattr(config, "MEMORY_BACKUP_DIR", mem / "backups")
    bot_mod = sys.modules.get("bot")
    if bot_mod is not None:
        monkeypatch.setattr(bot_mod, "_memory", None)   # сбросить кэшированный MemoryCore
