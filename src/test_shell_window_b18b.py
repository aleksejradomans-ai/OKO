"""B18b (18.11, решение Оператора 18.11 13:39): окно -- тумблер БОЕВОЙ / ТОЛК, маркер, 🎙 (автостоп 30 с),
запись %TEMP%\\oko_voice\\<uuid>.wav -> путь боту -> файл не переживает запрос; замер STT tools/stt_live.py.
Микрофон и модель -- подделки: тест не слушает звук и не грузит faster-whisper."""
from __future__ import annotations

import ast
import io
import json
import math
import socket
import struct
import sys
import threading
import types
import wave
from pathlib import Path

import pytest

import sandbox_talk as talk
import shell_client as sc
import shell_voice as sv

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _tone(secs: float, rate: int = sc.REC_RATE) -> bytes:
    n = int(secs * rate)
    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n))


class FakeStream:
    """Вместо sounddevice.RawInputStream: при start() отдаёт заранее заданные байты в callback."""
    def __init__(self, rate, cb, chunks=()):
        self.rate, self.cb, self.chunks = rate, cb, list(chunks)
        self.started = self.stopped = self.closed = False

    def start(self):
        self.started = True
        for c in self.chunks:
            self.cb(c, len(c) // 2, None, None)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def _factory(chunks, made=None):
    def f(rate, cb):
        st = FakeStream(rate, cb, chunks)
        if made is not None:
            made.append(st)
        return st
    return f


@pytest.fixture
def tmpdir_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path / sc.VOICE_DIR_NAME


# --- канал: поле audio ---------------------------------------------------------------------
def _server(reply: bytes):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port, seen = srv.getsockname()[1], {}

    def run():
        c, _ = srv.accept()
        seen["req"] = c.makefile("rb").readline()
        c.sendall(reply)
        c.close()
        srv.close()
    threading.Thread(target=run, daemon=True).start()
    return port, seen


def test_ask_carries_audio_path_only_when_given():
    port, seen = _server(b'{"ok": true, "replies": []}\n')
    assert sc.ask(sc.Conn("k", port), "", timeout=5, audio="C:\\t\\oko_voice\\x.wav").ok
    assert json.loads(seen["req"].decode("utf-8")) == {"key": "k", "text": "", "audio": "C:\\t\\oko_voice\\x.wav"}
    port, seen = _server(b'{"ok": true, "replies": []}\n')
    sc.ask(sc.Conn("k", port), "hi", timeout=5)
    assert "audio" not in json.loads(seen["req"].decode("utf-8"))


# --- режим ------------------------------------------------------------------------------------
def test_mode_follows_bot_answers():
    m = sc.ModeView(clock=Clock())
    assert m.current() == sc.COMBAT and m.toggle_command() == "/talk"
    assert m.update("/talk", ["[Око]: " + talk.T_ON]) == sc.TALK and m.toggle_command() == "/lock"
    assert m.update("/status", ["Статус: всё штатно"]) == sc.TALK                    # ответ команды режим не меняет
    assert m.update("", ["🎙 в записи нет речи"]) == sc.TALK                          # отказ голоса -- тоже
    assert m.update("мысль", [f"{talk.MARKER}\nответ"]) == sc.TALK
    assert m.update("/lock", [talk.T_OFF]) == sc.COMBAT
    m.update("/talk", [talk.T_ON])
    assert m.update("подсветка красная", ["Подсветка: красная"]) == sc.COMBAT      # бот рестартовал: ответ без маркера
    m.update("/talk", [talk.T_ON])
    assert m.update("x", [talk.T_EXPIRED]) == sc.COMBAT


def test_mode_silence_reset_matches_bot():
    c = Clock()
    m = sc.ModeView(clock=c)
    m.update("/talk", [talk.T_ON])
    c.t += talk.SILENCE_S - 61
    assert m.current() == sc.TALK and m.left_s() == 61
    c.t += 61
    assert m.current() == sc.COMBAT and m.left_s() == 0 and m.toggle_command() == "/talk"
    assert talk.T_OFF.startswith(sc._OFF_PREFIX) and talk.T_EXPIRED.startswith(sc._OFF_PREFIX)
    assert sc.MODE_TEXT == {sc.COMBAT: talk.BTN_LOCK, sc.TALK: talk.BTN_TALK}


# --- запись -----------------------------------------------------------------------------------
def test_voice_path_is_what_the_bot_accepts(tmpdir_voice):
    assert sc.VOICE_DIR_NAME == sv.DIR_NAME
    assert int(sc.MAX_REC_S * sc.REC_RATE) * 2 + 44 < sv.MAX_BYTES
    rec = sc.Recorder(stream_factory=_factory([_tone(1.0)]))
    rec.start()
    path, secs = rec.stop()
    assert path.parent == tmpdir_voice and sv.NAME_RE.match(path.name) and abs(secs - 1.0) < 0.01
    assert sv.check_path(str(path)) == path
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, sc.REC_RATE, sc.REC_RATE)


def test_recorder_autostop_caps_at_30s(tmpdir_voice):
    c, made = Clock(), []
    rec = sc.Recorder(stream_factory=_factory([b"\x01\x00" * sc.REC_RATE * 31], made), clock=c)
    rec.start()
    assert rec.active and rec.due()                         # буфер полон раньше часов
    path, secs = rec.stop()
    assert secs == sc.MAX_REC_S and path.stat().st_size == int(sc.MAX_REC_S * sc.REC_RATE) * 2 + 44
    assert made[0].stopped and made[0].closed and not rec.active
    rec2 = sc.Recorder(stream_factory=_factory([_tone(0.5)]), clock=c)
    rec2.start()
    assert not rec2.due()
    c.t += sc.MAX_REC_S
    assert rec2.due()                                       # по часам
    rec2.cancel()


