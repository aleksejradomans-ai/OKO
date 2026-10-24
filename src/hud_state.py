"""hud_state.py — логика HUD-оверлея без GUI (TZ_hud_overlay_2026-10-20).

Только чтение: хвост лога OkoBot (общий разбор с sleep_digest), data\\ops.db в режиме
read-only, проба Ollama (/api/version, /api/ps), состояние службы OkoBot (sc query).
Ничего не пишет ни в лог, ни в базы. Текст сообщений не показывается.

Правила отображения (решения Оператора):
- цвет только у транспорта (TransportGate, Ollama, служба); метрики M6 и дрейф — без цвета и
  без слова «warning» до калибровки;
- служба остановлена = «остановлен (игра?)» без тревоги;
- подписи M6 — сокращения ce/ad/rl/intent, без «empathy/resistance».
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import sleep_digest as sd
from ops_db import Cursor

START_TAIL_BYTES = 64 * 1024
OLLAMA_URL = "http://127.0.0.1:11434"

_GATE_RE = re.compile(r"(?:->|→)\s*(HALF_OPEN|CLOSED|OPEN)\b")
_PAUSE_RE = re.compile(r"pause=(\d+)s")
_SC_RE = re.compile(r"\b[1-7]\s+(STOPPED|START_PENDING|STOP_PENDING|RUNNING|CONTINUE_PENDING|PAUSE_PENDING|PAUSED)\b")


# --- пути -------------------------------------------------------------------------
def read_env_file(path: Path) -> Dict[str, str]:
    """Минимальный разбор .env (KEY=VALUE, # комментарии). Без python-dotenv и без config.py."""
    out: Dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


def resolve_paths(repo_root: Path, log_arg: Optional[str] = None, ops_arg: Optional[str] = None,
                  environ: Optional[Dict[str, str]] = None) -> Tuple[Optional[Path], Path]:
    """Порядок: аргумент -> переменная окружения -> .env в корне репозитория -> по умолчанию."""
    environ = os.environ if environ is None else environ
    env = read_env_file(Path(repo_root) / ".env")
    log = log_arg or environ.get("OKO_LOG_FILE") or env.get("OKO_LOG_FILE") or ""
    if ops_arg:
        ops = Path(ops_arg)
    else:
        base = environ.get("AI_BASE_DIR") or env.get("AI_BASE_DIR") or "data"
        base_p = Path(base)
        if not base_p.is_absolute():
            base_p = Path(repo_root) / base_p
        ops = base_p / "ops.db"
    return (Path(log) if log else None), ops


# --- снимок -------------------------------------------------------------------------
@dataclass
class HudSnapshot:
    now: float
    gate: str = "CLOSED"            # CLOSED | OPEN | HALF_OPEN
    gate_from_log: bool = False     # False = в хвосте лога переходов не было
    gate_ts: Optional[float] = None
    gate_pause_s: Optional[int] = None
    metrics: Optional[dict] = None
    metrics_ts: Optional[float] = None
    last_msg_ts: Optional[float] = None
    red_last_hour: int = 0
    log_ok: bool = False
    ollama_up: Optional[bool] = None
    ollama_version: Optional[str] = None
    ollama_rtt_ms: Optional[int] = None
    models: List[Tuple[str, float]] = field(default_factory=list)   # (имя, ГБ в VRAM)
    service: Optional[str] = None   # RUNNING | STOPPED | ... | None (не Windows / нет данных)
    sleep: Optional[dict] = None
    errors: List[str] = field(default_factory=list)


class LogWatcher:
    """Инкрементальный хвост лога. При старте — последние START_TAIL_BYTES."""

    def __init__(self, path: Optional[Path], start_tail: int = START_TAIL_BYTES):
        self.path = Path(path) if path else None
        self.cursor: Optional[Cursor] = None
        self.start_tail = start_tail
        self.gate, self.gate_from_log, self.gate_ts, self.gate_pause = "CLOSED", False, None, None
        self.metrics, self.metrics_ts, self.last_msg_ts = None, None, None
        self.red_times: List[float] = []

    def _initial_cursor(self) -> Cursor:
        with open(self.path, "rb") as f:
            head = f.read(sd.HEAD_LEN)
            size = f.seek(0, os.SEEK_END)
        return Cursor(max(0, size - self.start_tail), sd._head_hash(head), len(head))

    def poll(self) -> bool:
        if not self.path or not self.path.is_file():
            return False
        if self.cursor is None:
            self.cursor = self._initial_cursor()
        rr = sd.read_new_lines(self.path, self.cursor, max_bytes=4 * 1024 * 1024)
        self.cursor = rr.cursor
        for line in rr.lines:
            self.feed(line)
        return True

    def feed(self, line: str) -> None:
        p = sd.parse_line(line)
        if p is None:
            return
        ts, level, _name, msg = p
        if level in ("ERROR", "CRITICAL"):
            self.red_times.append(ts)
        if msg.startswith("[TRANSPORT_GATE]"):
            m = _GATE_RE.findall(msg)
            state = m[-1] if m else ("OPEN" if re.search(r"\bOPEN\b", msg) else None)
            if state:
                self.gate, self.gate_from_log, self.gate_ts = state, True, ts
                pm = _PAUSE_RE.search(msg)
                self.gate_pause = int(pm.group(1)) if (pm and state == "OPEN") else None
        elif msg.startswith("[METRICS]"):
            mt = sd.parse_metrics(msg)
            if mt is not None:
                self.metrics, self.metrics_ts = mt, ts
        elif msg.startswith("Processing message from"):
            self.last_msg_ts = ts

    def red_count(self, now: float, window_s: float = 3600) -> int:
        self.red_times = [t for t in self.red_times if now - t <= window_s]
        return len(self.red_times)


# --- внешние пробы ------------------------------------------------------------------
def _http_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def probe_ollama(base: str = OLLAMA_URL, timeout: float = 1.0,
                 fetch: Callable[[str, float], dict] = _http_json) -> dict:
    t0 = time.perf_counter()
    v = fetch(base + "/api/version", timeout)
    rtt = int((time.perf_counter() - t0) * 1000)
    models = []
    try:
        for m in fetch(base + "/api/ps", timeout).get("models", []) or []:
            models.append((str(m.get("name", "?")), round(float(m.get("size_vram", 0)) / 1024 ** 3, 1)))
    except Exception:
        pass
    return {"version": v.get("version"), "rtt_ms": rtt, "models": models}


def parse_sc_query(text: str) -> Optional[str]:
    m = _SC_RE.search(text or "")
    return m.group(1) if m else None


def query_service(name: str = "OkoBot",
                  run: Optional[Callable[[List[str]], str]] = None) -> Optional[str]:
    """Состояние службы через `sc query`. Не Windows -> None. Окно консоли не появляется."""
    if run is None:
        if sys.platform != "win32":
            return None

        def run(cmd):
            cp = subprocess.run(cmd, capture_output=True, timeout=3,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return cp.stdout.decode("cp866", errors="replace")
    return parse_sc_query(run(["sc", "query", name]))


def read_sleep(ops_path: Path) -> Optional[dict]:
    """Последняя сводка сна из ops.db строго read-only. Нет файла/таблицы -> None, файл не создаётся."""
    ops_path = Path(ops_path)
    if not ops_path.is_file():
        return None
    uri = ops_path.resolve().as_uri() + "?mode=ro"
    c = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        row = c.execute("SELECT created_at, answers, drift_score, drift_status, payload "
                        "FROM sleep_digest ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()
    if not row:
        return None
    try:
        drift = json.loads(row[4]).get("drift", {})
    except (ValueError, TypeError):
        drift = {}
    return {"created_at": row[0], "answers": row[1], "drift_score": row[2],
            "drift_status": row[3], "baseline_samples": drift.get("baseline_samples")}


class HudCollector:
    """Собирает HudSnapshot. Лог — каждый тик; Ollama и служба — раз в probe_every_s;
    ops.db — раз в ops_every_s. Сбой одного источника не гасит остальные (строка в errors)."""

    def __init__(self, log_path: Optional[Path], ops_path: Path, probe_every_s: float = 5.0,
                 ops_every_s: float = 30.0, ollama=probe_ollama, service=query_service,
                 sleep_reader=read_sleep, clock=time.time):
        self.watch = LogWatcher(log_path)
        self.ops_path = ops_path
        self.probe_every_s, self.ops_every_s = probe_every_s, ops_every_s
        self._ollama, self._service, self._sleep = ollama, service, sleep_reader
        self._clock = clock
        self._next_probe = self._next_ops = 0.0
        self._ol: dict = {}
        self._ol_up: Optional[bool] = None
        self._svc: Optional[str] = None
        self._sleep_val: Optional[dict] = None
        self._errs: Dict[str, str] = {}

    def _try(self, key: str, fn):
        try:
            res = fn()
            self._errs.pop(key, None)
            return True, res
        except Exception as e:
            self._errs[key] = f"{key}: {type(e).__name__}"
            return False, None

    def tick(self) -> HudSnapshot:
        now = self._clock()
        ok_log, polled = self._try("log", self.watch.poll)
        if now >= self._next_probe:
            self._next_probe = now + self.probe_every_s
            ok, res = self._try("ollama", self._ollama)
            self._ol_up, self._ol = (True, res) if ok else (False, {})
            self._errs.pop("ollama", None)   # недоступная Ollama — состояние, а не ошибка HUD
            ok, res = self._try("service", self._service)
            self._svc = res if ok else None
        if now >= self._next_ops:
            self._next_ops = now + self.ops_every_s
            ok, res = self._try("ops.db", lambda: self._sleep(self.ops_path))
            if ok:
                self._sleep_val = res
        w = self.watch
        return HudSnapshot(
            now=now, gate=w.gate, gate_from_log=w.gate_from_log, gate_ts=w.gate_ts,
            gate_pause_s=w.gate_pause, metrics=w.metrics, metrics_ts=w.metrics_ts,
            last_msg_ts=w.last_msg_ts, red_last_hour=w.red_count(now),
            log_ok=bool(ok_log and polled), ollama_up=self._ol_up,
            ollama_version=self._ol.get("version"), ollama_rtt_ms=self._ol.get("rtt_ms"),
            models=list(self._ol.get("models", [])), service=self._svc,
            sleep=self._sleep_val, errors=sorted(self._errs.values()))


# --- отображение (чистые функции) ----------------------------------------------------
def _hm(ts: Optional[float]) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "—"


def _ago(now: float, ts: Optional[float]) -> str:
    if not ts:
        return "—"
    s = max(0, int(now - ts))
    if s < 90:
        return f"{s} с назад"
    if s < 5400:
        return f"{s // 60} мин назад"
    if s < 48 * 3600:
        return f"{s // 3600} ч назад"
    return f"{s // 86400} дн назад"


def render(s: HudSnapshot) -> List[Tuple[str, str]]:
    """-> [(текст, тон)]; тон: ok | alert | warn | muted | plain. Цвет только у транспорта."""
    rows: List[Tuple[str, str]] = []
    # 1. связь
    if s.gate == "OPEN":
        pause = f", пауза {s.gate_pause_s} с" if s.gate_pause_s else ""
        rows.append((f"Связь: OPEN — запросы к модели не идут (с {_hm(s.gate_ts)}{pause})", "alert"))
    elif s.gate == "HALF_OPEN":
        rows.append(("Связь: HALF_OPEN — проба восстановления", "warn"))
    else:
        src = f"с {_hm(s.gate_ts)}" if s.gate_from_log else "переходов в логе нет"
        rows.append((f"Связь: CLOSED ({src})", "ok"))
    # 2. Ollama
    if s.ollama_up is None:
        rows.append(("Ollama: проверка…", "muted"))
    elif not s.ollama_up:
        rows.append(("Ollama: не отвечает", "alert"))
    else:
        if s.models:
            mods = ", ".join(f"{n} {g:g} ГБ" for n, g in s.models)
            vram = f"в VRAM: {mods}"
        else:
            vram = "модель выгружена"
        rows.append((f"Ollama {s.ollama_version or '?'} · {s.ollama_rtt_ms} мс · {vram}", "ok"))
    # 3. служба
    last = f"посл. сообщение {_ago(s.now, s.last_msg_ts)}"
    if s.service == "RUNNING":
        rows.append((f"OkoBot: работает · {last}", "ok"))
    elif s.service == "STOPPED":
        rows.append((f"OkoBot: остановлен (игра?) · {last}", "muted"))
    elif s.service:
        rows.append((f"OkoBot: {s.service} · {last}", "warn"))
    else:
        rows.append((f"OkoBot: состояние неизвестно · {last}", "muted"))
    # 4. метрики M6 — без цвета
    m = s.metrics
    if m:
        rows.append((f"M6 {_hm(s.metrics_ts)}: ce {m['ce']:.2f} · ad {m['ad']:.2f} · rl {m['rl']:.2f} · "
                     f"{m.get('intent', '?')} · len {int(m['len'])}", "plain"))
    else:
        rows.append(("M6: в хвосте лога нет строк [METRICS]", "muted"))
    # 5. сон и дрейф — без цвета, без порогов
    sl = s.sleep
    if sl:
        drift = "—" if sl.get("drift_score") is None else f"{sl['drift_score']:.2f}"
        base = (f" · база {sl['baseline_samples']}/{sd.BASELINE_MIN_SAMPLES}"
                if sl.get("drift_status") == "baseline_pending" and sl.get("baseline_samples") is not None else "")
        rows.append((f"Сон {_hm(sl.get('created_at'))}: ответов {sl.get('answers')} · дрейф {drift} "
                     f"({sl.get('drift_status')}){base}", "plain"))
    else:
        rows.append(("Сон: нет данных", "muted"))
    # 6. подвал
    foot = []
    if s.red_last_hour:
        foot.append(f"красных строк за час: {s.red_last_hour}")
    if not s.log_ok:
        foot.append("лог не найден (OKO_LOG_FILE)")
    foot.extend(s.errors)
    if foot:
        rows.append((" · ".join(foot), "muted"))
    return rows
