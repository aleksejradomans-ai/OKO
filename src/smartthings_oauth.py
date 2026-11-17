"""smartthings_oauth.py — OAuth SmartThings вместо PAT на 24 часа (29.10.2026).

Проверено живьём 29.10 10:46: приложение OAuth-In (smartthings apps:create, права r:devices:* x:devices:*)
выдаёт код авторизации. Здесь — обмен кода на токены и их обновление.
B15 (17.11.2026): адрес возврата — настройка OKO_ST_REDIRECT_URI, по умолчанию http://127.0.0.1:48143/callback
(раньше — внешняя эхо-страница: код авторизации проходил через чужой сервер). Код принимает локальный одноразовый слушатель
(только 127.0.0.1; закрывается после первого запроса или по таймауту; проверка state против CSRF). Его запускает
только команда authorize, не служба. Обновление refresh-токеном адрес возврата не использует.
Факты из документации SmartThings (token management): access-токен ~24 ч (expires_in); КАЖДОЕ обновление
выдаёт новый refresh-токен, а использованный становится недействительным; неиспользуемый refresh живёт ~29–30 дней.
Поэтому: обновление заранее (за REFRESH_MARGIN_S до истечения), новый refresh-токен записывается атомарно
ДО того, как access-токен отдаётся наружу; сбой записи = ошибка, а не тихая потеря цепочки.
Данные клиента (Client Id/Secret) и токены — в одном файле .smartthings_tokens.json: data/secrets/ (закрытая папка),
пока файл не перенесён — data/ (оба вне git; выбор пути — secrets_paths.secret_path).
Секреты никогда не печатаются: только сроки, права и отпечатки.

CLI:
  python smartthings_oauth.py authorize [--timeout 300] [--show-url]   # браузер -> 127.0.0.1 -> обмен -> файл токенов
         (ST_CLIENT_ID / ST_CLIENT_SECRET из окружения, иначе из сохранённого файла токенов)
  python smartthings_oauth.py exchange   # ручной код: ST_CLIENT_ID, ST_CLIENT_SECRET, ST_CODE — из окружения
  python smartthings_oauth.py redirect   # строка адреса возврата для кабинета разработчика
  python smartthings_oauth.py status
  python smartthings_oauth.py refresh
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

TOKEN_URLS = ("https://api.smartthings.com/oauth/token", "https://api.smartthings.com/v1/oauth/token")
AUTHORIZE_URL = "https://api.smartthings.com/oauth/authorize"
SCOPE = "r:devices:* x:devices:*"
LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = 48143            # фиксированный, вне динамического диапазона Windows (49152+)
REDIRECT_URI = f"http://{LOOPBACK_HOST}:{LOOPBACK_PORT}/callback"     # по умолчанию; настройка — redirect_uri()
LISTEN_TIMEOUT_S = 300.0
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
    from secrets_paths import secret_path
    return secret_path(".smartthings_tokens.json")


def redirect_uri() -> str:
    """Адрес возврата: OKO_ST_REDIRECT_URI или http://127.0.0.1:48143/callback. Должен совпадать с кабинетом."""
    return (os.getenv("OKO_ST_REDIRECT_URI") or "").strip() or REDIRECT_URI


def loopback_parts(uri: str) -> tuple[str, int, str]:
    """(host, port, path) адреса возврата; слушатель — только http://127.0.0.1:<порт>/<путь>."""
    u = urllib.parse.urlparse(uri)
    if u.scheme != "http" or u.hostname != LOOPBACK_HOST or u.port is None:
        raise OAuthError(f"адрес возврата не локальный ({u.scheme}://{u.hostname}): слушатель только на "
                         f"http://{LOOPBACK_HOST}:<порт>/…")
    return LOOPBACK_HOST, int(u.port), u.path or "/"


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
             post: Callable = _http_post, now: float | None = None, redirect: str | None = None) -> dict:
    resp = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect or redirect_uri()},
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


_PAGE = ("<!doctype html><meta charset='utf-8'><title>Oko</title><p>{msg}</p>"
         "<p>Это окно можно закрыть.</p>")


