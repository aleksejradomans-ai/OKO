"""PROOF_REGISTRY (этап A, 30.10.2026): производный реестр подтверждений функций Ока.

Зачем. Каталог функций (capabilities.py) не должен выдавать объявленное в коде за
работающее: ТВ переезжает по подсетям, облака падают. Реестр хранит ПОСЛЕДНЕЕ
подтверждение по каждой функции, каталог показывает его рядом со строкой функции.

Правила (RULES_proof, OKO_MEMORY_TTL_DAYS=0):
- файл append-only, ничего не удаляется и не переписывается;
- реестр производный: каждая запись ссылается на первоисточник (ref), сырые
  журналы не трогаются и не заменяются;
- в путь LLM-вызова диск не попадает: индекс живёт в памяти, governor получает
  готовую строку каталога.

Модель чтения:
- load(): один полный проход при создании -> индекс {fn: последняя запись} и
  байтовое смещение конца последней ЦЕЛОЙ строки;
- refresh(): os.stat; размер вырос -> читается только дельта; уменьшился (файл
  подменён) -> полный повторный проход с WARNING;
- record(): дописывает одну строку и сразу читает дельту (свою строку и чужие,
  дописанные другим процессом, например selfcheck) -- смещение не «перепрыгивает»
  чужие записи;
- обрывок без перевода строки в конце (запись ещё идёт) не потребляется;
- битая целая строка пропускается с WARNING, процесс не падает.

Формат строки (UTF-8, JSON, одна запись -- одна строка):
{"ts":"2026-10-30T13:11:04+02:00","fn":"tv","status":"PASS","src":"selfcheck",
 "ref":"logs\\\\OkoBot.stderr.log","net":"10.95.175"}
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

ENV_PATH = "OKO_PROOF_REGISTRY"
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "proof_registry.jsonl"
STATUSES = frozenset({"PASS", "WARN", "FAIL"})

logger = logging.getLogger("kernel.proof_registry")


def default_path() -> Path:
    raw = os.getenv(ENV_PATH, "").strip()
    return Path(raw) if raw else DEFAULT_PATH


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _valid(rec: object) -> bool:
    return (
        isinstance(rec, dict)
        and isinstance(rec.get("fn"), str) and bool(rec["fn"])
        and rec.get("status") in STATUSES
        and isinstance(rec.get("ts"), str) and bool(rec["ts"])
    )


class ProofRegistry:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._lock = threading.Lock()
        self._index: dict[str, dict] = {}
        self._offset = 0
        self.bad_lines = 0
        self.load()

    # -- чтение -------------------------------------------------------------

    def load(self) -> None:
        """Полный проход. Время пишется в лог: если со временем станет заметным
        (файл растёт без предела), решение -- снимок индекса, по данным, не заранее."""
        t0 = time.monotonic()
        with self._lock:
            self._index = {}
            self._offset = 0
            self.bad_lines = 0
            self._read_delta_locked()
            n, size = len(self._index), self._offset
        logger.info("[PROOF] loaded fns=%d bytes=%d ms=%d", n, size,
                    int((time.monotonic() - t0) * 1000))

    def refresh(self) -> bool:
        """Догрузка записей других процессов. True -- индекс изменился."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        with self._lock:
            if size < self._offset:
                logger.warning("[PROOF] file shrank %d -> %d bytes, full reload", self._offset, size)
                self._index = {}
                self._offset = 0
                self.bad_lines = 0
            if size == self._offset:
                return False
            before = dict(self._index)
            self._read_delta_locked()
            return self._index != before

    def _read_delta_locked(self) -> None:
        try:
            with open(self.path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except FileNotFoundError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return  # только обрывок -- ждём окончания записи
        pos = self._offset
        for raw in chunk[: end + 1].split(b"\n")[:-1]:
            line_at = pos
            pos += len(raw) + 1
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                rec = None
            if not _valid(rec):
                self.bad_lines += 1
                logger.warning("[PROOF] bad line @%d skipped", line_at)
                continue
            self._index[rec["fn"]] = rec
        self._offset += end + 1

    def latest(self) -> dict[str, dict]:
        """Копия индекса {fn: последняя запись}. Без диска."""
        with self._lock:
            return {k: dict(v) for k, v in self._index.items()}

    # -- запись -------------------------------------------------------------

    def record(
        self,
        fn: str,
        status: str,
        *,
        src: str,
        ref: Optional[str] = None,
        net: Optional[str] = None,
        ts: Optional[str] = None,
    ) -> dict:
        """Дописывает подтверждение. status: PASS / WARN / FAIL. src -- кто подтвердил
        (selfcheck, tv, net_context...). ref -- первоисточник (журнал, отчёт)."""
        if not isinstance(fn, str) or not fn:
            raise ValueError("fn must be a non-empty string")
        if status not in STATUSES:
            raise ValueError(f"status must be one of {sorted(STATUSES)}, got {status!r}")
        if not isinstance(src, str) or not src:
            raise ValueError("src must be a non-empty string")
        rec = {"ts": ts or _now_iso(), "fn": fn, "status": status, "src": src}
        if ref:
            rec["ref"] = ref
        if net:
            rec["net"] = net
        data = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as f:
                f.write(data)
                f.flush()
            self._read_delta_locked()
        return rec
