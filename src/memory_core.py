"""Ядро памяти «Ока»: персистентный слепок, независимый от транспортного каркаса.

Инварианты:
1. Каркас (Telegram, консоль, будущий шлюз) не знает про SQLite. Он передаёт
   Envelope и получает текст. Смена или падение каркаса не трогает ядро.
2. Ключ памяти — локальный subject_id, а не id транспорта. Один человек в
   разных каналах = одна память (устраняет split-brain bot/console).
3. Журнал событий (events) — единственный источник истины. Векторный индекс
   производный: его можно потерять и пересобрать из журнала.
4. Запись в журнал не зависит от доступности Ollama: событие коммитится
   первым, индексация — после; при сбое остаётся в index_queue.
5. В журнал попадает только текст после слоя B (DataHygieneFilter).
6. Производных ярлыков о человеке ядро не строит. Хранится то, что сказано,
   и векторы этого текста. inventory() отвечает на «что хранится» из БД.
7. «Память священна» = защищена от случайной потери. Удаление возможно
   только явной командой владельца: forget().
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Protocol, Tuple

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subjects (subject_id TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS aliases (
    channel TEXT NOT NULL, external_id TEXT NOT NULL,
    subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
    PRIMARY KEY (channel, external_id)
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL REFERENCES subjects(subject_id) ON DELETE CASCADE,
    channel TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content TEXT NOT NULL, content_hash TEXT NOT NULL, ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_subject_ts ON events(subject_id, ts);
CREATE TABLE IF NOT EXISTS index_queue (
    event_id INTEGER PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
"""


@dataclass(frozen=True)
class Envelope:
    """Контракт каркаса. Всё, что транспорт обязан передать ядру."""
    channel: str          # "telegram" | "console" | ...
    external_id: str      # id в терминах транспорта
    text: str


class SemanticIndex(Protocol):
    """Производный индекс (Chroma и т.п.). Может отказать — ядро это переживёт."""
    def upsert(self, event_id: int, subject_id: str, text: str) -> None: ...
    def delete_subject(self, subject_id: str) -> None: ...
    def search(self, subject_id: str, query: str, k: int) -> List[int]: ...


Sanitizer = Callable[[str], Tuple[str, List[str]]]  # -> (текст, категории замаскированного)


