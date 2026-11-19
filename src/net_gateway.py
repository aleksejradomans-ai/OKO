"""net_gateway.py -- единственная дверь в интернет для нового кода. ЗАКРЫТ ПО УМОЛЧАНИЮ.

Открыт только если одновременно: переменная окружения OKO_NET_OPEN=1 (её ставит блок запуска на один процесс)
И нет файла-рубильника <data>/net/KILL (проверяется перед каждым запросом). Иначе -- отказ до любого сокета.
Правила (config/net_allowlist.json): только GET; только https с проверкой сертификата и имени хоста; хост и
префикс пути -- из белого списка; без прокси из окружения (http.client их не читает), без cookies, без заголовков
авторизации; честный User-Agent; robots.txt хоста соблюдается (читается первым запросом прогона);
редиректы -- вручную, каждый шаг сверяется со списком, не больше max_redirects; лимиты: не чаще одного запроса
в min_interval_s (ожидание), не больше per_day в сутки (отказ), ответ не больше max_bytes (обрыв на потоке),
таймауты соединения / чтения; тип ответа -- только из content_types.
Журнал <data>/net/journal.jsonl: время, метод, адрес, код, байты, sha256, решение, причина; тела ответа нет.
Отказ (decision=deny) помечается incident=true. Только стандартная библиотека.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import json
import os
import ssl
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

OPEN_ENV = "OKO_NET_OPEN"
ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / "config" / "net_allowlist.json"
REDIRECTS = (301, 302, 303, 307, 308)


class Denied(Exception):
    """Шлюз отказал (до соединения или по ответу). reason -- короткий код для журнала."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    content_type: str
    body: bytes
    sha256: str
    fetched_at: str


def default_data_dir() -> Path:
    try:
        import config
        return Path(config.BASE_DIR)
    except Exception:  # noqa: BLE001 -- вне службы (ручной запуск, тесты)
        return ROOT / "data"


def load_allowlist(path: Path = ALLOWLIST) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _https_connection(host: str, timeout: float):
    ctx = ssl.create_default_context()           # CERT_REQUIRED + check_hostname
    return http.client.HTTPSConnection(host, 443, timeout=timeout, context=ctx)


