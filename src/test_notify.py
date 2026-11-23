"""tools/notify.py: уведомление владельцу через Bot API sendMessage -- без сети (подставной HTTP), без утечки токена
и id в вывод (с тестом-лжецом на саму проверку утечки), выключатель OKO_NOTIFY_OFF без сокета, обрезка текста."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "notify.py"
# синтетические значения (не похожи на живой токен: нет «AA» после двоеточия, ключ короче живого)
TOKEN = "7000001:TESTFAKE_notify_token_x1"
OWNER = 4242424
SECOND = 5151515


def _load():
    spec = importlib.util.spec_from_file_location("oko_notify_tool", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["oko_notify_tool"] = mod
    spec.loader.exec_module(mod)
    return mod


nt = _load()


@pytest.fixture(autouse=True)
def _no_outer_off_switch(monkeypatch):
    """Внешнее окружение (пробный прогон ставит OKO_NOTIFY_OFF=1 всем дочерним процессам) не должно менять проверяемую
    логику: каждый тест начинает без выключателя; тест выключателя ставит его сам."""
    monkeypatch.delenv("OKO_NOTIFY_OFF", raising=False)


def _env(tmp_path: Path, token: str = TOKEN, ids: str = f"{OWNER}, {SECOND}") -> Path:
    p = tmp_path / ".env"
    p.write_text(f"# test\nTELEGRAM_TOKEN={token}\nADMIN_USER_IDS={ids}\nOTHER=1\n", encoding="utf-8")
    return p


class _Resp:
    def __init__(self, body: bytes):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHTTP:
    """Подставной urlopen: план ответов по порядку; каждый -- bytes (тело 200) или исключение."""

    def __init__(self, *plan):
        self.plan = list(plan)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append({"url": req.full_url, "method": req.get_method(), "timeout": timeout,
                           "body": json.loads(req.data.decode("utf-8"))})
        x = self.plan.pop(0)
        if isinstance(x, BaseException):
            raise x
        return _Resp(x)


def _ok(mid=77):
    return json.dumps({"ok": True, "result": {"message_id": mid, "chat": {"id": OWNER}}}).encode()


def _http_error(code: int, desc: str):
    return urllib.error.HTTPError(f"{nt.API}/bot{TOKEN}/{nt.METHOD}", code, "err", {},
                                  io.BytesIO(json.dumps({"ok": False, "description": desc}).encode()))


def _no_leak(line: str, secrets=(TOKEN, str(OWNER))):
    """Проверка утечки: ни токена, ни его ключевой части, ни id владельца в выводе."""
    key = TOKEN.split(":", 1)[1]
    for s in list(secrets) + [key]:
        assert s not in line, "секрет в выводе"


def test_ok_sends_one_post_to_send_message_of_first_owner(tmp_path):
    h = FakeHTTP(_ok(77))
    rc, line = nt.notify("GREEN", "run done", _env(tmp_path), opener=h)
    assert (rc, line) == (0, "NOTIFY sent=yes message_id=77")
    assert len(h.calls) == 1
    c = h.calls[0]
    assert c["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert c["method"] == "POST"
    assert c["body"]["chat_id"] == OWNER and c["body"]["chat_id"] != SECOND
    assert c["body"]["text"] == "[GREEN] run done"
    assert 0 < c["timeout"] <= 10


def test_only_send_message_on_bot_api():
    assert nt.API == "https://api.telegram.org"
    assert nt.METHOD == "sendMessage"
    src = TOOL.read_text(encoding="utf-8")
    for other in ("getUpdates", "sendDocument", "sendPhoto", "forwardMessage", "setWebhook", "getMe"):
        assert other not in src


def test_network_error_retried_once_then_not_sent(tmp_path):
    h = FakeHTTP(urllib.error.URLError(OSError("unreachable")), urllib.error.URLError(OSError("unreachable")))
    rc, line = nt.notify("RED", "x", _env(tmp_path), opener=h)
    assert rc == 1 and line.startswith("NOTIFY sent=no reason=network: ")
    assert len(h.calls) == 2


def test_network_error_then_ok_on_the_single_retry(tmp_path):
    h = FakeHTTP(ConnectionResetError("reset"), _ok(5))
    rc, line = nt.notify("ASK", "x", _env(tmp_path), opener=h)
    assert (rc, line) == (0, "NOTIFY sent=yes message_id=5")
    assert len(h.calls) == 2


def test_timeout_is_not_sent_after_one_retry(tmp_path):
    h = FakeHTTP(urllib.error.URLError(TimeoutError("timed out")), TimeoutError("timed out"))
    rc, line = nt.notify("GREEN", "x", _env(tmp_path), opener=h)
    assert rc == 1 and line.startswith("NOTIFY sent=no reason=timeout: ")
    assert len(h.calls) == 2


def test_ok_false_is_not_sent_and_not_retried(tmp_path):
    h = FakeHTTP(json.dumps({"ok": False, "description": "Bad Request: chat not found"}).encode())
    rc, line = nt.notify("GREEN", "x", _env(tmp_path), opener=h)
    assert rc == 1 and line == "NOTIFY sent=no reason=not_ok: Bad Request: chat not found"
    assert len(h.calls) == 1


def test_http_error_is_not_retried(tmp_path):
    h = FakeHTTP(_http_error(401, "Unauthorized"))
    rc, line = nt.notify("GREEN", "x", _env(tmp_path), opener=h)
    assert rc == 1 and line == "NOTIFY sent=no reason=http_401: Unauthorized"
    assert len(h.calls) == 1


@pytest.mark.parametrize("body,code", [
    (b"<html>not json</html>", "bad_answer"),
    (json.dumps({"ok": True, "result": {}}).encode(), "bad_answer"),
    (json.dumps({"ok": True, "result": {"message_id": True}}).encode(), "bad_answer"),
    (json.dumps([1, 2]).encode(), "not_ok"),
])
def test_answer_without_message_id_is_not_sent(tmp_path, body, code):
    rc, line = nt.notify("GREEN", "x", _env(tmp_path), opener=FakeHTTP(body))
    assert rc == 1 and line.startswith(f"NOTIFY sent=no reason={code}: ")


def test_unexpected_error_never_raises(tmp_path):
    rc, line = nt.notify("GREEN", "x", _env(tmp_path), opener=FakeHTTP(ValueError(f"bad url {TOKEN}")))
    assert rc == 1 and line == "NOTIFY sent=no reason=error: ValueError"


def test_timeout_capped_at_10_seconds(tmp_path):
    h = FakeHTTP(_ok())
    nt.notify("GREEN", "x", _env(tmp_path), timeout=60, opener=h)
    assert h.calls[0]["timeout"] == 10.0


@pytest.mark.parametrize("env_text,code", [
    (None, "no_env"),
    ("ADMIN_USER_IDS=1\n", "no_token"),
    ("TELEGRAM_TOKEN=\nADMIN_USER_IDS=1\n", "no_token"),
    (f"TELEGRAM_TOKEN={TOKEN}\nADMIN_USER_IDS=\n", "no_chat"),
    (f"TELEGRAM_TOKEN={TOKEN}\nADMIN_USER_IDS=owner\n", "no_chat"),
])
def test_missing_credentials_not_sent_and_no_network(tmp_path, env_text, code):
    p = tmp_path / ".env"
    if env_text is not None:
        p.write_text(env_text, encoding="utf-8")
    h = FakeHTTP()
    rc, line = nt.notify("GREEN", "x", p, opener=h)
    assert rc == 1 and line.startswith(f"NOTIFY sent=no reason={code}: ")
    assert h.calls == []
    _no_leak(line)


def test_env_parsing_quotes_export_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text(f"﻿# c\nexport TELEGRAM_TOKEN=\"{TOKEN}\"\nADMIN_USER_IDS= {OWNER} ,{SECOND} # owner first\n",
                 encoding="utf-8")
    assert nt.credentials(p) == (TOKEN, OWNER)


def test_text_truncated_to_400():
    t = nt.build_text("GREEN", "x" * 1000)
    assert len(t) == 400 and t.startswith("[GREEN] x") and t.endswith("...")
    assert nt.build_text("RED", "short") == "[RED] short"


def test_text_without_profile_paths_full_hashes_and_tokens():
    win = "C:" + "\\" + "Users" + "\\" + "Someone Name" + "\\AppData\\x.txt"
    nix = "/" + "home" + "/someone/x.txt"
    full = "0123456789abcdef0123456789abcdef01234567"
    t = nt.build_text("RED", f"fail in {win} and {nix}; commit {full}; key {TOKEN}")
    assert "Someone" not in t and "someone" not in t and "<user>" in t
    assert full not in t and "0123456" in t
    assert TOKEN not in t and "<token>" in t


def _leaky_scenarios(tmp_path):
    """Пути, на которых токен или id могли бы попасть в вывод: сервер вернул их в описании ошибки, исключение несёт адрес."""
    return [
        FakeHTTP(_ok(9)),
        FakeHTTP(json.dumps({"ok": False, "description": f"chat {OWNER} token {TOKEN}"}).encode()),
        FakeHTTP(_http_error(400, f"bad chat_id {OWNER} for bot {TOKEN}")),
        FakeHTTP(urllib.error.URLError(OSError(f"{nt.API}/bot{TOKEN}/sendMessage")),
                 urllib.error.URLError(OSError(f"{nt.API}/bot{TOKEN}/sendMessage"))),
        FakeHTTP(RuntimeError(f"{TOKEN} {OWNER}")),
    ]


def test_token_and_owner_id_never_in_output(tmp_path):
    for h in _leaky_scenarios(tmp_path):
        rc, line = nt.notify("RED", f"text with {TOKEN}", _env(tmp_path), opener=h)
        _no_leak(line)


def test_liar_leak_check_turns_red_when_masking_is_broken(tmp_path, monkeypatch):
    """Тест-лжец: маскировка выключена -> проверка утечки обязана упасть (иначе тест выше ничего не доказывает)."""
    with pytest.raises(AssertionError):
        _no_leak(f"NOTIFY sent=no reason=not_ok: token {TOKEN}")
    monkeypatch.setattr(nt, "mask", lambda s, secrets=(): str(s))
    h = FakeHTTP(json.dumps({"ok": False, "description": f"chat {OWNER} token {TOKEN}"}).encode())
    rc, line = nt.notify("RED", "x", _env(tmp_path), opener=h)
    with pytest.raises(AssertionError):
        _no_leak(line)


def test_main_prints_one_line_without_secrets(tmp_path, monkeypatch, capsysbinary):
    tf = tmp_path / "msg.txt"
    tf.write_text("run B00 tests 1, commit abc1234", encoding="utf-8")
    h = FakeHTTP(_ok(11))
    monkeypatch.setattr(urllib.request, "urlopen", h)
    rc = nt.main(["--status", "GREEN", "--text-file", str(tf), "--env", str(_env(tmp_path))])
    out = capsysbinary.readouterr().out.decode("utf-8")
    assert rc == 0 and out == "NOTIFY sent=yes message_id=11\n"
    assert h.calls[0]["body"]["text"] == "[GREEN] run B00 tests 1, commit abc1234"
    rc = nt.main(["--status", "RED", "--text-file", str(tmp_path / "none.txt"), "--env", str(_env(tmp_path))])
    out = capsysbinary.readouterr().out.decode("utf-8")
    assert rc == 1 and out.startswith("NOTIFY sent=no reason=no_text: ") and out.count("\n") == 1


def test_off_switch_opens_no_socket(tmp_path, monkeypatch, capsysbinary):
    """Пробный прогон: OKO_NOTIFY_OFF=1 -> ни urlopen, ни сокета; вывод «не отправлено», код 1."""
    def boom(*a, **k):
        raise AssertionError("сеть в пробном прогоне")

    monkeypatch.setenv("OKO_NOTIFY_OFF", "1")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    rc = nt.main(["--status", "GREEN", "--text", "x", "--env", str(_env(tmp_path))])
    out = capsysbinary.readouterr().out.decode("utf-8")
    assert rc == 1 and out.startswith("NOTIFY sent=no reason=off: ")
    rc, line = nt.notify("GREEN", "x", _env(tmp_path), opener=FakeHTTP())
    assert rc == 1 and "reason=off" in line


def test_outer_off_switch_does_not_reach_the_tests(tmp_path):
    """Даже при OKO_NOTIFY_OFF=1 снаружи (pytest запущен с ним) тест видит чистое окружение и реальную отправку."""
    assert "OKO_NOTIFY_OFF" not in os.environ
    h = FakeHTTP(_ok(21))
    rc, line = nt.notify("GREEN", "x", _env(tmp_path), opener=h)
    assert (rc, line) == (0, "NOTIFY sent=yes message_id=21") and len(h.calls) == 1


def test_bad_status_is_an_argument_error(tmp_path):
    with pytest.raises(SystemExit) as e:
        nt.main(["--status", "BLUE", "--text", "x", "--env", str(_env(tmp_path))])
    assert e.value.code == 2
