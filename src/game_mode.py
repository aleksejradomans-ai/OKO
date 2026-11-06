"""GAME_MODE v2 (02.11.2026): игровой режим без остановки OkoBot.

Контракт с game_watcher.ps1 (два файла в data\\, межпроцессный канал -- исключение из X4):
  game_mode.flag   -- пишет watcher, пока идёт игра; перезаписывает каждый опрос (сердцебиение, mtime).
  brain_force.flag -- пишет бот по /force_brain («v2 until=<unix>»); watcher, пока он действует,
                      не выгружает модель; при выходе из игры watcher его удаляет.

Режим мозга (brain_mode):
  disabled -- OKO_GAME_MODE=0 (флаг игнорируется, откат);
  awake    -- флага нет;
  stale    -- флаг старше OKO_GAME_FLAG_STALE_S (watcher умер) -> fail-open (решение R1), мозг работает;
  force    -- игра, но действует /force_brain;
  sleep    -- игра: история/RAG/LLM/эмбеддинги не трогаются, ответ -- SLEEP_TEXT.
Горячий путь -- только stat() флага; содержимое читается лишь у force-флага и только в игре.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

import logger as oko_logger

logger = oko_logger.get_logger("game_mode")

DISABLED, AWAKE, STALE, FORCE, SLEEP = "disabled", "awake", "stale", "force", "sleep"

# Решение Оператора R2 (02.11).
SLEEP_TEXT = (
    "⚠️ Игровой режим активен (ресурсы видеокарты отданы под игры). Мозг временно отключен, "
    "чтобы не просаживать FPS. Если это осознанное решение и вам срочно нужен ответ модели, "
    "отправьте команду принудительного пробуждения: /force_brain"
)

_UNTIL_RE = re.compile(r"(?<![\w])until=(\d{1,12})(?![\d])")
_last_mode: dict = {"mode": None}


def flag_age(flag: Path, now: float) -> Optional[float]:
    """Возраст флага по mtime, с; None -- флага нет (или недоступен: fail-open)."""
    try:
        mtime = Path(flag).stat().st_mtime
    except OSError:
        return None
    return max(0.0, now - mtime)


def force_until(path: Path, now: float) -> Optional[float]:
    """Срок /force_brain, если он ещё в будущем; иначе None (нет файла, битый, истёк)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _UNTIL_RE.search(text)
    if not m:
        return None
    until = float(m.group(1))
    return until if until > now else None


def write_force(path: Path, until: float) -> None:
    """Атомарно (tmp + os.replace): watcher не прочтёт половину строки."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(f"v2 until={int(until)}\n", encoding="ascii")
    os.replace(tmp, p)


def clear_force(path: Path) -> bool:
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False


def _note(mode: str, age: Optional[float]) -> None:
    """Лог только при смене режима (не на каждый запрос)."""
    prev = _last_mode["mode"]
    if mode == prev:
        return
    _last_mode["mode"] = mode
    if mode == STALE:
        logger.warning("[GAME_MODE] stale_flag age_s=%.0f -> brain awake (fail-open)", age or 0.0)
    else:
        logger.info("[GAME_MODE] brain_mode=%s (was %s)", mode, prev)


def brain_mode(cfg, now: Optional[float] = None) -> str:
    """cfg -- модуль config (или его подмена в тестах). Нет атрибутов -> disabled."""
    if not getattr(cfg, "OKO_GAME_MODE", False):
        return DISABLED
    flag = getattr(cfg, "OKO_GAME_FLAG", None)
    if flag is None:
        return DISABLED
    now = time.time() if now is None else now
    age = flag_age(flag, now)
    if age is None:
        mode = AWAKE
    elif age > float(getattr(cfg, "OKO_GAME_FLAG_STALE_S", 120.0)):
        mode = STALE
    else:
        force = getattr(cfg, "OKO_GAME_FORCE_FLAG", None)
        mode = FORCE if (force is not None and force_until(force, now) is not None) else SLEEP
    _note(mode, age)
    return mode


def brain_asleep(cfg, now: Optional[float] = None) -> bool:
    return brain_mode(cfg, now) == SLEEP


def in_game(cfg, now: Optional[float] = None) -> bool:
    """Игра идёт (флаг свежий), независимо от /force_brain. Для фоновых задач."""
    return brain_mode(cfg, now) in (SLEEP, FORCE)
