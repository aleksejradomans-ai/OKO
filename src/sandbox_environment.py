"""SandboxEnvironment — изолированный прогон тестов перед применением патча.

Часть инфраструктуры Task A (Human-in-the-Loop code modification). Изоляция
конфигурации достигается через переменную окружения AI_BASE_DIR, не через
генерацию временного config.py или манипуляции sys.path/sys.modules —
config.py уже параметризован через AI_BASE_DIR (BASE_DIR, DB_PATH,
STAGING_DIR, SNAPSHOT_DIR — все определены как BASE_DIR / "что-то"), и этот
же механизм использовался для тестирования StagingManager/SnapshotManager/
run_system.py на протяжении всей разработки. Изобретать второй механизм
изоляции конфигурации для одной и той же задачи — источник рассинхрона,
не дополнительной надёжности.
"""
from __future__ import annotations

import fnmatch
import logger as oko_logger
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple, Optional

logger = oko_logger.get_logger("kernel.sandbox")

# Директории/паттерны, не копируемые в sandbox. SNAPSHOT_DIR/STAGING_DIR
# исключены, чтобы sandbox не наследовал production-состояние (pending
# tasks, backup-файлы) — тесты получают чистую копию КОДА, не данных.
# .git включён на случай, если репозиторий появится позже — не вредная
# строка даже сейчас, когда .git физически не существует в проекте.
# SANDBOX_HYGIENE (25.10): + секреты (.env), кэши инструментов, логи, виртуальные
# окружения, векторная БД. Имена — только верхний уровень project_root (как
# раньше: вложенный каталог "data" в коде может быть фикстурой).
EXCLUDED_NAMES = {
    "snapshots", "staging", ".git", "__pycache__", "data",
    ".env", ".pytest_cache", ".mypy_cache", ".ruff_cache", "logs",
    ".venv", "venv", "chroma_db", "backups",
}
# Файлы-данные на любом уровне: базы SQLite (с WAL/SHM), логи, байткод.
EXCLUDED_FILE_PATTERNS = ("*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3",
                          "*.log", "*.pyc")
_NESTED_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", *EXCLUDED_FILE_PATTERNS)


def _is_excluded_file(name: str) -> bool:
    return any(fnmatch.fnmatch(name.lower(), pat) for pat in EXCLUDED_FILE_PATTERNS)


