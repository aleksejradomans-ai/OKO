"""shell_client.py — логика окна Oko Shell без GUI (S2, 10.11.2026). ТЗ: claude/TZ_oko_shell_2026-11-10.md.

Окно (src/oko_shell.py) ничего не решает само: текст -> канал desktop (src/shell_channel.py, 127.0.0.1) ->
те же обработчики, что у Telegram. Здесь — только транспорт к каналу и разбор ответа для показа:
  - ask(): одна JSON-строка туда и обратно; сбой связи -> честный текст («Око не отвечает»), не догадка;
  - needs_confirm(): ответ ждёт «/tv да» (tv_bot_handler, 90 с) -> окно рисует карточку Да/Нет;
  - chips()/tacho_line(): тахограф из HudSnapshot (hud_state), цвет только у связи.
Ключ из .env не печатается и в журнал не пишется.
"""
from __future__ import annotations

import json
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_PORT = 47321
NAME_PREFIX = "[Око]: "
CONFIRM_RE = re.compile(r"«/tv да»")
CONFIRM_TTL_S = 90


def read_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


@dataclass
class Conn:
    key: str
    port: int = DEFAULT_PORT
    host: str = "127.0.0.1"     # не настраивается: канал слушает только localhost


def load_conn(repo_root: Path) -> Optional[Conn]:
    env = read_env(repo_root / ".env")
    key = env.get("OKO_SHELL_KEY", "")
    if not key:
        return None
    try:
        port = int(env.get("OKO_SHELL_PORT", "") or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    return Conn(key=key, port=port)


@dataclass
class Answer:
    ok: bool
    replies: List[str] = field(default_factory=list)
    error: str = ""          # для человека, без кодов вендора
    elapsed_s: float = 0.0


ERR_TEXT = {
    "offline": "Око не отвечает: служба остановлена (игра?) или ещё запускается.",
    "auth": "Ключ окна не подошёл — нужен ключ из .env (OKO_SHELL_KEY).",
    "too_long": "Слишком длинное сообщение для окна.",
    "bad_request": "Окно отправило неверный запрос.",
    "internal": "Сбой внутри Ока, подробности в журнале.",
    "no_key": "В .env нет OKO_SHELL_KEY — канал окна не настроен.",
    "timeout": "Ответа нет дольше предела ожидания — запрос мог выполниться, проверь результат.",
}


def ask(conn: Optional[Conn], text: str, timeout: float = 180.0) -> Answer:
    t0 = time.monotonic()
    if conn is None:
        return Answer(False, error=ERR_TEXT["no_key"])
    try:
        s = socket.create_connection((conn.host, conn.port), timeout=3.0)
    except OSError:
        return Answer(False, error=ERR_TEXT["offline"], elapsed_s=time.monotonic() - t0)
    buf = b""
    try:
        with s:
            s.settimeout(timeout)
            s.sendall((json.dumps({"key": conn.key, "text": text}, ensure_ascii=False) + "\n").encode("utf-8"))
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except socket.timeout:
        return Answer(False, error=ERR_TEXT["timeout"], elapsed_s=time.monotonic() - t0)
    except OSError:
        return Answer(False, error=ERR_TEXT["offline"], elapsed_s=time.monotonic() - t0)
    try:
        res = json.loads(buf.decode("utf-8"))
    except Exception:
        return Answer(False, error=ERR_TEXT["bad_request"], elapsed_s=time.monotonic() - t0)
    if not res.get("ok"):
        return Answer(False, error=ERR_TEXT.get(res.get("error", ""), "Сбой канала окна."),
                      elapsed_s=time.monotonic() - t0)
    return Answer(True, [str(r) for r in res.get("replies", [])], elapsed_s=time.monotonic() - t0)


def clean(reply: str) -> str:
    """Подпись «[Око]: » нужна чату Telegram; в окне Око и так подписано."""
    r = reply.strip()
    return r[len(NAME_PREFIX):] if r.startswith(NAME_PREFIX) else r


def needs_confirm(reply: str) -> bool:
    return bool(CONFIRM_RE.search(reply or ""))


def confirm_text(reply: str) -> str:
    """Вопрос для карточки: первая строка ответа без инструкции «Отправьте /tv да…»."""
    first = clean(reply).split("\n", 1)[0]
    first = re.split(r"\s*Отправьте\s", first, maxsplit=1)[0].strip()
    return first or "Подтвердить команду?"


# --- тахограф ------------------------------------------------------------------------
def _hm(ts: Optional[float]) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "—"


def chips(snap) -> List[Tuple[str, str]]:
    """[(текст, тон)]; тон ok | warn | alert | muted. Только транспорт: служба, связь с моделью."""
    out: List[Tuple[str, str]] = []
    svc = getattr(snap, "service", None)
    if svc == "RUNNING":
        out.append(("Око работает", "ok"))
    elif svc == "STOPPED":
        out.append(("Око спит (игра?)", "muted"))
    elif svc:
        out.append((f"служба: {svc.lower()}", "warn"))
    else:
        out.append(("служба: нет данных", "muted"))
    up = getattr(snap, "ollama_up", None)
    gate = getattr(snap, "gate", "CLOSED")
    if up is None:
        out.append(("мозг: проверка", "muted"))
    elif not up:
        out.append(("мозг не отвечает", "alert"))
    elif gate == "OPEN":
        out.append(("мозг: связь закрыта", "alert"))
    elif gate == "HALF_OPEN":
        out.append(("мозг: проба связи", "warn"))
    else:
        models = getattr(snap, "models", None) or []
        out.append(("мозг готов" if models else "мозг: модель выгружена", "ok" if models else "muted"))
    return out


def model_line(snap) -> str:
    models = getattr(snap, "models", None) or []
    return " · ".join(n for n, _ in models) if models else "модель не в памяти"


def tacho_line(snap, busy: str = "") -> str:
    last = getattr(snap, "last_msg_ts", None)
    doing = busy or "—"
    return f"выполняется: {doing} · последнее сообщение: {_hm(last)}"
