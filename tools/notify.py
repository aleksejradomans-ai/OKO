"""Короткое уведомление владельцу через Telegram Bot API (только метод sendMessage, только id владельца).

  python tools/notify.py --status GREEN|RED|ASK|MANUAL (--text TEXT | --text-file FILE) [--env FILE] [--timeout S]

Токен и адресат -- из файла .env движка (по умолчанию <корень>/.env), как у бота: TELEGRAM_TOKEN и ПЕРВЫЙ id из
ADMIN_USER_IDS. Переменные окружения не читаются. Токен и id никогда не печатаются: весь вывод проходит маскировку.
Текст: «[STATUS] текст», не длиннее 400 символов; пути профиля пользователя и полные 40-значные хэши сокращаются.
Сеть: один POST https://api.telegram.org/bot<токен>/sendMessage, таймаут не больше 10 с, один повтор только при
сетевой ошибке (HTTP-ответ с кодом не повторяется). Без новых зависимостей (только стандартная библиотека).
Выключатель: OKO_NOTIFY_OFF=1 -> ничего не отправляется и сокет не открывается (пробный прогон, тесты).

Вывод -- одна строка:
  NOTIFY sent=yes message_id=<N>
  NOTIFY sent=no reason=<код>: <пояснение без секретов>
Код возврата: 0 -- Telegram ответил ok:true и вернул message_id; 1 -- не отправлено (любая причина); 2 -- ошибка
аргументов."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.telegram.org"
METHOD = "sendMessage"
MAX_TEXT = 400
MAX_TIMEOUT = 10.0
STATUSES = ("GREEN", "RED", "ASK", "MANUAL")
ROOT = Path(__file__).resolve().parent.parent
# похожее на токен бота (цифры:ключ) -- маскируется в любом выводе, даже если это не наш токен
_TOKEN_SHAPE = re.compile(r"\d{5,12}:[A-Za-z0-9_-]{20,}")
_HEX40 = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{7})[0-9a-fA-F]{33}(?![0-9a-fA-F])")
_WIN_PROFILE = re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\r\n\"'<>]+")
_NIX_PROFILE = re.compile(r"(?<![\w.~])/(?:home|Users)/[^/\s\"'<>]+")


class NotifyError(Exception):
    """Причина неотправки: code -- машинный код (a-z_), text -- пояснение (маскируется перед выводом)."""

    def __init__(self, code: str, text: str = ""):
        super().__init__(text)
        self.code = code
        self.text = text


def mask(s: str, secrets=()) -> str:
    """Убирает из строки известные секреты (токен, id) и всё похожее на токен бота."""
    t = str(s)
    for v in sorted({str(x) for x in secrets if x}, key=len, reverse=True):
        t = t.replace(v, "<secret>")
    return _TOKEN_SHAPE.sub("<token>", t)


def build_text(status: str, body: str, limit: int = MAX_TEXT) -> str:
    """«[STATUS] body»: пути профиля -> <user>, 40-значные хэши -> первые 7 знаков, пробелы схлопнуты, длина <= limit."""
    t = _WIN_PROFILE.sub("<user>", str(body))
    t = _NIX_PROFILE.sub("<user>", t)
    t = _HEX40.sub(r"\1", t)
    t = _TOKEN_SHAPE.sub("<token>", t)
    t = re.sub(r"[ \t]+", " ", t).strip()
    t = f"[{status}] {t}".strip()
    if len(t) > limit:
        t = t[: limit - 3].rstrip() + "..."
    return t


def read_env(path: Path) -> dict:
    """Минимальный разбор .env: KEY=VALUE, комментарии #, необязательный export, кавычки ' или "."""
    res = {}
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        res[k] = v
    return res


