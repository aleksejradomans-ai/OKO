"""smartthings_oauth.py — OAuth SmartThings вместо PAT на 24 часа (29.10.2026).

Проверено живьём 29.10 10:46: приложение OAuth-In (smartthings apps:create, права r:devices:* x:devices:*,
redirect https://httpbin.org/get) выдаёт код авторизации. Здесь — обмен кода на токены и их обновление.
Факты из документации SmartThings (token management): access-токен ~24 ч (expires_in); КАЖДОЕ обновление
выдаёт новый refresh-токен, а использованный становится недействительным; неиспользуемый refresh живёт ~29–30 дней.
Поэтому: обновление заранее (за REFRESH_MARGIN_S до истечения), новый refresh-токен записывается атомарно
ДО того, как access-токен отдаётся наружу; сбой записи = ошибка, а не тихая потеря цепочки.
Данные клиента (Client Id/Secret) и токены — в одном файле data/.smartthings_tokens.json (data/ вне git).
Секреты никогда не печатаются: только сроки, права и отпечатки.

CLI:
  python smartthings_oauth.py exchange   # ST_CLIENT_ID, ST_CLIENT_SECRET, ST_CODE — из окружения (Read-Host)
  python smartthings_oauth.py status
  python smartthings_oauth.py refresh
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

TOKEN_URLS = ("https://api.smartthings.com/oauth/token", "https://api.smartthings.com/v1/oauth/token")
REDIRECT_URI = "https://httpbin.org/get"
REFRESH_MARGIN_S = 3600          # обновлять, когда до истечения меньше часа
_lock = threading.Lock()


class OAuthError(RuntimeError):
    """Обмен/обновление не удались. auth=True — нужен повторный вход (refresh недействителен)."""

    def __init__(self, msg: str, auth: bool = False):
        super().__init__(msg)
        self.auth = auth


def token_path() -> Path:
    env = os.getenv("OKO_ST_TOKENS")
    if env:
        return Path(env)
    try:
        import config
        return Path(config.BASE_DIR) / ".smartthings_tokens.json"
    except Exception:
        return Path(__file__).resolve().parent.parent / "data" / ".smartthings_tokens.json"


def fingerprint(s: str | None) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()[:8].upper() if s else "—"


def _http_post(url: str, form: dict, basic: str, timeout: float = 20.0) -> tuple[int, dict]:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "Authorization": "Basic " + basic})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        return e.code, body


def _token_request(form: dict, client_id: str, client_secret: str, post: Callable = _http_post) -> dict:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    form = dict(form, client_id=client_id)
    last = None
    for url in TOKEN_URLS:
        try:
            code, body = post(url, form, basic)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OAuthError(f"сеть: {type(e).__name__}: {e}") from e
        if code == 404:
            last = f"{url}: 404"
            continue
        if code == 200 and body.get("access_token"):
            return body
        err = body.get("error") or body.get("error_description") or ""
        raise OAuthError(f"HTTP {code} {err}".strip(), auth=code in (400, 401) or err == "invalid_grant")
    raise OAuthError(f"адрес обмена не найден ({last})")


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)                      # атомарно: читатель видит старый или новый файл целиком


def load(path: Path | None = None) -> dict | None:
    p = path or token_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _store(resp: dict, client_id: str, client_secret: str, now: float) -> dict:
    return {"client_id": client_id, "client_secret": client_secret,
            "access_token": resp["access_token"], "refresh_token": resp.get("refresh_token"),
            "expires_at": now + float(resp.get("expires_in") or 0), "scope": resp.get("scope"),
            "installed_app_id": resp.get("installed_app_id"), "updated_at": now}


def exchange(code: str, client_id: str, client_secret: str, path: Path | None = None,
             post: Callable = _http_post, now: float | None = None) -> dict:
    resp = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
                          client_id, client_secret, post)
    data = _store(resp, client_id, client_secret, (time.time() if now is None else now))
    if not data["refresh_token"]:
        raise OAuthError("в ответе нет refresh_token — обновлять будет нечем")
    _save(path or token_path(), data)
    return data


def refresh(path: Path | None = None, post: Callable = _http_post, now: float | None = None) -> dict:
    p = path or token_path()
    data = load(p)
    if not data or not data.get("refresh_token"):
        raise OAuthError("нет сохранённого refresh-токена — нужен обмен кода (exchange)", auth=True)
    resp = _token_request({"grant_type": "refresh_token", "refresh_token": data["refresh_token"]},
                          data["client_id"], data["client_secret"], post)
    new = _store(resp, data["client_id"], data["client_secret"], (time.time() if now is None else now))
    if not new["refresh_token"]:
        new["refresh_token"] = data["refresh_token"]   # если сервер не ротировал — старый остаётся действующим
    _save(p, new)                                       # ДО возврата: использованный refresh уже недействителен
    return new


def get_token(path: Path | None = None, post: Callable = _http_post, now: float | None = None) -> str | None:
    """Действующий access-токен (с обновлением заранее) или None, если OAuth не настроен (тогда — PAT)."""
    p = path or token_path()
    with _lock:
        data = load(p)
        if not data:
            return None
        t = (time.time() if now is None else now)
        if float(data.get("expires_at") or 0) - t < REFRESH_MARGIN_S:
            data = refresh(p, post, t)
        return data["access_token"]


def _status(data: dict | None) -> str:
    if not data:
        return f"OAuth не настроен ({token_path()})"
    left = float(data.get("expires_at") or 0) - time.time()
    return (f"access {fingerprint(data.get('access_token'))}, ещё {left / 3600:.1f} ч; refresh "
            f"{fingerprint(data.get('refresh_token'))}; права: {data.get('scope')}; client {fingerprint(data.get('client_id'))}")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if cmd == "exchange":
            cid, sec, code = (os.getenv(k, "").strip() for k in ("ST_CLIENT_ID", "ST_CLIENT_SECRET", "ST_CODE"))
            if not (cid and sec and code):
                print("СТОП: нужны ST_CLIENT_ID, ST_CLIENT_SECRET, ST_CODE в окружении")
                return 2
            print("[обмен] OK: " + _status(exchange(code, cid, sec)))
        elif cmd == "refresh":
            print("[обновление] OK: " + _status(refresh()))
        else:
            print(_status(load()))
        return 0
    except OAuthError as e:
        print(f"[{'ВХОД НУЖЕН' if e.auth else 'ОШИБКА'}] {e}")
        return 3 if e.auth else 1


if __name__ == "__main__":
    sys.exit(main())
