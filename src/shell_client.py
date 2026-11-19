"""shell_client.py — логика окна Oko Shell без GUI (S2, 10.11.2026). ТЗ: claude/TZ_oko_shell_2026-11-10.md.

Окно (src/oko_shell.py) ничего не решает само: текст -> канал desktop (src/shell_channel.py, 127.0.0.1) ->
те же обработчики, что у Telegram. Здесь — только транспорт к каналу и разбор ответа для показа:
  - ask(): одна JSON-строка туда и обратно; сбой связи -> честный текст («Око не отвечает»), не догадка;
  - needs_confirm(): ответ ждёт «/tv да» (tv_bot_handler, 90 с) -> окно рисует карточку Да/Нет;
  - chips()/tacho_line(): тахограф из HudSnapshot (hud_state), цвет только у связи.
Ключ из .env не печатается и в журнал не пишется.

B18b (18.11, решение Оператора 18.11 13:39): тумблер БОЕВОЙ / ТОЛК и голос окна.
  - ModeView: режим окна по ответам бота (маркер ТОЛК / «Режим БОЕВОЙ») + своя метка 30 мин тишины; бот -- источник
    истины, окно только показывает (после рестарта бота = БОЕВОЙ, окно узнаёт это по первому ответу без маркера);
  - Recorder: запись микрофона (sounddevice, 16 кГц моно int16), автостоп MAX_REC_S, файл
    %TEMP%\\oko_voice\\<uuid>.wav пишется байтами; ask(..., audio=путь) -> бот распознаёт и удаляет файл;
    discard() -- страховка окна: файл удаляется и если бот не ответил.
"""
from __future__ import annotations

import json
import math
import re
import socket
import tempfile
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import sandbox_talk as talk     # только константы режима (модуль -- стандартная библиотека, без привода)

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


def ask(conn: Optional[Conn], text: str, timeout: float = 180.0, audio: str = "") -> Answer:
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
            req = {"key": conn.key, "text": text}
            if audio:
                req["audio"] = audio
            s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
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


# --- B18b: режим БОЕВОЙ / ТОЛК --------------------------------------------------------------
COMBAT, TALK = "combat", "talk"
MODE_TEXT = {COMBAT: talk.BTN_LOCK, TALK: talk.BTN_TALK}
PILL_REPLY_S = 30          # B18c: короткий ответ держится в плашке столько секунд (полный -- в развёрнутом чате)
PILL_REPLY_CHARS = 90


def short_reply(text: str, n: int = PILL_REPLY_CHARS) -> str:
    """Ответ для свёрнутой плашки: без подписи и маркера ТОЛК, первая непустая строка, не длиннее n."""
    t = clean(text or "")
    if t.startswith(talk.MARKER):
        t = t[len(talk.MARKER):]
    line = next((ln.strip() for ln in t.splitlines() if ln.strip()), "")
    return line if len(line) <= n else line[: n - 1].rstrip() + "…"


def rec_clock(elapsed_s: float, max_s: Optional[float] = None) -> str:
    """Секундомер записи (B18c, Оператор 15:38): «● 0:07 / 0:30» -- прошло (вниз до целых секунд) / предел."""
    m = int(MAX_REC_S if max_s is None else max_s)
    e = max(0, min(int(elapsed_s), m))
    return f"● {e // 60}:{e % 60:02d} / {m // 60}:{m % 60:02d}"


def talk_line(view: "ModeView") -> str:
    """Строка ТОЛК для плашки и полосы над полем: маркер + минуты до автосброса ('' в БОЕВОЙ)."""
    if view.current() != TALK:
        return ""
    return f"{talk.MARKER} · автосброс через {view.left_minutes()} мин"
_OFF_PREFIX = "🔒 Режим БОЕВОЙ"