def credentials(env_path: Path) -> tuple[str, int]:
    """(токен, первый id владельца) из файла .env; нет файла / значения -> NotifyError."""
    if not env_path.is_file():
        raise NotifyError("no_env", "файл .env не найден")
    try:
        env = read_env(env_path)
    except (OSError, UnicodeDecodeError) as e:
        raise NotifyError("no_env", f"файл .env не читается ({type(e).__name__})")
    token = env.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise NotifyError("no_token", "TELEGRAM_TOKEN пуст")
    first = env.get("ADMIN_USER_IDS", "").split(",")[0].strip()
    if not re.fullmatch(r"-?\d{1,20}", first):
        raise NotifyError("no_chat", "первый ADMIN_USER_IDS не число")
    return token, int(first)


def _post(url: str, data: bytes, timeout: float, opener):
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with opener(req, timeout=timeout) as resp:
        return resp.read()


def send(token: str, chat_id: int, text: str, timeout: float = MAX_TIMEOUT, opener=None) -> int:
    """POST sendMessage; возвращает message_id. Один повтор только при сетевой ошибке. Ошибка -> NotifyError."""
    timeout = max(1.0, min(float(timeout), MAX_TIMEOUT))
    opener = opener or urllib.request.urlopen
    url = f"{API}/bot{token}/{METHOD}"
    data = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                      ensure_ascii=False).encode("utf-8")
    raw = None
    for attempt in (1, 2):
        try:
            raw = _post(url, data, timeout, opener)
            break
        except urllib.error.HTTPError as e:            # ответ сервера с кодом: не повторяется
            desc = ""
            try:
                desc = str(json.loads(e.read().decode("utf-8", "replace")).get("description", ""))[:120]
            except Exception:  # noqa: BLE001 -- тело ответа необязательно
                desc = ""
            raise NotifyError(f"http_{e.code}", desc or "ответ сервера с ошибкой")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt == 2:
                why = getattr(e, "reason", None) or e
                code = "timeout" if isinstance(why, TimeoutError) or "timed out" in str(why) else "network"
                raise NotifyError(code, f"нет сети или нет ответа после повтора ({type(why).__name__})")
    try:
        ans = json.loads(raw.decode("utf-8"))
    except (ValueError, AttributeError, UnicodeDecodeError):
        raise NotifyError("bad_answer", "ответ не JSON")
    if not isinstance(ans, dict) or ans.get("ok") is not True:
        desc = str(ans.get("description", "")) if isinstance(ans, dict) else ""
        raise NotifyError("not_ok", desc[:120] or "ok:false")
    mid = (ans.get("result") or {}).get("message_id") if isinstance(ans.get("result"), dict) else None
    if not isinstance(mid, int) or isinstance(mid, bool):
        raise NotifyError("bad_answer", "нет message_id")
    return mid


def notify(status: str, body: str, env_path: Path, timeout: float = MAX_TIMEOUT, opener=None) -> tuple[int, str]:
    """(код возврата, строка вывода). Никогда не бросает; строка уже замаскирована."""
    secrets: list = []
    try:
        if os.environ.get("OKO_NOTIFY_OFF", "").strip() == "1":
            raise NotifyError("off", "OKO_NOTIFY_OFF=1, отправка выключена (сокет не открывался)")
        token, chat = credentials(env_path)
        secrets = [token, str(chat)]
        mid = send(token, chat, build_text(status, body), timeout, opener)
        return 0, f"NOTIFY sent=yes message_id={mid}"
    except NotifyError as e:
        return 1, mask(f"NOTIFY sent=no reason={e.code}: {e.text}", secrets)
    except Exception as e:  # noqa: BLE001 -- уведомление никогда не роняет вызывающего
        return 1, mask(f"NOTIFY sent=no reason=error: {type(e).__name__}", secrets)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="notify")
    ap.add_argument("--status", required=True, choices=STATUSES)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--text-file")
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--timeout", type=float, default=MAX_TIMEOUT)
    a = ap.parse_args(argv)
    if a.text_file is not None:
        try:
            body = Path(a.text_file).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            rc, line = 1, f"NOTIFY sent=no reason=no_text: файл текста не читается ({type(e).__name__})"
            sys.stdout.buffer.write((line + "\n").encode("utf-8"))
            return rc
    else:
        body = a.text
    rc, line = notify(a.status, body, Path(a.env), a.timeout)
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
