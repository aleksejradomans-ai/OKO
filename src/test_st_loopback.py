"""B15 (17.11.2026): SmartThings OAuth через локальный 127.0.0.1 — одноразовый слушатель, state (CSRF), закрытие
сокета, настройка адреса возврата, refresh без redirect, запись токенов в закрытую папку (Windows). Сервер SmartThings
и браузер подменены; сеть наружу не используется."""
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

import smartthings_oauth as O
from test_secrets_paths import BROAD, _me, _sids, acl_problems
from test_smartthings_oauth import FakeServer


def _browser(url_holder, code="good", state=None, path=None, extra=None, delay=0.05):
    """Имитация браузера: из адреса входа берёт redirect_uri и state и делает один GET на 127.0.0.1."""
    def run():
        time.sleep(delay)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url_holder[0]).query)
        r = urllib.parse.urlparse(q["redirect_uri"][0])
        params = {"state": state if state is not None else q["state"][0]}
        if code is not None:
            params["code"] = code
        params.update(extra or {})
        u = f"http://{r.hostname}:{r.port}{path or r.path}?" + urllib.parse.urlencode(params)
        try:
            urllib.request.urlopen(u, timeout=5).read()
        except urllib.error.HTTPError:
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _refused(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def test_default_redirect_is_loopback_and_env_overrides(monkeypatch):
    monkeypatch.delenv("OKO_ST_REDIRECT_URI", raising=False)
    assert O.redirect_uri() == "http://127.0.0.1:48143/callback" == O.REDIRECT_URI
    assert O.loopback_parts(O.redirect_uri())[0] == "127.0.0.1"
    monkeypatch.setenv("OKO_ST_REDIRECT_URI", "http://127.0.0.1:48200/cb")
    assert O.redirect_uri() == "http://127.0.0.1:48200/cb"
    assert O.loopback_parts(O.redirect_uri()) == ("127.0.0.1", 48200, "/cb")


@pytest.mark.parametrize("uri", ["https://example.org/get", "http://localhost:48143/callback",
                                 "http://0.0.0.0:48143/callback", "https://127.0.0.1:48143/callback",
                                 "http://127.0.0.1/callback"])
def test_listener_refuses_non_loopback_redirect(uri):
    with pytest.raises(O.OAuthError):
        O.loopback_parts(uri)


def test_authorize_exchange_via_loopback(tmp_path):
    p = tmp_path / ".smartthings_tokens.json"
    holder, seen = [None], {}

    def open_url(u):
        holder[0] = u
        seen["q"] = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        _browser(holder)

    lst_port = _free_port()
    srv = FakeServer(redirect=f"http://127.0.0.1:{lst_port}/callback")
    d = O.authorize("cid", "sec", open_url, path=p, post=srv, timeout=10,
                    redirect=f"http://127.0.0.1:{lst_port}/callback", now=1000.0)
    assert d["access_token"] == "A1" and json.loads(p.read_text(encoding="utf-8"))["refresh_token"] == "R1"
    q = seen["q"]
    assert q["response_type"] == ["code"] and q["client_id"] == ["cid"] and q["scope"] == [O.SCOPE]
    assert len(q["state"][0]) >= 24
    form = srv.calls[0][1]
    assert form["grant_type"] == "authorization_code" and form["redirect_uri"] == f"http://127.0.0.1:{lst_port}/callback"
    assert _refused(lst_port)                                   # сокет закрыт, порт не висит


def test_state_mismatch_rejected_nothing_saved_socket_closed(tmp_path):
    p = tmp_path / "t.json"
    port = _free_port()
    holder = [None]

    def open_url(u):
        holder[0] = u
        _browser(holder, state="forged-state")

    srv = FakeServer()
    with pytest.raises(O.OAuthError) as ei:
        O.authorize("cid", "sec", open_url, path=p, post=srv, timeout=10, redirect=f"http://127.0.0.1:{port}/callback")
    assert "state" in str(ei.value) and not p.exists() and not srv.calls and _refused(port)


def test_user_denied_is_auth_error(tmp_path):
    port = _free_port()
    holder = [None]

    def open_url(u):
        holder[0] = u
        _browser(holder, code=None, extra={"error": "access_denied"})

    with pytest.raises(O.OAuthError) as ei:
        O.authorize("cid", "sec", open_url, path=tmp_path / "t.json", post=FakeServer(), timeout=10,
                    redirect=f"http://127.0.0.1:{port}/callback")
    assert ei.value.auth and _refused(port)


def test_wrong_path_closes_after_first_request(tmp_path):
    port = _free_port()
    holder = [None]

    def open_url(u):
        holder[0] = u
        _browser(holder, path="/favicon.ico")

    with pytest.raises(O.OAuthError):
        O.authorize("cid", "sec", open_url, path=tmp_path / "t.json", post=FakeServer(), timeout=10,
                    redirect=f"http://127.0.0.1:{port}/callback")
    assert _refused(port)


def test_timeout_closes_listener():
    lst = O.LoopbackListener(port=0)
    port = lst.port
    assert lst.server.server_address[0] == "127.0.0.1"
    t0 = time.time()
    with pytest.raises(O.OAuthError) as ei:
        lst.wait("s", timeout=0.3)
    assert time.time() - t0 < 5 and "нет ответа" in str(ei.value)
    assert lst.closed and _refused(port)


def test_port_free_detects_busy_port():
    lst = O.LoopbackListener(port=0)
    try:
        assert not O.port_free(lst.port)
    finally:
        lst.close()
    assert O.port_free(lst.port)


def test_refresh_does_not_use_redirect(tmp_path, monkeypatch):
    p = tmp_path / "t.json"
    srv = FakeServer()
    O.exchange("good", "cid", "sec", path=p, post=srv, now=0.0)
    monkeypatch.setenv("OKO_ST_REDIRECT_URI", "http://127.0.0.1:48999/other")   # смена адреса не ломает цепочку
    assert O.get_token(p, post=srv, now=86399 - 100) == "A2"
    assert "redirect_uri" not in srv.calls[-1][1] and srv.calls[-1][1]["grant_type"] == "refresh_token"


def test_cli_redirect_prints_setting(monkeypatch, capsys):
    monkeypatch.delenv("OKO_ST_REDIRECT_URI", raising=False)
    monkeypatch.setattr("sys.argv", ["x", "redirect"])
    assert O.main() == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:48143/callback"


def test_cli_authorize_without_client_stops(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OKO_ST_TOKENS", str(tmp_path / "none.json"))
    monkeypatch.delenv("ST_CLIENT_ID", raising=False)
    monkeypatch.delenv("ST_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("sys.argv", ["x", "authorize"])
    assert O.main() == 2 and "СТОП" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="ACL Windows: закрытая папка секретов проверяется только на Windows")
def test_authorize_saves_into_closed_dir_without_broad_groups(tmp_path):
    d = tmp_path / "secrets"
    d.mkdir()
    me = _me()
    r = subprocess.run(["icacls", str(d), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
                        f"*{me}:(OI)(CI)F"], capture_output=True, timeout=60)
    assert r.returncode == 0
    subprocess.run(["icacls", str(d), "/remove:g"] + [f"*{x}" for x in sorted(BROAD)], capture_output=True, timeout=60)
    p = d / ".smartthings_tokens.json"
    port = _free_port()
    holder = [None]

    def open_url(u):
        holder[0] = u
        _browser(holder)

    O.authorize("cid", "sec", open_url, path=p, post=FakeServer(redirect=f"http://127.0.0.1:{port}/callback"),
                timeout=10, redirect=f"http://127.0.0.1:{port}/callback")
    assert acl_problems(_sids(d), _sids(p), me) == []          # SID файла -- из ACL папки, без широких групп


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
