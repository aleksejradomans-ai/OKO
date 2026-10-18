"""process_watchdog.py — sidecar-процесс мониторинга здоровья целевого процесса.

НЕ ПУТАТЬ с библиотекой watchdog (файловый мониторинг, см. run_system.py) —
это отдельный, наш собственный скрипт. Имя "Governor" для этой роли
отклонено (см. историю решений) — Governor уже занят AgentGovernor
(LLM-оркестрация), называть этим именем supervisor процесса создало бы
путаницу в коде, которую невозможно было бы устранить без переименования
одного из двух.

Разделение ответственности (см. историю решений):
- systemd/NSSM отвечает за БИНАРНОЕ состояние процесса (запущен/упал,
  Restart=on-failure уже настроен в deploy/*.service).
- process_watchdog.py отвечает за ЛОГИКУ здоровья (норма/деградация) —
  heartbeat timeout, error rate в логах, memory ceiling.

Watchdog НЕ вызывает SnapshotManager.rollback() сам. При обнаружении
деградации он шлёт SIGTERM целевому процессу и логирует причину с явной
инструкцией для оператора — откат кода требует решения человека.
Автоматический rollback на основании метрики создаёт риск "качелей"
(rollback -> auto-restart -> crash -> rollback), что хуже простой остановки.

Один экземпляр watchdog отслеживает ОДИН целевой процесс — не оба
(bot.py и run_system.py) одним watchdog'ом. Это сохраняет ту же изоляцию
сбоев, ради которой у нас два отдельных systemd-юнита для основных
процессов: единый watchdog для обоих был бы новой единой точкой отказа,
противоречащей этому принципу.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import psutil

import logger as oko_logger

logger = oko_logger.get_logger("kernel.process_watchdog")


class WatchdogConfig:
    def __init__(
        self,
        target_pid_file: Path,
        heartbeat_file: Path,
        target_log_file: Path,
        memory_limit_mb: int,
        error_rate_limit: int,
        error_window_sec: float,
        heartbeat_timeout_sec: float,
        check_interval_sec: float,
    ) -> None:
        self.target_pid_file = target_pid_file
        self.heartbeat_file = heartbeat_file
        self.target_log_file = target_log_file
        self.memory_limit_mb = memory_limit_mb
        self.error_rate_limit = error_rate_limit
        self.error_window_sec = error_window_sec
        self.heartbeat_timeout_sec = heartbeat_timeout_sec
        self.check_interval_sec = check_interval_sec


class DegradationDetected(Exception):
    """Поднимается одной из проверок при обнаружении деградации.
    Сообщение исключения — человекочитаемая причина, идёт в лог и в
    инструкцию оператору."""


def read_target_pid(pid_file: Path) -> Optional[int]:
    """Читает PID целевого процесса из файла. Возвращает None, если файл
    не существует или содержит не-число — это не ошибка watchdog'а самого
    по себе (целевой процесс может ещё не успеть стартовать), но проверки
    дальше не могут выполняться без PID."""
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def check_heartbeat(config: WatchdogConfig) -> None:
    """Проверяет mtime heartbeat-файла. Целевой процесс обязан обновлять
    его периодически (например, через os.utime каждые 30с) — если файл
    не обновлялся дольше heartbeat_timeout_sec, главный поток считается
    зависшим (deadlock).

    Отсутствие самого файла — НЕ деградация (целевой процесс мог ещё не
    стартовать или ещё не дошёл до первого heartbeat) — поднимать
    DegradationDetected здесь было бы ложным срабатыванием на старте.
    """
    if not config.heartbeat_file.exists():
        logger.debug("Heartbeat file does not exist yet: %s", config.heartbeat_file)
        return

    mtime = config.heartbeat_file.stat().st_mtime
    elapsed = time.time() - mtime
    if elapsed > config.heartbeat_timeout_sec:
        raise DegradationDetected(
            f"Heartbeat timeout: {elapsed:.0f}s since last update "
            f"(limit={config.heartbeat_timeout_sec:.0f}s) — target process appears stalled"
        )


def check_error_rate(config: WatchdogConfig) -> None:
    """Считает строки с 'ERROR'/'CRITICAL' в логе за последние
    error_window_sec секунд. Читает файл целиком — приемлемо для разумного
    размера лога, не для логов в гигабайты (если лог вырастет настолько,
    это отдельная задача ротации логов, не предмет этой проверки).

    Отсутствие лог-файла — НЕ деградация, по той же причине, что и
    отсутствие heartbeat-файла (процесс мог не успеть его создать).
    """
    if not config.target_log_file.exists():
        logger.debug("Target log file does not exist yet: %s", config.target_log_file)
        return

    cutoff = time.time() - config.error_window_sec
    error_count = 0

    try:
        with open(config.target_log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not _is_red_line(line):
                    continue
                line_time = _parse_log_timestamp(line)
                if line_time is not None and line_time >= cutoff:
                    error_count += 1
    except OSError as e:
        logger.warning("Could not read target log file: %s", e)
        return

    if error_count > config.error_rate_limit:
        raise DegradationDetected(
            f"Error rate exceeded: {error_count} ERROR/CRITICAL lines in last "
            f"{config.error_window_sec:.0f}s (limit={config.error_rate_limit})"
        )


# PATCH_V2_STAGNATION: светофор. Красными считаются ТОЛЬКО строки уровня
# ERROR/CRITICAL, определённого по полю "[LEVEL]" формата лога, а не по
# подстроке: жёлтая WARNING-строка, в тексте которой встретилось слово ERROR,
# раньше засчитывалась бы как красная. Строка без распознаваемого "[LEVEL]"
# (чужой формат) трактуется по-старому, по подстроке -- fail-safe.
RED_LEVELS = frozenset({"ERROR", "CRITICAL"})
_LEVEL_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[([A-Z]+)\]")


def _is_red_line(line: str) -> bool:
    m = _LEVEL_RE.match(line)
    if m:
        return m.group(1) in RED_LEVELS
    return "ERROR" in line or "CRITICAL" in line


def _parse_log_timestamp(line: str) -> Optional[float]:
    """Парсит timestamp из строки лога. Формат строки определяется
    logger.LOG_FORMAT (единая точка истины, см. logger.py) — здесь
    жёстко предполагается, что timestamp всегда занимает первые 23
    символа строки ('%Y-%m-%d %H:%M:%S,%f'), что верно для текущего
    LOG_FORMAT. Если LOG_FORMAT изменится (например, порядок полей или
    формат даты) — эту функцию нужно обновить синхронно, иначе
    check_error_rate тихо перестанет находить ошибки (тот же класс
    риска, который logger.py создан устранять — расхождение между
    источником формата и его потребителями).
    """
    import datetime

    try:
        timestamp_part = line[:23]  # "YYYY-MM-DD HH:MM:SS,fff"
        dt = datetime.datetime.strptime(timestamp_part, "%Y-%m-%d %H:%M:%S,%f")
        return dt.timestamp()
    except ValueError:
        return None


def check_memory(config: WatchdogConfig, pid: int) -> None:
    """Проверяет RSS память целевого процесса через psutil — кросс-
    платформенно (Linux/Windows читают память процесса совершенно
    по-разному на низком уровне, psutil абстрагирует это).

    psutil.NoSuchProcess пробрасывается дальше — main loop отличает это
    от деградации (процесс уже не существует, не "процесс деградировал").
    """
    process = psutil.Process(pid)
    rss_mb = process.memory_info().rss / (1024 * 1024)

    if rss_mb > config.memory_limit_mb:
        raise DegradationDetected(
            f"Memory limit exceeded: {rss_mb:.0f}MB > {config.memory_limit_mb}MB"
        )


def terminate_target(pid: int, reason: str) -> None:
    """Шлёт SIGTERM целевому процессу. НЕ вызывает SnapshotManager — это
    единственное действие watchdog'а при деградации, дальше решает
    supervisor (systemd/NSSM) и оператор (см. docstring модуля)."""
    logger.critical(
        "[WATCHDOG] Sending SIGTERM to pid=%d. Reason: %s. "
        "System state at risk — manual verification required: "
        "check `cli_patch.py rollback --snapshot-id <id>` if a recent patch is suspect.",
        pid, reason,
    )
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        logger.warning("Process pid=%d already gone before SIGTERM could be sent", pid)
    except PermissionError as e:
        logger.error("Could not send SIGTERM to pid=%d: %s", pid, e)


def run_check_cycle(config: WatchdogConfig, last_terminated: dict) -> None:
    """Один цикл всех трёх проверок. Поднятое DegradationDetected из любой
    из них прерывает цикл и ведёт к terminate_target — не накапливаем
    несколько причин за один цикл, первая найденная достаточна.

    last_terminated — изменяемый словарь {pid: timestamp последнего SIGTERM},
    переданный извне (не глобальное состояние модуля) — защита от повторной
    отправки SIGTERM на каждом цикле проверки, пока процесс не успел
    завершиться после первого сигнала. Подтверждено реальным тестом: без
    этой защиты watchdog слал SIGTERM на каждой итерации check_interval_sec,
    пока процесс не исчезал из ОС — не опасно для SIGTERM как такового, но
    шумит в логе и было бы реальной проблемой для менее безопасного сигнала.
    Грейс-период — 2x check_interval_sec, не константа: после медленного
    цикла проверки (например, чтение большого лог-файла) нужно пропорционально
    больше времени, чтобы не отправить повторный сигнал раньше, чем процесс
    успел отреагировать на первый.
    """
    pid = read_target_pid(config.target_pid_file)
    if pid is None:
        logger.debug("No target PID file yet — target process may not have started.")
        return

    if not psutil.pid_exists(pid):
        logger.warning("Target pid=%d from PID file no longer exists. Nothing to monitor.", pid)
        last_terminated.pop(pid, None)
        return

    grace_period = config.check_interval_sec * 2
    last_sent = last_terminated.get(pid)
    if last_sent is not None and (time.time() - last_sent) < grace_period:
        logger.debug("Recently sent SIGTERM to pid=%d, within grace period — skipping checks", pid)
        return

    try:
        check_heartbeat(config)
        check_error_rate(config)
        check_memory(config, pid)
    except DegradationDetected as e:
        terminate_target(pid, str(e))
        last_terminated[pid] = time.time()
    except psutil.NoSuchProcess:
        logger.warning("Target pid=%d disappeared during check cycle.", pid)
        last_terminated.pop(pid, None)


def main() -> int:
    parser = argparse.ArgumentParser(description="process_watchdog.py — sidecar health monitor")
    parser.add_argument("--target-pid-file", required=True, type=Path)
    parser.add_argument("--heartbeat-file", required=True, type=Path)
    parser.add_argument("--target-log-file", required=True, type=Path)
    parser.add_argument("--memory-limit-mb", type=int, default=512)
    parser.add_argument("--error-rate-limit", type=int, default=5)
    parser.add_argument("--error-window-sec", type=float, default=60.0)
    parser.add_argument("--heartbeat-timeout-sec", type=float, default=120.0)
    parser.add_argument("--check-interval-sec", type=float, default=10.0)
    args = parser.parse_args()

    oko_logger.setup_logging()

    config = WatchdogConfig(
        target_pid_file=args.target_pid_file,
        heartbeat_file=args.heartbeat_file,
        target_log_file=args.target_log_file,
        memory_limit_mb=args.memory_limit_mb,
        error_rate_limit=args.error_rate_limit,
        error_window_sec=args.error_window_sec,
        heartbeat_timeout_sec=args.heartbeat_timeout_sec,
        check_interval_sec=args.check_interval_sec,
    )

    logger.info("process_watchdog started, watching pid file: %s", config.target_pid_file)

    last_terminated: dict = {}
    try:
        while True:
            # ИСПРАВЛЕНО (см. историю решений, аудит блока №4): раньше
            # run_check_cycle() не была защищена от собственных исключений
            # внутри while-цикла — любая ошибка за пределами явно
            # обработанных внутри неё (DegradationDetected, psutil.NoSuchProcess)
            # пробрасывалась наружу и убивала ВЕСЬ процесс watchdog целиком,
            # не только один цикл проверки. Конкретные реальные сценарии:
            # psutil.AccessDenied (Windows, если watchdog запущен под другой
            # учётной записью, чем целевой процесс), OSError из check_heartbeat
            # при гонке между Path.exists() и Path.stat() (heartbeat-файл
            # исчезает между двумя вызовами). Единственный внешний наблюдатель
            # системы не должен падать от того же класса транзиентных ошибок,
            # от которых он должен защищать остальную систему — это
            # противоречит собственной философии "иммунной системы" из
            # начала проекта (self-diagnostic слой не должен быть более
            # хрупким, чем то, что он диагностирует).
            #
            # try/except здесь НЕ глушит DegradationDetected и
            # psutil.NoSuchProcess — те уже обработаны внутри run_check_cycle
            # и никогда сюда не долетают. Этот except ловит только то, что
            # НЕ было предусмотрено при разработке run_check_cycle — по
            # определению неожиданное, поэтому logger.exception (не .error)
            # для полного traceback, необходимого для диагностики нового
            # класса ошибки, который проявился впервые.
            try:
                run_check_cycle(config, last_terminated)
            except Exception:
                logger.exception(
                    "Unexpected error in watchdog check cycle — continuing to next "
                    "cycle rather than crashing the watchdog itself. If this repeats "
                    "persistently, investigate immediately: an unmonitored target "
                    "process is a silent single point of failure."
                )
            time.sleep(config.check_interval_sec)
    except KeyboardInterrupt:
        logger.info("process_watchdog stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