def test_short_or_cancelled_recording_leaves_no_file(tmpdir_voice):
    rec = sc.Recorder(stream_factory=_factory([_tone(0.1)]))
    rec.start()
    assert rec.stop() == (None, pytest.approx(0.1))
    rec.start()
    rec.cancel()
    assert not rec.active
    assert not tmpdir_voice.exists() or not list(tmpdir_voice.iterdir())


def test_discard_is_idempotent(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    sc.discard(str(p))
    sc.discard(str(p))
    sc.discard("")
    assert not p.exists()


def test_no_text_mode_file_writes_in_window_code():
    """Стык build -> pc (run_b18a 18.11): только двоичная запись файлов в коде окна и замера."""
    for f in (SRC / "shell_client.py", SRC / "oko_shell.py", ROOT / "tools" / "stt_live.py"):
        tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                name = getattr(n.func, "attr", getattr(n.func, "id", ""))
                assert name != "write_text", f"{f.name}:{n.lineno}"
                if name == "open" and not (isinstance(n.func, ast.Attribute) and getattr(n.func.value, "id", "") == "wave"):
                    for a in n.args[1:2]:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str) and any(ch in a.value for ch in "wax"):
                            assert "b" in a.value, f"{f.name}:{n.lineno} open({a.value!r})"


def test_window_has_no_combat_imports():
    src = (SRC / "oko_shell.py").read_text(encoding="utf-8")
    for bad in ("import router", "import bot", "import stt", "faster_whisper", "governor", "OKO_SHELL_KEY"):
        assert bad not in src
    assert "sc.ask(" in src and "sc.discard(" in src


# --- окно (без экрана) ------------------------------------------------------------------------
def test_window_toggle_marker_and_mic_offscreen(monkeypatch, tmpdir_voice):
    pytest.importorskip("PyQt6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    import oko_shell
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmpdir_voice.parent / "s.ini"), QSettings.Format.IniFormat)
    w = oko_shell.Shell(settings, sc.Conn("k", 1))
    sent = []
    monkeypatch.setattr(w, "_send", lambda t, shown=None, audio="": sent.append((t, shown, audio)))
    assert w.b_mode.text() == talk.BTN_LOCK and w.talk_bar.isHidden() and w.mic.isEnabled()
    w._toggle_mode()
    assert sent[-1][0] == "/talk"
    w._on_answer(sc.Answer(True, [talk.T_ON], elapsed_s=0.1), "/talk")
    assert w.b_mode.text() == talk.BTN_TALK and not w.talk_bar.isHidden() and talk.MARKER in w.talk_bar.text()
    w._toggle_mode()
    assert sent[-1][0] == "/lock"
    w._on_answer(sc.Answer(True, [talk.T_OFF], elapsed_s=0.1), "/lock")
    assert w.b_mode.text() == talk.BTN_LOCK and w.talk_bar.isHidden()
    # 🎙: старт -> стоп -> путь уходит в _send с audio, текст пустой
    w.rec = sc.Recorder(stream_factory=_factory([_tone(1.0)]))
    w._mic_clicked()
    assert w.rec.active and w.mic.text().startswith("■")
    w._mic_clicked()
    t, shown, audio = sent[-1]
    assert t == "" and shown.startswith("🎙 запись 1.0") and sv.check_path(audio) and w.mic.text() == "🎙"
    # AskJob: бот недоступен -> файл всё равно удалён окном
    job = oko_shell.AskJob(None, "", audio)
    job.run()
    assert not Path(audio).exists()
    # микрофон не открылся -> честный текст, окно живо
    def boom(rate, cb):
        raise OSError("no device")
    w.rec = sc.Recorder(stream_factory=boom)
    w._mic_clicked()
    assert not w.rec.active
    w.close()


def test_window_without_channel_disables_mic(monkeypatch, tmp_path):
    pytest.importorskip("PyQt6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    import oko_shell
    app = QApplication.instance() or QApplication([])
    w = oko_shell.Shell(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat), None)
    assert not w.mic.isEnabled()
    w.close()


# --- замер STT на живой фразе (шаг Оператора) ---------------------------------------------------
def test_stt_live_uses_bot_path_and_deletes_file(monkeypatch, tmpdir_voice, capsys):
    calls = []

    class SttUnavailable(RuntimeError):
        pass
    fake = types.SimpleNamespace(SttUnavailable=SttUnavailable, load=lambda: (object(), "cpu/int8"),
                                 transcribe=lambda p: (calls.append(p), "вниз три")[1])
    monkeypatch.setitem(sys.modules, "stt", fake)
    monkeypatch.setattr(sc, "_sd_stream", _factory([_tone(1.0)]))
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import importlib
        live = importlib.import_module("stt_live")
        rc = live.main(["--seconds", "5"])
    finally:
        sys.path.remove(str(ROOT / "tools"))
    out = capsys.readouterr().out
    assert rc == 0 and len(calls) == 1 and not Path(calls[0]).exists()
    assert "device=cpu/int8" in out and "result=ok chars=8 words=2 file_left=False" in out and "TEXT вниз три" in out
    assert [ln for ln in out.splitlines() if "вниз" in ln] == ["TEXT вниз три"]     # текст -- только в строке TEXT
