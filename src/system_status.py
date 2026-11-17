"""Живой снимок состояния системы для контекста LLM.

Не переиспользует monitor.py (метрики качества ответов, in-memory в
другом процессе) и не переиспользует process_watchdog.py (сторожевой
процесс над конкретным PID, ничего не отдаёт как читаемое состояние,
следит за bot.py, не за этим процессом). Это отдельная, более простая
задача: разовый снимок "что сейчас с ресурсами", на каждый вопрос
заново — как build_context() в RAG, не кэшируется при старте.

psutil уже используется в проекте (process_watchdog.py), новая
зависимость не добавляется.
"""
from __future__ import annotations

import psutil
import httpx


def get_status_line(ollama_host: str, ollama_port: int, timeout: float = 2.0) -> str:
    """Возвращает готовую строку для вклейки в промпт. Не бросает исключений
    наружу — если Ollama недоступна или psutil на что-то пожалуется, это
    само по себе часть статуса ("Ollama: Down"), не повод падать."""
    cpu_percent = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    ram_used_gb = mem.used / (1024 ** 3)
    ram_total_gb = mem.total / (1024 ** 3)

    ollama_active = False
    try:
        resp = httpx.get(f"http://{ollama_host}:{ollama_port}/api/tags", timeout=timeout)
        ollama_active = resp.status_code == 200
    except httpx.RequestError:
        ollama_active = False

    return (
        f"[CPU: {cpu_percent:.0f}% | RAM: {ram_used_gb:.1f}/{ram_total_gb:.1f}GB | "
        f"Ollama: {'Active' if ollama_active else 'Down'}]"
    )
