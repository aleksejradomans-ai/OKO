"""logger.py — единая точка настройки логирования.

Устраняет конкретный, подтверждённый риск (не гипотетический): logging.
basicConfig() в Python имеет эффект ТОЛЬКО при первом вызове в процессе —
повторные вызовы с другим format= молча игнорируются (подтверждено
тестом при разработке). Раньше bot.py, run_system.py и process_watchdog.py
каждый вызывал basicConfig() независимо, с разными форматами (bot.py:
"%(asctime)s - %(levelname)s - %(message)s", run_system.py: тот же формат
без тире-разделителей) — то, какой формат реально победит, зависело от
того, какая точка входа запущена, что в данной топологии (отдельные
процессы на отдельные systemd-юниты) не создавало реального конфликта
само по себе, но было хрупким: расширение формата в одном месте без
синхронизации с другим легко создаёт расхождение, которое
process_watchdog.py's _parse_log_timestamp может не переварить.

Уточнение по guardrails.py: его собственный logging.basicConfig() находится
внутри `if __name__ == "__main__":` — проверено реальным тестом (import
guardrails не добавляет handler'ов корневому logger'у), он НЕ срабатывает
при обычном импорте через initialize_guardrail(). Это не было риском гонки
конфигурации, как изначально предполагалось при проектировании этого
модуля — исправлено здесь, чтобы не оставлять неточное утверждение.

setup_logging() вызывается РОВНО ОДИН РАЗ, в самом начале main() каждой
точки входа (bot.py, run_system.py, process_watchdog.py) — не в каждом
модуле, который импортируется. Остальные модули вызывают только
get_logger(__name__), не настраивают формат сами.
"""
from __future__ import annotations

import logging
import sys

# Единый формат для всего проекта — детерминированный, парсимый
# process_watchdog.py._parse_log_timestamp (читает первые 23 символа как
# "YYYY-MM-DD HH:MM:SS,fff"). Если этот формат меняется — нужно синхронно
# обновить _parse_log_timestamp в process_watchdog.py, иначе error-rate
# мониторинг тихо перестанет находить ошибки (тот же класс риска, который
# этот модуль и существует устранять).
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    """Настраивает корневой logger РОВНО ОДИН РАЗ за жизнь процесса.

    Повторные вызовы — no-op с явным предупреждением в лог, не молчаливое
    игнорирование (в отличие от стандартного logging.basicConfig, чьё
    молчаливое поведение при повторном вызове и было корнем найденной
    проблемы). Это позволяет точкам входа звать setup_logging() в своём
    main() без необходимости помнить, кто и в каком порядке его уже вызвал.

    StandardOutput/StandardError перенаправляются systemd в файл на уровне
    deploy/*.service (см. историю решений) — этот модуль пишет в stdout/
    stderr через стандартный StreamHandler, не открывает файлы сам. Если
    логирование запускается не под systemd (например, локальный запуск
    для разработки) — вывод просто идёт в консоль, тоже корректно.
    """
    global _configured
    if _configured:
        logging.getLogger(__name__).warning(
            "setup_logging() called more than once in this process — ignoring "
            "repeated call. This may indicate two entry-point main()s running "
            "in the same process, which is not the expected topology."
        )
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Возвращает logger для модуля. Не настраивает формат/handler — это
    ответственность setup_logging(), вызванного один раз точкой входа.
    Если setup_logging() ещё не был вызван (например, модуль импортирован
    в unit-тесте без точки входа), logger всё равно работает — просто
    использует дефолтную конфигурацию Python logging (вывод в stderr без
    явного формата), не падает и не требует try/except от вызывающего кода.
    """
    return logging.getLogger(name)
