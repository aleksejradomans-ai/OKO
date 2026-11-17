"""Retry-обёртка для файловых операций, подверженных временной блокировке
хендла на Windows (антивирус сканирует файл сразу после создания, индексатор
Windows Search держит хендл, бэкап-софт). На POSIX PermissionError на этих
операциях практически не возникает по той же причине — retry-цикл там почти
всегда завершается на первой попытке, не вредит, просто не часто нужен.

Вынесено в отдельный модуль из registry.py, где эта логика появилась впервые
(см. историю решений) — чтобы knowledge_indexer.py и другие точки файлового
I/O могли использовать ТУ ЖЕ функцию, не копию со своим набором тонких
отличий, которые неизбежно расходятся при поддержке двух копий одной логики.
"""
from __future__ import annotations

import logger as oko_logger
import random
import time
from typing import Callable, Optional, TypeVar

logger = oko_logger.get_logger("kernel.io_retry")

T = TypeVar("T")


def retry_on_permission_error(
    operation: Callable[[], T],
    *,
    max_attempts: int = 5,
    base_delay_s: float = 0.1,
    jitter_s: float = 0.05,
    op_description: str = "file operation",
) -> tuple[bool, Optional[T]]:
    """Выполняет operation(), повторяя при PermissionError с экспоненциальной
    задержкой + джиттером. Возвращает (success, result) — result is None при
    неудаче после всех попыток, или при операции, не возвращающей значения
    (os.replace/os.remove возвращают None и при успехе — это нормально,
    success-флаг отличает "вернул None потому что упал" от "вернул None
    потому что так и должно быть").

    Джиттер (±jitter_s, равномерно распределённый) защищает от thundering
    herd: без него несколько потоков/процессов, попавших в retry от одной
    и той же причины блокировки (например, антивирус просканировал сразу
    несколько новых файлов), просыпались бы синхронно в одинаковые моменты
    и заново конфликтовали друг с другом за тот же ресурс.

    Только PermissionError перехватывается и повторяется — это специфика
    временно занятого хендла. Любая другая ошибка (нет прав на директорию,
    диск не существует) — структурная проблема, retry её не решит, она
    поднимается вызывающему коду немедленно, без лишних попыток.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            result = operation()
            return True, result
        except PermissionError as e:
            if attempt == max_attempts:
                logger.error(
                    "%s failed after %d attempts (file locked?): %s",
                    op_description, max_attempts, e,
                )
                return False, None
            delay = base_delay_s * (2 ** (attempt - 1))
            jitter = random.uniform(-jitter_s, jitter_s)
            actual_delay = max(0.0, delay + jitter)
            logger.warning(
                "%s blocked (attempt %d/%d), retrying in %.2fs: %s",
                op_description, attempt, max_attempts, actual_delay, e,
            )
            time.sleep(actual_delay)

    return False, None