class ModeView:
    """Что показывает окно. Бот -- источник истины: ответ с маркером = ТОЛК, «🔒 Режим БОЕВОЙ…» = БОЕВОЙ,
    ответ на обычный текст без маркера = БОЕВОЙ (в ТОЛК бот метит каждый ответ). Ответы на команды (/status и т.п.)
    режим не меняют. Тишина SILENCE_S с последнего хода ТОЛК -> БОЕВОЙ (бот сбрасывает так же)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, silence_s: float = talk.SILENCE_S):
        self.clock, self.silence_s = clock, silence_s
        self.mode, self.last = COMBAT, 0.0

    def current(self) -> str:
        if self.mode == TALK and self.clock() - self.last >= self.silence_s:
            self.mode = COMBAT
        return self.mode

    def update(self, sent: str, replies: List[str]) -> str:
        texts = [clean(r) for r in replies]
        is_cmd = (sent or "").strip().startswith("/")
        if any(t.startswith(_OFF_PREFIX) for t in texts):
            self.mode = COMBAT
        elif any(t.startswith(talk.MARKER) for t in texts):
            self.mode, self.last = TALK, self.clock()
        elif not is_cmd and any(not t.startswith("🎙") for t in texts):
            self.mode = COMBAT
        return self.current()

    def toggle_command(self) -> str:
        return "/lock" if self.current() == TALK else "/talk"

    def left_minutes(self) -> int:
        """Минуты до автосброса для показа -- вверх (ceil): сразу после /talk «30», «29» -- когда осталось <= 29:00.
        Одно правило для шапки и плашки. 18.11 run_b18c: было left_s // 60 -- на Windows monotonic шагает ~15,6 мс,
        и сразу после включения выходило то 30 (прошло 0 мс), то 29 (прошло > 0)."""
        if self.current() != TALK:
            return 0
        left = self.silence_s - (self.clock() - self.last)
        return max(0, math.ceil(left / 60.0))

    def left_s(self) -> int:
        return max(0, int(self.silence_s - (self.clock() - self.last))) if self.current() == TALK else 0


# --- B18b: голос окна -----------------------------------------------------------------------
VOICE_DIR_NAME = "oko_voice"        # = shell_voice.DIR_NAME (сверяет тест)
MAX_REC_S = 30.0
REC_RATE = 16000
MIN_REC_S = 0.4


def voice_dir() -> Path:
    return Path(tempfile.gettempdir()) / VOICE_DIR_NAME


def new_voice_path() -> Path:
    d = voice_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uuid.uuid4()}.wav"


def write_wav(path: Path, pcm: bytes, rate: int = REC_RATE) -> Path:
    """16 бит моно; файл открывается только в двоичном режиме (стык Windows: текстовый режим меняет \\n)."""
    with open(path, "wb") as f:
        with wave.open(f, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
    return path


def discard(path) -> None:
    """Страховка окна: запись не должна пережить запрос (бот удаляет сам; здесь -- если бот не ответил)."""
    if not path:
        return
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _sd_stream(rate: int, callback):
    import sounddevice as sd     # пакет env oko_bot (READ probe_b18 17.11); импорт только при записи
    return sd.RawInputStream(samplerate=rate, channels=1, dtype="int16", callback=callback)


class Recorder:
    """Старт -> байты с микрофона в память -> стоп (вручную или автостоп MAX_REC_S) -> WAV во временной папке."""

    def __init__(self, stream_factory: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic,
                 max_s: float = MAX_REC_S, rate: int = REC_RATE):
        self.factory, self.clock, self.max_s, self.rate = stream_factory, clock, max_s, rate
        self.stream, self.t0, self._buf, self._lock = None, 0.0, bytearray(), threading.Lock()
        self._limit = int(max_s * rate) * 2

    @property
    def active(self) -> bool:
        return self.stream is not None

    def _cb(self, data, frames, t, status):
        with self._lock:
            room = self._limit - len(self._buf)
            if room > 0:
                self._buf += bytes(data)[:room]

    def start(self) -> None:
        if self.active:
            return
        self._buf = bytearray()
        st = (self.factory or _sd_stream)(self.rate, self._cb)
        st.start()
        self.stream, self.t0 = st, self.clock()

    def elapsed(self) -> float:
        return self.clock() - self.t0 if self.active else 0.0

    def due(self) -> bool:
        """Пора ли автостоп (окно спрашивает раз в 200 мс)."""
        return self.active and (self.elapsed() >= self.max_s or len(self._buf) >= self._limit)

    def cancel(self) -> None:
        """Выход окна во время записи: поток закрыт, байты выброшены, файл не создаётся."""
        st, self.stream = self.stream, None
        if st is not None:
            try:
                st.stop()
            finally:
                st.close()
        with self._lock:
            self._buf = bytearray()

    def stop(self) -> Tuple[Optional[Path], float]:
        """-> (путь WAV | None если записи нет / слишком коротко, секунды звука)."""
        st, self.stream = self.stream, None
        if st is not None:
            try:
                st.stop()
            finally:
                st.close()
        with self._lock:
            pcm, self._buf = bytes(self._buf), bytearray()
        secs = len(pcm) / 2 / self.rate
        if secs < MIN_REC_S:
            return None, secs
        return write_wav(new_voice_path(), pcm, self.rate), secs
