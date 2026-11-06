"""EVENT_LOG (этап C, 01.11.2026): события модулей бота -> факты для мозга.

Проблема (глобальный тест 30.10): «Сушильная: запущена «Эко-хлопок», закончит около 17:44»
приходит в Telegram, но мозг о нём не знает -> «когда закончит сушилка?» без ответа;
ответы маршрутизатора (ТВ, Garmin) в историю не пишутся -> «что ты сделал с подсветкой?»
без ответа.

Почему НЕ в журнал памяти (memory_core.events):
- там роли только user/assistant (CHECK в схеме) -- событие не реплика; запись парой
  выдуманных реплик -- ложь в журнале и шум для P1;
- журнал -- память о ЧЕЛОВЕКЕ (subject), а техника -- общая для дома.
Поэтому отдельный журнал событий: append-only JSONL (TTL=0, как proof_registry), хвост в
памяти процесса. В контекст мозга идёт короткий блок фактов (facts_block) рядом с RAG --
в канале «справочных данных» (данные, не инструкции).

Модель чтения: при init() читается только ХВОСТ файла (TAIL_BYTES): файл растёт без предела,
полный проход на каждом старте со временем дорожает. В памяти -- последние KEEP событий.
Запись: одна строка под блокировкой; ошибка записи -- WARNING, событие остаётся в памяти.
Текст пользователя сюда не пишется: только ответы модулей.
"""
from __future__ import annotations

import collections
import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

ENV_PATH = "OKO_EVENT_LOG"
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"
TAIL_BYTES = 128 * 1024
KEEP = 200
MAX_TEXT = 300
FACT_TEXT = 140
LABELS = {"devices": "Техника", "tv": "ТВ", "garmin": "Garmin", "net": "Сеть"}

logger = logging.getLogger("kernel.event_log")


def default_path() -> Path:
    raw = os.getenv(ENV_PATH, "").strip()
    return Path(raw) if raw else DEFAULT_PATH


def _now() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def _clean(text: str, limit: int) -> str:
    t = " | ".join(l.strip() for l in str(text or "").splitlines() if l.strip())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _ts(rec: dict) -> Optional[_dt.datetime]:
    try:
        t = _dt.datetime.fromisoformat(rec.get("ts", ""))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


class EventLog:
    def __init__(self, path: Optional[Path] = None, keep: int = KEEP) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._lock = threading.Lock()
        self._items: collections.deque = collections.deque(maxlen=keep)
        self.bad_lines = 0
        self._load_tail()

    def _load_tail(self) -> None:
        t0 = time.monotonic()
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as f:
                f.seek(max(0, size - TAIL_BYTES))
                chunk = f.read()
        except FileNotFoundError:
            size, chunk = 0, b""
        except OSError as e:               # путь недоступен -- журнал только в памяти, бот работает
            logger.warning("[EVENTS] read failed: %s: %s", type(e).__name__, e)
            size, chunk = 0, b""
        lines = chunk.split(b"\n")
        if size > TAIL_BYTES and lines:
            lines = lines[1:]              # первая строка хвоста может быть обрезана
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                rec = None
            if not (isinstance(rec, dict) and isinstance(rec.get("src"), str)
                    and isinstance(rec.get("text"), str) and _ts(rec) is not None):
                self.bad_lines += 1
                continue
            self._items.append(rec)
        logger.info("[EVENTS] loaded n=%d bytes=%d ms=%d", len(self._items), size,
                    int((time.monotonic() - t0) * 1000))

    def record(self, src: str, text: str, ts: Optional[str] = None) -> dict:
        if not isinstance(src, str) or not src:
            raise ValueError("src must be a non-empty string")
        rec = {"ts": ts or _now().isoformat(timespec="seconds"), "src": src,
               "text": _clean(text, MAX_TEXT)}
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            self._items.append(rec)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "ab") as f:
                    f.write(line)
            except OSError as e:
                logger.warning("[EVENTS] write failed: %s: %s", type(e).__name__, e)
        return rec

    def recent(self, n: int = 8, within_s: float = 86400.0, src: Optional[str] = None,
               now: Optional[_dt.datetime] = None) -> list[dict]:
        now = now or _now()
        with self._lock:
            items = list(self._items)
        out = []
        for rec in reversed(items):
            if src is not None and rec.get("src") != src:
                continue
            t = _ts(rec)
            if t is None or (now - t).total_seconds() > within_s:
                continue
            out.append(dict(rec))
            if len(out) >= n:
                break
        return list(reversed(out))


_DEFAULT: Optional[EventLog] = None


def init(path: Optional[Path] = None) -> EventLog:
    """Один раз при старте бота (post_init). До init() модуль молчит: record -- no-op, recent -- []."""
    global _DEFAULT
    _DEFAULT = EventLog(path)
    return _DEFAULT


def reset() -> None:
    global _DEFAULT
    _DEFAULT = None


def record(src: str, text: str) -> None:
    """Не бросает: сбой журнала событий не должен ронять задачу бота."""
    if _DEFAULT is None:
        return
    try:
        _DEFAULT.record(src, text)
    except Exception as e:  # noqa: BLE001
        logger.warning("[EVENTS] record failed: %s: %s", type(e).__name__, e)


def recent(n: int = 8, within_s: float = 86400.0, src: Optional[str] = None,
           now: Optional[_dt.datetime] = None) -> list[dict]:
    if _DEFAULT is None:
        return []
    return _DEFAULT.recent(n, within_s, src, now)


def line(rec: dict) -> str:
    t = _ts(rec)
    when = t.strftime("%d.%m %H:%M") if t else "?"
    return f"[{when}] {LABELS.get(rec.get('src'), rec.get('src'))}: {_clean(rec.get('text', ''), FACT_TEXT)}"


def facts_block(device_state: str = "", now: Optional[_dt.datetime] = None, n: int = 50,
                within_s: float = 86400.0) -> str:
    """Блок для контекста мозга. Пусто, если нет ни состояния техники, ни событий за сутки.
    01.11 (глобальный тест): только ПОСЛЕДНЕЕ событие каждого источника. 8 событий подряд дали
    fill окна 0.84 (pe 3447/4096) и образец для подделки ответа модуля мозгом."""
    now = now or _now()
    latest: dict = {}
    for r in recent(n, within_s, now=now):
        latest.pop(r.get("src"), None)
        latest[r.get("src")] = r              # порядок -- по времени последнего события источника
    ev = list(latest.values())
    if not ev and not device_state:
        return ""
    lines = [f"Факты дома от модулей бота (не слова собеседника). Сейчас {now:%d.%m %H:%M}."]
    if device_state:
        lines.append("Техника сейчас: " + device_state)
    if ev:
        lines.append("Последние события (новые снизу):")
        lines += [line(r) for r in ev]
    return "\n".join(lines)