class MemoryCore:
    def __init__(self, db_path: Path, index: SemanticIndex, sanitize: Sanitizer,
                 backup_dir: Optional[Path] = None, keep_backups: int = 7) -> None:
        self.db_path, self.index, self.sanitize = Path(db_path), index, sanitize
        self.backup_dir, self.keep_backups = backup_dir, keep_backups
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as c:
            c.executescript(_SCHEMA)
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                c.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(f"DB schema {row[0]} newer than code {SCHEMA_VERSION}")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")   # коммит переживает падение ОС
        c.execute("PRAGMA foreign_keys=ON")
        return c

    @contextmanager
    def _tx(self):
        """Транзакция: commit при успехе, rollback при ошибке, close всегда."""
        c = self._conn()
        try:
            with c:
                yield c
        finally:
            c.close()

    # --- идентичность: каркас-независимый ключ ---------------------------
    def resolve(self, channel: str, external_id: str) -> str:
        with self._tx() as c:
            row = c.execute("SELECT subject_id FROM aliases WHERE channel=? AND external_id=?",
                            (channel, external_id)).fetchone()
            if row:
                return row[0]
            sid = str(uuid.uuid4())
            c.execute("INSERT INTO subjects VALUES (?, ?)", (sid, time.time()))
            c.execute("INSERT INTO aliases VALUES (?, ?, ?)", (channel, external_id, sid))
            return sid

    def link(self, subject_id: str, channel: str, external_id: str) -> None:
        """Явная привязка второго канала к той же памяти (решение оператора)."""
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO aliases VALUES (?, ?, ?)",
                      (channel, external_id, subject_id))

    # --- запись: журнал первым, индекс вторым ----------------------------
    @staticmethod
    def _append(c: sqlite3.Connection, sid: str, channel: str, role: str,
                clean: str, ts: float) -> int:
        cur = c.execute(
            "INSERT INTO events (subject_id, channel, role, content, content_hash, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, channel, role, clean, hashlib.sha256(clean.encode()).hexdigest(), ts))
        c.execute("INSERT INTO index_queue (event_id) VALUES (?)", (cur.lastrowid,))
        return cur.lastrowid

    def record(self, env: Envelope, role: str, text: Optional[str] = None) -> Tuple[int, List[str]]:
        sid = self.resolve(env.channel, env.external_id)
        clean, redacted = self.sanitize(env.text if text is None else text)
        with self._tx() as c:
            eid = self._append(c, sid, env.channel, role, clean, time.time())
        self.drain_index(limit=1)
        return eid, redacted

    def record_exchange(self, env: Envelope, user_text: str, assistant_text: str) -> List[str]:
        """Пара реплик одной транзакцией: либо обе в журнале, либо ни одной."""
        sid = self.resolve(env.channel, env.external_id)
        u, red_u = self.sanitize(user_text)
        a, red_a = self.sanitize(assistant_text)
        now = time.time()
        with self._tx() as c:
            self._append(c, sid, env.channel, "user", u, now)
            self._append(c, sid, env.channel, "assistant", a, now)
        self.drain_index(limit=2)
        return red_u + red_a

    def import_legacy(self, channel: str, external_id: str,
                      rows: Iterable[Tuple[str, str, float]]) -> int:
        """Однократный перенос старой истории (role, content, ts). Без индексации:
        после импорта вызвать rebuild_index(), когда индекс будет готов."""
        sid = self.resolve(channel, external_id)
        n = 0
        with self._tx() as c:
            for role, content, ts in rows:
                clean, _ = self.sanitize(content)
                self._append(c, sid, channel, role, clean, ts)
                n += 1
        return n

    def meta_get(self, key: str) -> Optional[str]:
        with self._tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    # --- эпоха (M7.3): граница истории диалога, журнал не трогается -------
    EPOCH_KEY_PREFIX = "epoch_start:"

    def epoch_start(self, subject_id: str) -> Optional[float]:
        """ts начала текущей эпохи субъекта или None (эпох не было). Битое,
        нечисловое или будущее значение -> None: граница не должна молча
        скрыть всю историю."""
        raw = self.meta_get(self.EPOCH_KEY_PREFIX + subject_id)
        if raw is None:
            return None
        try:
            ts = float(raw)
        except ValueError:
            return None
        if not math.isfinite(ts) or ts > time.time() + 60.0:
            return None
        return ts

    def new_epoch(self, subject_id: str, ts: Optional[float] = None) -> float:
        """/newepoch: история для модели начинается с ts. Журнал events, индекс,
        S и база дрейфа не трогаются. Часы те же, что у record_exchange."""
        ts = time.time() if ts is None else float(ts)
        self.meta_set(self.EPOCH_KEY_PREFIX + subject_id, repr(ts))
        return ts

    def drain_index(self, limit: int = 100) -> int:
        """Переносит очередь в индекс. Сбой индекса не трогает журнал."""
        done = 0
        with self._tx() as c:
            rows = c.execute(
                "SELECT q.event_id, e.subject_id, e.content FROM index_queue q "
                "JOIN events e USING(event_id) ORDER BY q.event_id LIMIT ?", (limit,)).fetchall()
            for eid, sid, content in rows:
                try:
                    self.index.upsert(eid, sid, content)
                    c.execute("DELETE FROM index_queue WHERE event_id=?", (eid,))
                    done += 1
                except Exception as e:  # индекс производный: фиксируем и идём дальше
                    c.execute("UPDATE index_queue SET attempts=attempts+1, last_error=? "
                              "WHERE event_id=?", (f"{type(e).__name__}: {e}"[:200], eid))
        return done

    def rebuild_index(self) -> None:
        """Индекс потерян или повреждён: всё событийное поставить в очередь заново."""
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO index_queue (event_id) SELECT event_id FROM events")
        while self.drain_index(limit=500):
            pass

    # --- чтение ----------------------------------------------------------
    def history(self, subject_id: str, limit: int = 20,
                since_ts: Optional[float] = None) -> List[Tuple[str, str]]:
        """Последние limit событий (порядок по event_id). since_ts (M7.3) —
        только события с ts >= since_ts (граница эпохи)."""
        with self._tx() as c:
            if since_ts is None:
                rows = c.execute("SELECT role, content FROM events WHERE subject_id=? "
                                 "ORDER BY event_id DESC LIMIT ?", (subject_id, limit)).fetchall()
            else:
                rows = c.execute("SELECT role, content FROM events WHERE subject_id=? "
                                 "AND ts >= ? ORDER BY event_id DESC LIMIT ?",
                                 (subject_id, float(since_ts), limit)).fetchall()
        return list(reversed(rows))

    def recall(self, subject_id: str, query: str, k: int = 5) -> List[str]:
        try:
            ids = self.index.search(subject_id, query, k)
        except Exception:
            return []  # индекс недоступен: работаем на истории, память цела
        if not ids:
            return []
        with self._tx() as c:
            marks = ",".join("?" * len(ids))
            rows = dict(c.execute(f"SELECT event_id, content FROM events WHERE subject_id=? "
                                  f"AND event_id IN ({marks})", (subject_id, *ids)).fetchall())
        return [rows[i] for i in ids if i in rows]

    def session_stats(self, subject_id: str, pause_s: float,
                      since_ts: Optional[float] = None,
                      pause_within_epoch: bool = False) -> dict:
        """Метрики текущей сессии субъекта. ТОЛЬКО чтение, без состояния в памяти.
        turns — число ответов; span_s — от первого до последнего; active_s — сумма
        интервалов между ответами, НЕ превышающих pause_s (длинные паузы вырезаны).

        Границы сессии: без since_ts — назад от последнего ответа, пока интервал
        между соседними ответами <= pause_s (пауза > pause_s обрывает сессию). С
        since_ts (M7.3, граница эпохи) — все ответы с ts >= since_ts; тогда active_s
        < span_s, если внутри эпохи были паузы > pause_s. pause_within_epoch=True —
        граница = более поздняя из (эпоха, пауза > pause_s): решение 2 ТЗ рубежа.
        Пустая история (или нет ответов после эпохи) -> нули."""
        with self._tx() as c:
            if since_ts is None:
                rows = c.execute(
                    "SELECT ts FROM events WHERE subject_id=? AND role='assistant' "
                    "ORDER BY ts DESC LIMIT 500", (subject_id,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT ts FROM events WHERE subject_id=? AND role='assistant' "
                    "AND ts >= ? ORDER BY ts DESC LIMIT 500",
                    (subject_id, float(since_ts))).fetchall()
        ts = [r[0] for r in rows]  # по убыванию: новые -> старые
        if not ts:
            return {"turns": 0, "active_s": 0.0, "span_s": 0.0}
        cut_by_pause = since_ts is None or pause_within_epoch
        session = [ts[0]]
        for t in ts[1:]:
            if cut_by_pause and session[-1] - t > pause_s:
                break
            session.append(t)
        session.reverse()  # старые -> новые
        turns = len(session)
        if turns < 2:
            return {"turns": turns, "active_s": 0.0, "span_s": 0.0}
        span_s = round(session[-1] - session[0], 1)
        active_s = round(sum(b - a for a, b in zip(session, session[1:])
                             if (b - a) <= pause_s), 1)
        return {"turns": turns, "active_s": active_s, "span_s": span_s}

    def inventory(self, subject_id: str) -> dict:
        """Детерминированный ответ на «что ты обо мне хранишь». Без LLM."""
        with self._tx() as c:
            aliases = c.execute("SELECT channel, external_id FROM aliases WHERE subject_id=?",
                                (subject_id,)).fetchall()
            n, first, last = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM events "
                                       "WHERE subject_id=?", (subject_id,)).fetchone()
            pending = c.execute("SELECT COUNT(*) FROM index_queue q JOIN events e "
                                "USING(event_id) WHERE e.subject_id=?", (subject_id,)).fetchone()[0]
        return {"aliases": aliases, "events": n, "first_ts": first, "last_ts": last,
                "vectors": n - pending, "pending_index": pending, "derived_labels": 0}

    # --- явное удаление и защита от случайной потери ---------------------
    def forget(self, subject_id: str, purge_snapshots: bool = True) -> int:
        """Только по команде владельца. Удаляет всё о subject и стирает физически:
        строки после DELETE живут в -wal (до checkpoint) и в свободных страницах
        (до VACUUM); снапшоты - полные копии журнала, поэтому пересоздаются."""
        with self._tx() as c:
            n = c.execute("SELECT COUNT(*) FROM events WHERE subject_id=?",
                          (subject_id,)).fetchone()[0]
        self.index.delete_subject(subject_id)
        with self._tx() as c:
            c.execute("DELETE FROM subjects WHERE subject_id=?", (subject_id,))
            c.execute("DELETE FROM meta WHERE key=?", (self.EPOCH_KEY_PREFIX + subject_id,))
        self._physical_wipe()
        if purge_snapshots:
            self._reset_snapshots()
        return n

    def apply_ttl(self, days: float) -> int:
        """Удаляет события старше days. Порог считается в Python как число:
        явно и без опоры на правила приведения типов SQLite."""
        cutoff = time.time() - days * 86400
        with self._tx() as c:
            n = c.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
        if n:
            self._physical_wipe()
            self._reset_snapshots()
        return n

    def _physical_wipe(self) -> None:
        c = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.execute("VACUUM")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()

    def _reset_snapshots(self) -> None:
        """Старые снапшоты содержат удалённое -> удалить и снять один чистый."""
        if not self.backup_dir or not self.backup_dir.exists():
            return
        for old in self.backup_dir.glob("memory_*.db"):
            old.unlink()
        self.snapshot()

    def snapshot(self) -> Optional[Path]:
        """Согласованная копия через backup API (безопасно при работающем боте)."""
        if not self.backup_dir:
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dst = self.backup_dir / f"memory_{time.strftime('%Y%m%d_%H%M%S')}.db"
        src, out = self._conn(), sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close(); src.close()
        for old in sorted(self.backup_dir.glob("memory_*.db"))[:-self.keep_backups]:
            old.unlink()
        return dst