def _check_patch_path(relative_path: str) -> PurePosixPath:
    """ARCH01 (25.10): ключ patch_files обязан быть путём ВНУТРИ песочницы.
    Отклоняются (ValueError) пустой путь и ".", абсолютный путь, диск ("C:", "C:\\x"),
    UNC ("\\\\server\\share"), корень ("\\x", "/x") и любой компонент "..".
    Проверяются оба синтаксиса (Windows и POSIX) независимо от ОС: без этого
    Path(sandbox) / "C:\\oko\\src\\bot.py" на Windows — это сам боевой файл, а на
    Linux "/etc/x" — системный (подтверждено тестом 25.10 на старом коде)."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError(f"unsafe patch path: {relative_path!r} (empty)")
    win = PureWindowsPath(relative_path)
    posix = PurePosixPath(relative_path.replace("\\", "/"))
    if win.drive or win.root or posix.is_absolute():
        raise ValueError(f"unsafe patch path: {relative_path!r} (absolute or drive)")
    parts = [p for p in posix.parts if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"unsafe patch path: {relative_path!r} (outside sandbox)")
    return PurePosixPath(*parts)


def _on_rm_error(func, path, exc) -> None:
    """Обработчик rmtree: на Windows read-only файлы (.pyc, объекты git) не удаляются,
    пока не снят атрибут. Снимаем (и с каталога-родителя) и повторяем ОДИН раз;
    повторная неудача уходит в cleanup(), где превращается в WARNING."""
    for target in (path, os.path.dirname(path)):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except OSError:
            pass
    func(path)


class SandboxResult(NamedTuple):
    success: bool
    returncode: int
    stdout: str
    stderr: str
    sandbox_dir: Path


class SandboxEnvironment:
    def __init__(self, project_root: Path) -> None:
        """project_root — каталог с исходным кодом, который копируется в
        sandbox (обычно oko/src/). Не путать с config.BASE_DIR (data/) —
        тот явно исключается из копирования (см. EXCLUDED_NAMES), sandbox
        работает только с кодом, данные генерируются заново внутри
        изолированного AI_BASE_DIR на момент прогона тестов.
        """
        self.project_root = Path(project_root).resolve()

    def run_tests(
        self,
        test_target: Optional[str] = None,
        timeout: float = 120.0,
        patch_files: Optional[dict[str, str]] = None,
    ) -> SandboxResult:
        """Копирует project_root в изолированную временную директорию,
        ОПЦИОНАЛЬНО применяет patch_files (relative_path -> new_content)
        внутри sandbox-копии — ДО запуска тестов, после копирования —
        затем запускает pytest с AI_BASE_DIR внутрь sandbox.

        patch_files применяется здесь, не до вызова run_tests, по
        конкретной причине: если патч сначала пишется в project_root
        (production), а потом копируется в sandbox для теста, существует
        окно, где production уже изменён, а тест ещё не подтвердил
        безопасность патча. Любой другой процесс/перезапуск в это окно
        получит непроверенный код. Патч должен применяться К КОПИИ, не
        к оригиналу — оригинал трогается только после успешного теста
        (см. PatchApplier._commit, который и делает этот финальный шаг).

        Sandbox-директория НЕ удаляется автоматически при успехе —
        вызывающий код (PatchApplier) решает, когда её можно убрать; при
        сбое она тем более должна остаться для диагностики.
        """
        # ARCH01: все пути патча проверяются ДО создания песочницы — один плохой
        # ключ отклоняет весь патч, на диск не пишется ничего.
        checked = {rel: _check_patch_path(rel) for rel in (patch_files or {})}

        sandbox_id = f"sandbox_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        sandbox_base = Path(tempfile.gettempdir()) / sandbox_id
        sandbox_src = sandbox_base / "src"
        sandbox_data = sandbox_base / "data"

        sandbox_src.mkdir(parents=True)
        sandbox_data.mkdir(parents=True)

        self._copy_filtered(self.project_root, sandbox_src)

        if patch_files:
            root = sandbox_src.resolve()
            for relative_path, new_content in patch_files.items():
                target = (sandbox_src / checked[relative_path]).resolve()
                if not target.is_relative_to(root):   # вторая линия: символические ссылки и т.п.
                    raise ValueError(f"unsafe patch path: {relative_path!r} resolves outside sandbox")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_content, encoding="utf-8")

        # SANDBOX_HYGIENE: тот же интерпретатор, что у процесса, а не первый "pytest"
        # из PATH (под службой/NSSM это может быть другой Python без зависимостей).
        cmd = [sys.executable, "-m", "pytest", "-v"]
        if test_target:
            cmd.append(test_target)

        # Наследуем ПОЛНОЕ окружение процесса, переопределяя только то, что
        # реально нужно изолировать — не строим env с нуля. Прежняя версия
        # передавала только AI_BASE_DIR/TELEGRAM_TOKEN/PATH, что работало на
        # POSIX (там минимального набора обычно достаточно), но ломало
        # Windows: без SystemRoot/windir и остальных системных переменных
        # некоторые нативные расширения Python (например, _overlapped.pyd,
        # который тянет asyncio через anyio/pytest-плагин) падают на этапе
        # инициализации Winsock с невнятным OSError WinError 10106.
        # Подтверждено реальным сбоем sandbox-теста на Windows.
        env = os.environ.copy()
        env["AI_BASE_DIR"] = str(sandbox_data)
        # TELEGRAM_TOKEN обязателен для импорта config.py (см. _require_env)
        # — фиктивное значение, sandbox не делает реальных сетевых вызовов
        # к Telegram во время тестов.
        env["TELEGRAM_TOKEN"] = "sandbox_fake_token"

        try:
            result = subprocess.run(
                cmd,
                cwd=str(sandbox_src),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            success = result.returncode == 0
            return SandboxResult(
                success=success,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                sandbox_dir=sandbox_base,
            )
        except subprocess.TimeoutExpired as e:
            logger.error("Sandbox test run timed out after %.0fs", timeout)
            return SandboxResult(
                success=False,
                returncode=-1,
                stdout=e.stdout or "",
                stderr=f"TimeoutExpired after {timeout}s",
                sandbox_dir=sandbox_base,
            )

    def cleanup(self, sandbox_dir: Path) -> None:
        """Удаляет sandbox-директорию. Вызывается явно вызывающим кодом,
        не автоматически после run_tests (PatchApplier зовёт его только при
        успехе; при провале каталог остаётся как улика — решение 20.10).

        ARCH01: без ignore_errors — read-only файлы снимаются обработчиком,
        а если каталог всё равно остался, это одна строка WARNING
        [SANDBOX_CLEANUP], не молчание и не исключение."""
        sandbox_dir = Path(sandbox_dir)
        if not sandbox_dir.exists():
            return
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(sandbox_dir, onexc=_on_rm_error)
            else:
                shutil.rmtree(sandbox_dir, onerror=lambda f, p, ei: _on_rm_error(f, p, ei[1]))
        except OSError as e:
            logger.warning("[SANDBOX_CLEANUP] %s: %s", sandbox_dir, e)
            return
        if sandbox_dir.exists():
            logger.warning("[SANDBOX_CLEANUP] %s: directory still present after rmtree", sandbox_dir)
        else:
            logger.info("Sandbox cleaned up: %s", sandbox_dir)

    def _copy_filtered(self, src: Path, dst: Path) -> None:
        for item in src.iterdir():
            if item.name in EXCLUDED_NAMES:
                continue
            if item.is_dir():
                shutil.copytree(item, dst / item.name, ignore=_NESTED_IGNORE)
            elif not _is_excluded_file(item.name):
                shutil.copy2(item, dst / item.name)
