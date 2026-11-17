"""Тесты для path_guard.py.

Переносит inline-сценарии, проверенные в ходе разработки (через
python3 -c "..."), в форму, которую pytest реально обнаруживает и
собирает — это и есть содержательный вход для SandboxEnvironment.run_tests(),
без которого её первый прогон проверил бы только "что происходит при
отсутствии тестов", не реальную работу pytest.
"""
import tempfile
from pathlib import Path

import pytest

from src.path_guard import SecurityAccessViolation, guard_path, _is_within, FORBIDDEN_PATHS


def test_path_within_allowed_root_passes():
    with tempfile.TemporaryDirectory() as tmpdir:
        allowed = Path(tmpdir)
        result = guard_path(allowed / "subdir" / "file.txt", allowed_root=allowed)
        assert result == (allowed / "subdir" / "file.txt").resolve()


def test_path_outside_allowed_root_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        allowed = Path(tmpdir)
        with pytest.raises(SecurityAccessViolation):
            guard_path("/etc/passwd", allowed_root=allowed)


def test_relative_traversal_does_not_escape_check():
    """.. не обходит проверку — resolve() происходит ДО сравнения с allowed_root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        allowed = Path(tmpdir)
        with pytest.raises(SecurityAccessViolation):
            guard_path(allowed / ".." / ".." / "etc" / "passwd", allowed_root=allowed)


def test_windows_forbidden_path_detected_via_pure_windows_path():
    """Синтетический тест через PureWindowsPath — структура путей, не
    файловые операции (см. историю решений — реальный os.replace/
    PermissionError на Windows не воспроизводим в Linux sandbox)."""
    from pathlib import PureWindowsPath
    windows_path = PureWindowsPath(r"C:\Windows\System32\drivers\etc\hosts")
    windows_forbidden = PureWindowsPath(r"C:\Windows")
    assert _is_within(windows_path, windows_forbidden) is True


def test_similar_directory_name_does_not_false_positive():
    """C:\\WindowsApps не должен ложно сработать как C:\\Windows — структурная
    проверка через relative_to, не наивный startswith на строке."""
    from pathlib import PureWindowsPath
    similar = PureWindowsPath(r"C:\WindowsApps\SomeApp")
    forbidden = PureWindowsPath(r"C:\Windows")
    assert _is_within(similar, forbidden) is False


def test_guard_path_lazy_imports_config_only_when_needed():
    """allowed_root явно передан -> guard_path не должен требовать config
    (и значит не должен требовать TELEGRAM_TOKEN в окружении). Это сам
    факт изоляции модуля, найденный и исправленный в ходе разработки."""
    with tempfile.TemporaryDirectory() as tmpdir:
        allowed = Path(tmpdir)
        # Если бы guard_path жёстко импортировал config на уровне модуля,
        # этот тест упал бы при импорте path_guard самого по себе, не здесь.
        result = guard_path(allowed / "file.txt", allowed_root=allowed)
        # guard_path() возвращает резолвленный путь — сравниваем с резолвленным
        # allowed, не с "сырым" allowed. На Windows Path(tmpdir), отданный
        # tempfile, и его же резолвленная форма могут текстуально различаться
        # (короткое 8.3-имя vs развёрнутое длинное) для одного и того же
        # физического каталога — тест должен сравнивать эквивалентные формы,
        # не полагаться на то, что tempfile уже вернул путь в резолвленном виде.
        assert result.parent == allowed.resolve()