class LoopbackListener:
    """Одноразовый HTTP-слушатель кода авторизации: только 127.0.0.1, один запрос, затем сокет закрыт.

    port=0 — свободный порт (тесты); фактический адрес — .redirect. wait() возвращает код или бросает OAuthError
    (таймаут / чужой путь / state не совпал / отказ пользователя); сокет закрывается в любом исходе."""

    def __init__(self, port: int = LOOPBACK_PORT, path: str = "/callback"):
        self.path = path
        self.result: dict = {}
        owner = self

        class _H(http.server.BaseHTTPRequestHandler):
            timeout = 30                             # принятое соединение без запроса не держит слушатель вечно

            def do_GET(self):  # noqa: N802
                u = urllib.parse.urlparse(self.path)
                owner.result = {"path": u.path, "query": urllib.parse.parse_qs(u.query)}
                ok = u.path == owner.path and "code" in owner.result["query"]
                body = _PAGE.format(msg="Код получен, Око продолжает." if ok else "Запрос не принят.").encode("utf-8")
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # тишина: в запросе код авторизации
                pass

        class _S(http.server.HTTPServer):
            allow_reuse_address = False

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):     # Windows: чужой процесс не займёт тот же порт
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                socketserver.TCPServer.server_bind(self)      # без getfqdn (обратный DNS не нужен)
                self.server_name, self.server_port = self.server_address[:2]

            def handle_error(self, request, client_address):  # без трассировки в консоль (там мог быть код)
                pass

        self.server = _S((LOOPBACK_HOST, port), _H)
        self.port = self.server.server_address[1]
        self.redirect = f"http://{LOOPBACK_HOST}:{self.port}{path}"
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.server.server_close()
            self.closed = True

    def wait(self, state: str, timeout: float = LISTEN_TIMEOUT_S) -> str:
        try:
            self.server.timeout = timeout
            self.server.handle_request()            # ровно один запрос или таймаут
        finally:
            self.close()
        if not self.result:
            raise OAuthError(f"нет ответа браузера за {timeout:.0f} с (слушатель закрыт)")
        q = self.result["query"]
        if self.result["path"] != self.path:
            raise OAuthError("запрос не на путь возврата (слушатель закрыт)")
        if q.get("error"):
            raise OAuthError(f"вход отклонён: {q['error'][0]}", auth=True)
        got = (q.get("state") or [""])[0]
        if not got or not secrets.compare_digest(got, state):
            raise OAuthError("state не совпал (возможна подмена запроса) — код не принят")
        code = (q.get("code") or [""])[0]
        if not code:
            raise OAuthError("в ответе нет кода")
        return code


def authorize_url(client_id: str, state: str, redirect: str, scope: str = SCOPE) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({"client_id": client_id, "response_type": "code",
                                                          "redirect_uri": redirect, "scope": scope, "state": state})


def authorize(client_id: str, client_secret: str, open_url: Callable[[str], object], path: Path | None = None,
              post: Callable = _http_post, timeout: float = LISTEN_TIMEOUT_S, redirect: str | None = None,
              now: float | None = None) -> dict:
    """Полный вход: слушатель 127.0.0.1 -> open_url(адрес входа) -> код -> обмен -> атомарная запись токенов."""
    _, port, rpath = loopback_parts(redirect or redirect_uri())
    lst = LoopbackListener(port, rpath)
    try:
        state = secrets.token_urlsafe(24)
        open_url(authorize_url(client_id, state, lst.redirect))
        code = lst.wait(state, timeout)
    finally:
        lst.close()
    return exchange(code, client_id, client_secret, path, post, now, redirect=lst.redirect)


def port_free(port: int = LOOPBACK_PORT) -> bool:
    """Порт слушателя свободен на 127.0.0.1 (проверка привязкой и немедленным закрытием)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LOOPBACK_HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


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
        elif cmd == "authorize":
            import webbrowser
            saved = load() or {}
            cid = os.getenv("ST_CLIENT_ID", "").strip() or saved.get("client_id") or ""
            sec = os.getenv("ST_CLIENT_SECRET", "").strip() or saved.get("client_secret") or ""
            if not (cid and sec):
                print("СТОП: нужны ST_CLIENT_ID и ST_CLIENT_SECRET (в окружении или в сохранённом файле токенов)")
                return 2
            timeout = float(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else LISTEN_TIMEOUT_S
            show = "--show-url" in sys.argv

            def _open(url: str) -> None:
                print(f"[вход] адрес возврата {redirect_uri()}; жду браузер до {timeout:.0f} с (client {fingerprint(cid)})")
                if show:
                    print(url)
                if not webbrowser.open(url):
                    print("[вход] браузер не открылся: запустите с --show-url и откройте адрес вручную")

            print("[вход] OK: " + _status(authorize(cid, sec, _open, timeout=timeout)))
        elif cmd == "redirect":
            print(redirect_uri())
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
