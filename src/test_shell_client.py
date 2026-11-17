"""SHELL_S2 (10.11): логика окна Oko Shell без GUI + проводка окна (только через shell_client)."""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import shell_client as sc

SRC = Path(__file__).resolve().parent


# --- .env и подключение -----------------------------------------------------------------
def test_load_conn_reads_key_and_port(tmp_path):
    (tmp_path / ".env").write_text("# c\nOKO_SHELL_KEY=abc  # comment\nOKO_SHELL_PORT=50001\n", encoding="utf-8")
    c = sc.load_conn(tmp_path)
    assert c.key == "abc" and c.port == 50001 and c.host == "127.0.0.1"


def test_load_conn_none_without_key(tmp_path):
    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    assert sc.load_conn(tmp_path) is None
    assert sc.load_conn(tmp_path / "missing") is None


def test_load_conn_bad_port_default(tmp_path):
    (tmp_path / ".env").write_text("OKO_SHELL_KEY=k\nOKO_SHELL_PORT=xx\n", encoding="utf-8")
    assert sc.load_conn(tmp_path).port == sc.DEFAULT_PORT


# --- ask: честные ошибки ------------------------------------------------------------------
def _server(reply: bytes):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen = {}

    def run():
        c, _ = srv.accept()
        seen["req"] = c.makefile("rb").readline()
        c.sendall(reply)
        c.close()
        srv.close()
    threading.Thread(target=run, daemon=True).start()
    return port, seen


def test_ask_ok_and_request_shape():
    port, seen = _server(b'{"ok": true, "replies": ["[\xd0\x9e\xd0\xba\xd0\xbe]: hi"]}\n')
    ans = sc.ask(sc.Conn("key1", port), "привет", timeout=5)
    assert ans.ok and ans.replies == ["[Око]: hi"]
    assert json.loads(seen["req"].decode("utf-8")) == {"key": "key1", "text": "привет"}


def test_ask_auth_error_human_text():
    port, _ = _server(b'{"ok": false, "error": "auth"}\n')
    ans = sc.ask(sc.Conn("bad", port), "x", timeout=5)
    assert not ans.ok and "Ключ" in ans.error


def test_ask_offline():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()   # порт свободен -> отказ соединения
    ans = sc.ask(sc.Conn("k", port), "x", timeout=2)
    assert not ans.ok and ans.error == sc.ERR_TEXT["offline"]


def test_ask_no_conn():
    assert sc.ask(None, "x").error == sc.ERR_TEXT["no_key"]


# --- разбор ответа ------------------------------------------------------------------------
def test_clean_prefix():
    assert sc.clean("[Око]: На связи. Что нужно?") == "На связи. Что нужно?"
    assert sc.clean("Подсветка: синяя") == "Подсветка: синяя"


def test_confirm_detect_and_question():
    r = ("[Око]: Выключить телевизор (режим ожидания)? Отправьте «/tv да» в течение 90 секунд, "
         "любой другой текст отменит.")
    assert sc.needs_confirm(r)
    assert sc.confirm_text(r) == "Выключить телевизор (режим ожидания)?"
    assert not sc.needs_confirm("Подсветка включена.")


# --- тахограф -----------------------------------------------------------------------------
def _snap(**kw):
    base = dict(service="RUNNING", ollama_up=True, gate="CLOSED", models=[("qwen2.5:14b", 6.1)], last_msg_ts=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_chips_running_ready():
    assert sc.chips(_snap()) == [("Око работает", "ok"), ("мозг готов", "ok")]


def test_chips_game_and_down():
    assert sc.chips(_snap(service="STOPPED"))[0] == ("Око спит (игра?)", "muted")
    assert sc.chips(_snap(ollama_up=False))[1] == ("мозг не отвечает", "alert")
    assert sc.chips(_snap(gate="OPEN"))[1][1] == "alert"
    assert sc.chips(_snap(models=[]))[1] == ("мозг: модель выгружена", "muted")


def test_model_line_and_tacho():
    assert sc.model_line(_snap()) == "qwen2.5:14b"
    assert "выполняется: —" in sc.tacho_line(_snap())
    assert "запрос" in sc.tacho_line(_snap(), "запрос к Оку")


# --- окно: без своего мозга/маршрута, только через shell_client ------------------------------------
def test_window_talks_only_via_client():
    src = (SRC / "oko_shell.py").read_text(encoding="utf-8")
    for bad in ("import router", "import bot", "governor", "process_logic", "requests", "httpx", "OKO_SHELL_KEY"):
        assert bad not in src
    assert "sc.ask(" in src and '"/tv да"' in src


def test_window_smoke_offscreen(monkeypatch):
    """Окно собирается и отрабатывает ответ/подтверждение/полоску без экрана (если PyQt6 есть)."""
    pytest.importorskip("PyQt6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    import oko_shell
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(Path(__import__("tempfile").mkdtemp()) / "s.ini"), QSettings.Format.IniFormat)
    w = oko_shell.Shell(settings, None)
    w.show_snapshot(_snap())
    w._on_answer(sc.Answer(True, ["[Око]: Выключить телевизор? Отправьте «/tv да» в течение 90 секунд"], elapsed_s=0.2), "x")
    assert not w.card.isHidden()
    sent = []
    monkeypatch.setattr(w, "_send", lambda t, shown=None: sent.append((t, shown)))
    w._answer_confirm(False)
    assert sent == [("/tv нет", "нет")] and w.card.isHidden()
    w._on_answer(sc.Answer(False, error="Око не отвечает"), "x")
    w.set_compact(True)
    assert w.pill.isVisibleTo(w) and not w.panel.isVisibleTo(w)
    w.set_compact(False)
    w.close()