class Gateway:
    def __init__(self, data_dir: Optional[Path] = None, allowlist: Optional[dict] = None,
                 connect: Callable = _https_connection, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, env: Optional[dict] = None):
        self.data = Path(data_dir) if data_dir else default_data_dir()
        self.net = self.data / "net"
        self.cfg = allowlist if allowlist is not None else load_allowlist()
        self.connect, self.clock, self.sleep = connect, clock, sleep
        self.env = os.environ if env is None else env
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    # ---------------------------------------------------------------- состояние
    @property
    def kill_file(self) -> Path:
        return self.net / "KILL"

    def is_open(self) -> bool:
        return self.env.get(OPEN_ENV) == "1" and not self.kill_file.exists()

    def _journal(self, **rec) -> None:
        self.net.mkdir(parents=True, exist_ok=True)
        rec = dict({"at": dt.datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds")}, **rec)
        rec["incident"] = rec.get("decision") == "deny"
        with (self.net / "journal.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    def _rate_state(self) -> dict:
        p = self.net / "rate.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"last": 0.0, "day": "", "count": 0}

    def _rate_take(self) -> None:
        lim = self.cfg["limits"]
        st = self._rate_state()
        today = dt.date.fromtimestamp(self.clock()).isoformat()
        if st.get("day") != today:
            st = {"last": st.get("last", 0.0), "day": today, "count": 0}
        if st["count"] >= int(lim["per_day"]):
            raise Denied("per_day", f"{st['count']} запросов за сутки (предел {lim['per_day']})")
        wait = float(st.get("last", 0.0)) + float(lim["min_interval_s"]) - self.clock()
        if wait > 0:
            self.sleep(wait)
        st["last"], st["count"] = self.clock(), st["count"] + 1
        self.net.mkdir(parents=True, exist_ok=True)
        (self.net / "rate.json").write_text(json.dumps(st), encoding="utf-8")

    # ---------------------------------------------------------------- проверки
    def check_url(self, url: str) -> urllib.parse.SplitResult:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https":
            raise Denied("scheme", f"только https, получено {u.scheme or '-'}")
        if u.username or u.password:
            raise Denied("userinfo", "учётные данные в адресе запрещены")
        host = (u.hostname or "").lower()
        if u.port not in (None, 443):
            raise Denied("port", str(u.port))
        prefixes = self.cfg["hosts"].get(host)
        if prefixes is None:
            raise Denied("host", host or "-")
        path = u.path or "/"
        if ".." in path.split("/"):
            raise Denied("path", "переход «..» в пути")
        if not any(path == p or (p.endswith("/") and path.startswith(p)) for p in prefixes):
            raise Denied("path", path)
        return u

    def _robots_allows(self, u: urllib.parse.SplitResult) -> bool:
        host = u.hostname.lower()
        if u.path == "/robots.txt":
            return True
        rp = self._robots.get(host)
        if rp is None:
            self._get(f"https://{host}/robots.txt", robots_check=False)     # результат кэширует _remember_robots
            rp = self._robots[host]
        return rp.can_fetch(self.cfg["user_agent"].split("/")[0], urllib.parse.urlunsplit(u))

    def _remember_robots(self, url: str, status: int, body: bytes) -> None:
        u = urllib.parse.urlsplit(url)
        rp = urllib.robotparser.RobotFileParser()
        if status >= 500:
            rp.disallow_all = True                           # robots недоступен из-за сервера -- считать запретом
        else:
            rp.parse(body.decode("utf-8", "replace").splitlines() if status == 200 else [])
        self._robots[(u.hostname or "").lower()] = rp

    # ---------------------------------------------------------------- запрос
    def request(self, method: str, url: str) -> Response:
        """Единая точка: метод проверяется здесь (POST/PUT/DELETE/PATCH -- отказ до соединения)."""
        if method.upper() not in self.cfg["methods"]:
            self._journal(method=method.upper(), url=url, decision="deny", reason="method")
            raise Denied("method", method)
        return self.get(url)

    def get(self, url: str) -> Response:
        return self._get(url, robots_check=True)

    def _get(self, url: str, robots_check: bool) -> Response:
        hops = 0
        cur = url
        while True:
            try:
                if not self.is_open():
                    raise Denied("closed", "рубильник KILL" if self.kill_file.exists() else f"{OPEN_ENV} не задан")
                u = self.check_url(cur)
                if robots_check and not self._robots_allows(u):
                    raise Denied("robots", u.path)
                if not self.is_open():                       # рубильник мог сработать во время robots
                    raise Denied("closed", "рубильник KILL")
                self._rate_take()
                status, ctype, loc, body = self._fetch_once(u)
            except Denied as e:
                self._journal(method="GET", url=cur, decision="deny", reason=e.reason, detail=str(e)[:200])
                raise
            except (OSError, http.client.HTTPException, ssl.SSLError) as e:
                self._journal(method="GET", url=cur, decision="error", reason=type(e).__name__)
                raise
            sha = hashlib.sha256(body).hexdigest()
            if status in REDIRECTS:
                nxt = urllib.parse.urljoin(cur, loc or "")
                self._journal(method="GET", url=cur, status=status, bytes=len(body), sha256=sha, decision="redirect",
                              reason="location", to=nxt)
                hops += 1
                if hops > int(self.cfg["limits"]["max_redirects"]):
                    self._journal(method="GET", url=nxt, decision="deny", reason="redirects")
                    raise Denied("redirects", f"больше {self.cfg['limits']['max_redirects']}")
                cur = nxt
                continue
            base = ctype.split(";")[0].strip().lower()
            if status == 200 and base not in self.cfg["content_types"]:
                self._journal(method="GET", url=cur, status=status, bytes=len(body), decision="deny",
                              reason="content_type", detail=base)
                raise Denied("content_type", base)
            self._journal(method="GET", url=cur, status=status, bytes=len(body), sha256=sha, decision="allow",
                          reason="ok" if status == 200 else f"status_{status}")
            if urllib.parse.urlsplit(cur).path == "/robots.txt":
                self._remember_robots(cur, status, body)
            return Response(cur, status, ctype, body, sha,
                            dt.datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds"))

    def _fetch_once(self, u: urllib.parse.SplitResult) -> tuple[int, str, str, bytes]:
        lim = self.cfg["limits"]
        conn = self.connect(u.hostname.lower(), float(lim["connect_timeout_s"]))
        try:
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            conn.request("GET", path, headers={"User-Agent": self.cfg["user_agent"], "Accept": "text/html, text/plain",
                                               "Accept-Encoding": "identity", "Connection": "close"})
            if getattr(conn, "sock", None) is not None:
                conn.sock.settimeout(float(lim["read_timeout_s"]))
            r = conn.getresponse()
            cap = int(lim["max_bytes"])
            buf = bytearray()
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > cap:
                    raise Denied("too_large", f"больше {cap} байт")
            return r.status, r.getheader("Content-Type", "") or "", r.getheader("Location", "") or "", bytes(buf)
        finally:
            conn.close()
