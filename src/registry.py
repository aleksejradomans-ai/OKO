"""Thread-safe, process-local state registry with atomic persistence.

NOT safe across multiple OS processes. If you scale beyond a single
process (multiprocessing, multiple workers, supervisor with N replicas),
back this with Redis or SQLite instead of an in-memory singleton + JSON file.
"""
from __future__ import annotations

import json
import logger as oko_logger
import os
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple, Optional

import config
import registry_schema
from src.io_retry import retry_on_permission_error
from src.path_guard import SecurityAccessViolation, guard_path

logger = oko_logger.get_logger("kernel.registry")

ALLOWED_STATE_KEYS = frozenset({
    "current_task",
    "uptime",
    "status",
    "last_error",
    "narrative_state",
    "reflection_depth",
    "compliance_violations",
    "compliance_check_failures",
    "total_requests",
    "last_compliance_status",
    "quarantine_buffer",
    "input_blocked_count",
    "last_alert_state_compliance",
    "last_alert_state_input",
    "pending_tasks",
    # PATCH_V2_STAGNATION: маркеры деградации (observability)
    "transport_failures",
    "stagnation_breaks",
    "last_stagnation_reason",
})


class BootCheckResult(NamedTuple):
    ok: bool
    failures: list[str]


class Registry:
    """Process-local singleton. See module docstring for scaling caveat."""

    _instance: Optional["Registry"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "Registry":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self, filepath: str | None = None) -> None:
        """
        ВАЖНО — взаимодействие с паттерном синглтона: guard_path проверяет
        filepath только при ПЕРВОЙ успешной инициализации процесса. Если
        Registry() уже был создан раньше (self._initialized == True),
        любой filepath, переданный в повторном вызове, молча игнорируется
        этой же строкой ниже (if self._initialized: return) — даже не
        опасный, любой. Это не дефект guard_path, это уже существующее
        поведение синглтона: defense здесь работает как fail-fast на
        старте процесса, не как continuous enforcement при каждом вызове
        конструктора. Подтверждено тестом при интеграции path_guard.
        """
        if self._initialized:
            return

        raw_path = filepath or config.REGISTRY_FILEPATH
        try:
            if filepath is not None:
                # Явно переданный filepath (например, тестовой фикстурой на
                # изолированный tmp_base_dir) не обязан лежать внутри
                # config.BASE_DIR. config.BASE_DIR вычисляется ОДИН РАЗ, в
                # момент первого импорта config.py — Python кэширует модули,
                # и последующие monkeypatch.setenv("AI_BASE_DIR", ...) в
                # других тестах эту константу не пересчитывают. Сравнение
                # с устаревшим BASE_DIR ложно отклоняло бы легитimные,
                # изолированные тестовые пути. Резолвим allowed_root от
                # родителя самого filepath — FORBIDDEN_PATHS (чёрный список)
                # всё равно проверяется guard_path безусловно, защита не
                # ослабляется, просто не сравнивается с чужим, чужим по
                # времени, значением BASE_DIR.
                self.filepath = str(guard_path(raw_path, allowed_root=Path(raw_path).parent))
            else:
                self.filepath = str(guard_path(raw_path))
        except SecurityAccessViolation as e:
            # Fail-fast, не "тихий откат на дефолт" — если путь к реестру
            # состояния выходит за пределы BASE_DIR, это сигнал неверной
            # конфигурации (например, AI_BASE_DIR указывает куда-то наружу),
            # и продолжать с искажённым путём опаснее, чем громко упасть
            # здесь, на самом раннем этапе инициализации ядра.
            logger.critical("Registry filepath rejected by path_guard: %s", e)
            raise

        self._data_lock = threading.RLock()

        self.identity: dict[str, Any] = {
            "name": "kernel_core",
            "version": "1.0.0",
        }

        self.constraints: dict[str, Any] = {
            "max_runtime_s": 86400,
            "allowed_processes": ("bot", "watcher", "indexer"),
            "critical_threshold": 0.9,
        }

        self.state: dict[str, Any] = {
            "current_task": "IDLE",
            "uptime": 0,
            "status": "READY",
            "last_error": None,
            "narrative_state": "FACTUAL_MODE",
            "reflection_depth": 0,
            "compliance_violations": 0,
            "compliance_check_failures": 0,
            "total_requests": 0,
            "last_compliance_status": None,
            "quarantine_buffer": {},
            "input_blocked_count": 0,
            "last_alert_state_compliance": "NORMAL",
            "last_alert_state_input": "NORMAL",
            "pending_tasks": {},
            "transport_failures": 0,
            "stagnation_breaks": 0,
            "last_stagnation_reason": None,
        }

        self._load_checkpoint()
        self._initialized = True

    # ------------------------------------------------------------------
    # State access
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        with self._data_lock:
            return {"identity": self.identity, "state": dict(self.state)}

    def _read_disk_state(self) -> dict[str, Any]:
        """Читает текущее состояние с диска, не трогая self.state.

        Используется для merge при записи (см. update_state/increment_state)
        и для явного refresh_state(). Без этого шага второй процесс
        (run_system.py/watcher, knowledge_indexer как отдельный вызов),
        пишущий из своей устаревшей копии в памяти, целиком перезаписывал
        бы файл своим self.state — затирая изменения, сделанные другим
        процессом (bot.py) в промежутке между чтениями. Это подтверждённая
        причина двух наблюдавшихся симптомов: "воскрешение" уже
        одобренных/отклонённых pending_tasks и скачки total_requests между
        процессами (см. историю решений, диагностика 15 июля).

        Registry — process-local singleton (см. docstring модуля), это не
        делает его safe для нескольких процессов — просто уменьшает ущерб
        от слепой перезаписи. Полная защита потребовала бы file locking
        или перехода на Redis/SQLite (сознательно отложено, см. историю
        решений — не в рамках точечного патча).
        """
        if not os.path.exists(self.filepath):
            return {}
        try:
            with open(self.filepath, "r") as f:
                loaded = json.load(f)
            return {k: v for k, v in loaded.items() if k in ALLOWED_STATE_KEYS}
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Disk state read failed during merge, using in-memory only: %s", e)
            return {}

    def refresh_state(self) -> None:
        """Принудительно перечитывает состояние с диска, обновляя in-memory
        self.state поверх текущего.

        Нужно вызывать явно перед чтением ключей, которые может изменить
        ДРУГОЙ процесс — например, pending_tasks пишется из
        run_system.py/StagingManager, а читается из bot.py/staging_monitor_job.
        Это разные процессы с разными Registry-синглтонами (см. module
        docstring); get_state() сам по себе не перечитывает диск, чтобы не
        делать файловый I/O на каждое из множества обращений к состоянию
        внутри одного процесса — вызывающий явно решает, когда нужна
        гарантированная свежесть с диска.
        """
        with self._data_lock:
            disk_state = self._read_disk_state()
            self.state = {**self.state, **disk_state}

    def update_state(self, key: str, value: Any) -> bool:
        """Returns False on rejected write. Callers MUST check this.

        Двухуровневая проверка: сначала ключ (ALLOWED_STATE_KEYS — защита от
        опечаток в именах полей), потом значение (registry_schema.validate —
        защита от опечаток/неверных типов в данных, например 'RUNNUNG'
        вместо 'RUNNING'). Контракт метода не меняется — он остаётся bool,
        не бросает исключение, чтобы не ломать существующие вызовы в
        governor.py, которые ожидают проверяемый возврат, а не try/except.

        Merge с диском ПЕРЕД записью (см. _read_disk_state): другой процесс
        мог изменить другие ключи с момента, когда этот процесс в последний
        раз читал файл. Без этого шага следующий _save_checkpoint() слепо
        переписал бы их устаревшими значениями из памяти текущего процесса.
        Явно заданный key/value имеет приоритет над диском — это и есть
        запрошенная запись, она должна победить, даже если на диске для
        того же ключа успело появиться что-то ещё в узком окне гонки
        (полностью устранить это окно без file locking нельзя — см.
        docstring refresh_state).
        """
        with self._data_lock:
            if key not in ALLOWED_STATE_KEYS:
                logger.error("Rejected write to undeclared key: %s", key)
                return False

            ok, detail = registry_schema.validate(key, value)
            if not ok:
                logger.error("Rejected write to %s: %s", key, detail)
                return False

            disk_state = self._read_disk_state()
            self.state = {**self.state, **disk_state, key: value}
            self._save_checkpoint()
            return True

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._data_lock:
            return self.state.get(key, default)

    def increment_state(self, key: str, amount: int = 1) -> int | bool:
        """Атомарный инкремент под одним захватом блокировки.
        Использовать вместо паттерна get_state + update_state для счётчиков —
        тот паттерн допускает lost update между конкурентными запросами.
        Возвращает новое значение, либо False при недопустимом/нечисловом ключе.

        Merge с диском перед вычислением new_value — та же причина, что и в
        update_state: без этого счётчик отсчитывался бы от устаревшей
        in-memory базы текущего процесса, теряя инкременты, сделанные
        другим процессом. Полностью устранить гонку между процессами это
        не может (нет file locking — окно между чтением диска и записью
        остаётся), но значительно уменьшает частоту lost update по
        сравнению с использованием только in-memory self.state.
        """
        with self._data_lock:
            if key not in ALLOWED_STATE_KEYS:
                logger.error("Rejected increment on undeclared key: %s", key)
                return False
            disk_state = self._read_disk_state()
            current = disk_state.get(key, self.state.get(key, 0))
            if not isinstance(current, int):
                logger.error("increment_state called on non-int key: %s (%r)", key, current)
                return False
            new_value = current + amount
            self.state = {**self.state, **disk_state, key: new_value}
            self._save_checkpoint()
            return new_value

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_checkpoint(self) -> None:
        """Atomic write: tmp file + os.replace prevents corruption on crash
        mid-write.

        На Windows os.replace может кидать PermissionError, если целевой
        файл временно занят другим процессом (антивирус сканирует файл
        сразу после записи, индексатор Windows Search держит хендл, бэкап-
        софт). На POSIX этого ограничения нет — os.replace не блокируется
        чужими хендлами. Retry оборачивает ТОЛЬКО сам os.replace, не весь
        блок записи во временный файл — если оборачивать весь блок, второй
        проход попытался бы писать через уже закрытый file descriptor
        (with-блок os.fdopen закрывает fd при выходе, даже при исключении
        дальше по коду), что дало бы непредсказуемую ошибку вместо чистого
        PermissionError, который мы и пытаемся пережить.
        """
        dir_ = os.path.dirname(self.filepath) or "."
        try:
            fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".registry_tmp_")
        except Exception as e:
            logger.error("Checkpoint save failed (tempfile creation): %s", e)
            return

        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error("Checkpoint save failed (write to tmp file): %s", e)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return

        self._replace_with_retry(tmp_path)

    def _replace_with_retry(self, tmp_path: str) -> None:
        """Делегирует в io_retry.retry_on_permission_error — общую утилиту,
        используемую также knowledge_indexer.py для той же категории
        ошибок (Windows file-lock). Раньше эта логика была собственной
        копией внутри Registry; вынесена в общий модуль, чтобы не
        поддерживать два места с одной и той же retry-логикой, которые
        неизбежно расходятся в деталях со временем.

        ВАЖНО — блокирующий вызов внутри async-контекста: time.sleep()
        внутри retry_on_permission_error блокирующий, не asyncio.sleep().
        Registry — синхронный класс (используется и из bot.py напрямую,
        и из governor.py, где методы update_state/increment_state
        вызываются внутри async def без await). В худшем случае (реальный
        файловый конфликт на Windows, все 5 попыток исчерпаны) это
        блокирует ВЕСЬ event loop процесса на ~3.1с, не только текущий
        запрос. Осознанный компромисс — переписывать Registry на async def
        для редкого сценария не оправдано (см. историю решений).
        """
        try:
            success, _ = retry_on_permission_error(
                lambda: os.replace(tmp_path, self.filepath),
                max_attempts=config.IO_RETRY_MAX_ATTEMPTS,
                base_delay_s=config.IO_RETRY_BASE_DELAY_S,
                op_description="Checkpoint save (os.replace)",
            )
        except Exception as e:
            # retry_on_permission_error пробрасывает любое исключение,
            # кроме PermissionError, без повторных попыток (см. её docstring
            # и тест 3 при разработке io_retry.py) — структурная проблема
            # (нет прав на директорию, диск недействителен) не решается
            # повтором. Перехватываем здесь, чтобы гарантировать очистку
            # tmp_path в любом случае — без этого исключение вылетело бы
            # из _save_checkpoint, оставив временный файл на диске.
            logger.error("Checkpoint save failed (os.replace): %s", e)
            success = False

        if not success and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    def _load_checkpoint(self) -> None:
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, "r") as f:
                loaded = json.load(f)
            for k, v in loaded.items():
                if k in ALLOWED_STATE_KEYS:
                    self.state[k] = v
            logger.info("Checkpoint restored from %s", self.filepath)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Checkpoint load failed, starting clean: %s", e)

    # ------------------------------------------------------------------
    # Boot diagnostics
    # ------------------------------------------------------------------

    def boot_check(self) -> BootCheckResult:
        """Fail-fast checks: config validity, filesystem, network reachability.
        Does NOT check model readiness — see check_model_loaded for that.
        """
        failures: list[str] = []

        if not config.TELEGRAM_TOKEN:
            failures.append("TELEGRAM_TOKEN not set")

        check_dir = os.path.dirname(self.filepath) or "."
        write_ok, write_detail = self._verify_writable(check_dir)
        if not write_ok:
            failures.append(write_detail)

        try:
            with socket.create_connection(
                (config.OLLAMA_HOST, config.OLLAMA_PORT), timeout=3.0
            ):
                pass
        except OSError as e:
            failures.append(
                f"LLM endpoint {config.OLLAMA_HOST}:{config.OLLAMA_PORT} unreachable: {e}"
            )

        return BootCheckResult(ok=not failures, failures=failures)

    @staticmethod
    def _verify_writable(check_dir: str) -> tuple[bool, str]:
        """Поведенческая проверка прав на запись — реальная попытка создать
        и удалить временный файл, не декларативная проверка через os.access.

        os.access(dir, os.W_OK) на Windows известен ложноположительными
        срабатываниями: может вернуть True для директорий, где реальная
        запись позже упадёт (UAC-контролируемые папки, некоторые сетевые
        share с особыми ACL). Поведенческая проверка устраняет этот разрыв
        между "ОС говорит что можно" и "запись реально проходит".

        Дополнительный побочный эффект: если check_dir указывает на
        несуществующий диск (например, конфиг скопирован с машины, где был
        D:, на машину только с C:), tempfile.mkstemp здесь упадёт с понятной
        ошибкой ОС — отдельную проверку "диск существует" писать не нужно,
        она покрывается этой же попыткой создания файла.
        """
        try:
            fd, tmp_path = tempfile.mkstemp(dir=check_dir, prefix=".boot_check_")
            os.close(fd)
            os.unlink(tmp_path)
            return True, ""
        except OSError as e:
            return False, f"Cannot write to {check_dir}: {e}"

    def check_model_loaded(self, timeout_s: float = 5.0) -> tuple[bool, str]:
        """Single-shot check against Ollama's /api/tags.
        Does not retry — caller controls retry policy (see bot.wait_for_model).
        """
        url = f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/api/tags"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                if resp.status != 200:
                    return False, f"Ollama /api/tags returned HTTP {resp.status}"
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            return False, f"Ollama unreachable: {e}"
        except (json.JSONDecodeError, ValueError) as e:
            return False, f"Ollama returned malformed body: {e}"

        models = {m.get("name", "") for m in body.get("models", [])}
        target = config.OLLAMA_MODEL
        if not any(m == target or m.startswith(f"{target}:") for m in models):
            return False, f"Model '{target}' not loaded. Available: {sorted(models) or 'none'}"

        return True, f"Model '{target}' ready"
