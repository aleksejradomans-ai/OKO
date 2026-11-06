"""smartthings_oauth (29.10.2026): обмен кода, обновление заранее, ротация refresh-токена (использованный —
недействителен), атомарная запись, честные ошибки. Сервер SmartThings подменён."""
import base64
import json

import pytest

import smartthings_oauth as O


class FakeServer:
    """Повторяет поведение из документации: каждый refresh выдаёт новый refresh, старый становится недействителен."""

    def __init__(self, first_404=False):
        self.valid_refresh = set()
        self.n = 0
        self.calls = []
        self.first_404 = first_404

    def __call__(self, url, form, basic, timeout=20.0):
        self.calls.append((url, dict(form), basic))
        if self.first_404 and url == O.TOKEN_URLS[0]:
            return 404, {}
        if base64.b64decode(basic).decode() != "cid:sec":
            return 401, {"error": "invalid_client"}
        if form["grant_type"] == "authorization_code":
            if form["code"] != "good" or form["redirect_uri"] != O.REDIRECT_URI:
                return 400, {"error": "invalid_grant"}
        elif form["grant_type"] == "refresh_token":
            if form["refresh_token"] not in self.valid_refresh:
                return 400, {"error": "invalid_grant"}
            self.valid_refresh.discard(form["refresh_token"])
        self.n += 1
        rt = f"R{self.n}"
        self.valid_refresh.add(rt)
        return 200, {"access_token": f"A{self.n}", "refresh_token": rt, "expires_in": 86399,
                     "scope": "r:devices:* x:devices:*", "installed_app_id": "app"}


@pytest.fixture
def path(tmp_path):
    return tmp_path / ".smartthings_tokens.json"


def test_exchange_saves_tokens_and_client(path):
    srv = FakeServer()
    d = O.exchange("good", "cid", "sec", path=path, post=srv, now=1000.0)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "A1" and saved["refresh_token"] == "R1" and saved["client_secret"] == "sec"
    assert saved["expires_at"] == 1000.0 + 86399 and d["scope"] == "r:devices:* x:devices:*"
    assert srv.calls[0][1]["client_id"] == "cid" and srv.calls[0][1]["grant_type"] == "authorization_code"


def test_bad_code_is_auth_error_and_nothing_saved(path):
    with pytest.raises(O.OAuthError) as ei:
        O.exchange("expired", "cid", "sec", path=path, post=FakeServer())
    assert ei.value.auth and not path.exists()


def test_fresh_token_returned_without_refresh(path):
    srv = FakeServer()
    O.exchange("good", "cid", "sec", path=path, post=srv, now=0.0)
    assert O.get_token(path, post=srv, now=1000.0) == "A1" and len(srv.calls) == 1


def test_refresh_ahead_and_rotation_chain(path):
    srv = FakeServer()
    O.exchange("good", "cid", "sec", path=path, post=srv, now=0.0)
    t = 86399 - 1800                                     # меньше часа до истечения
    assert O.get_token(path, post=srv, now=t) == "A2"
    assert json.loads(path.read_text())["refresh_token"] == "R2"
    assert O.get_token(path, post=srv, now=t + 86399 - 100) == "A3"       # следующий день — снова по цепочке
    assert "R1" not in srv.valid_refresh and "R2" not in srv.valid_refresh


def test_used_refresh_token_is_invalid_auth_needed(path):
    srv = FakeServer()
    O.exchange("good", "cid", "sec", path=path, post=srv, now=0.0)
    old = path.read_text()
    O.refresh(path, post=srv, now=10.0)
    path.write_text(old)                                 # кто-то вернул файл со старым (использованным) refresh
    with pytest.raises(O.OAuthError) as ei:
        O.refresh(path, post=srv, now=20.0)
    assert ei.value.auth


def test_falls_back_to_second_endpoint_on_404(path):
    srv = FakeServer(first_404=True)
    O.exchange("good", "cid", "sec", path=path, post=srv)
    assert [c[0] for c in srv.calls] == list(O.TOKEN_URLS)


def test_network_error_is_not_auth(path):
    srv = FakeServer()
    O.exchange("good", "cid", "sec", path=path, post=srv, now=0.0)

    def dead(*a, **k):
        raise TimeoutError("timed out")

    with pytest.raises(O.OAuthError) as ei:
        O.get_token(path, post=dead, now=86000.0)
    assert not ei.value.auth
    assert json.loads(path.read_text())["refresh_token"] == "R1"   # цепочка не потеряна


def test_not_configured_returns_none(path):
    assert O.get_token(path, post=FakeServer()) is None


def test_status_never_prints_secrets(path, monkeypatch, capsys):
    srv = FakeServer()
    O.exchange("good", "cid", "sec", path=path, post=srv)
    monkeypatch.setenv("OKO_ST_TOKENS", str(path))
    monkeypatch.setattr("sys.argv", ["x", "status"])
    O.main()
    out = capsys.readouterr().out
    for secret in ("A1", "R1", "sec", "cid"):
        assert secret not in out.split("права")[0].replace(O.fingerprint(secret), "")
