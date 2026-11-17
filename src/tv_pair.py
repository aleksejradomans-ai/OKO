"""tv_pair.py — разовый пайринг с телевизором Philips (JointSpace API v6).

Запускается ОДИН раз вручную, с пультом под рукой: телевизор показывает
4-значный PIN на экране, его нужно ввести в консоль. В ответ на `grant`
телевизор выдаёт пару `username`/`password`, действующую постоянно —
дальше работает только `tv_control.py`, этот модуль больше не нужен.

Протокол (из документа `tv_philips_api_v6`, раздел «протокол и пайринг»):
  1. POST /6/pair/request  {scope, device}        -> auth_key, timestamp, timeout
  2. телевизор показывает PIN на экране
  3. POST /6/pair/grant    {auth, device}         -> 200
     signature = base64(HMAC-SHA1(SECRET, str(timestamp) + pin))
     digest-auth на этом шаге: username=device_id, password=auth_key

SECRET — публичная константа протокола JointSpace, она одинакова для всех
телевизоров Philips и давно опубликована (pylips, philipstv). Это не
секрет Оператора: без PIN с экрана она бесполезна, физический доступ к
телевизору остаётся обязательным условием. Поэтому лежит в коде, а не в
`.env` — в `.env` ей нечего делать, она не конфигурация.

ВАЖНО: `device_id` должен совпадать на шагах 1 и 3 и далее НЕ нужен —
постоянная пара приходит в ответе на `request` (поля `auth_key` и сам
`device_id` и есть будущие password/username). Телевизор привязывает
пару к этому id, поэтому id генерируется один раз и сохраняется вместе
с парой в `data\\tv_creds.json`.

Запись пары — через `tv_control.save_creds` (атомарная, 0600), не своим
кодом: иначе появились бы два места, знающих формат файла.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import string
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import urllib3

import tv_control
from tv_control import DEFAULT_PORT, TVError, TVTransportError

logger = logging.getLogger("oko.tv.pair")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Публичная константа протокола JointSpace (см. docstring выше).
SECRET_B64 = (
    "ZmVay1EQVFOaZhwQ4Kv81ypLAZNczV9sG4KkseXWn1NEk6cXmPKO/MCa9sryslvLCFMnNe4Z4CPXzToowvhHvA=="
)

DEVICE = {
    "device_name": "oko",
    "device_os": "Android",
    "app_name": "Oko System 0",
    "type": "native",
}


def new_device_id(rng: random.Random | None = None) -> str:
    """16 символов [a-z0-9]. Формат из протокола: телевизор принимает id как
    строку, но длина 16 — то, что используют рабочие клиенты."""
    r = rng or random.SystemRandom()
    alphabet = string.ascii_lowercase + string.digits
    return "".join(r.choice(alphabet) for _ in range(16))


def sign(timestamp: Any, pin: str, secret_b64: str = SECRET_B64) -> str:
    """base64(HMAC-SHA1(secret, str(timestamp) + pin)).

    Порядок конкатенации обратный не работает, и телевизор на неверную
    подпись отвечает тем же кодом, что и на неверный PIN — отладить по
    ответу невозможно. Поэтому порядок зафиксирован тестом.
    """
    key = base64.b64decode(secret_b64)
    msg = f"{timestamp}{pin}".encode("utf-8")
    return base64.b64encode(hmac.new(key, msg, hashlib.sha1).digest()).decode("ascii")


def _device_payload(device_id: str) -> dict[str, Any]:
    return {**DEVICE, "id": device_id}


def request_pair(
    host: str,
    device_id: str,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
    session: Any | None = None,
) -> dict[str, Any]:
    """Шаг 1. Возвращает тело ответа с `auth_key` и `timestamp`."""
    s = session if session is not None else requests.Session()
    payload = {
        "scope": ["read", "write", "control"],
        "device": _device_payload(device_id),
    }
    url = f"https://{host}:{port}/6/pair/request"
    try:
        resp = s.request("POST", url, json=payload, timeout=timeout, verify=False)
    except requests.exceptions.RequestException as e:
        raise TVTransportError(f"pair/request: {e}") from e

    status = getattr(resp, "status_code", None)
    if status is not None and status >= 400:
        raise TVError(f"pair/request: HTTP {status}")
    try:
        data = resp.json()
    except ValueError as e:
        raise TVError("pair/request: ответ не JSON") from e

    missing = [k for k in ("auth_key", "timestamp") if k not in data]
    if missing:
        raise TVError(f"pair/request: в ответе нет {', '.join(missing)}: {data!r}")
    return data


def grant_pair(
    host: str,
    device_id: str,
    auth_key: str,
    timestamp: Any,
    pin: str,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
    session: Any | None = None,
) -> None:
    """Шаг 3. Успех — отсутствие исключения."""
    from requests.auth import HTTPDigestAuth

    s = session if session is not None else requests.Session()
    payload = {
        "auth": {
            "auth_AppId": "1",
            "pin": str(pin),
            "auth_timestamp": timestamp,
            "auth_signature": sign(timestamp, str(pin)),
        },
        "device": _device_payload(device_id),
    }
    url = f"https://{host}:{port}/6/pair/grant"
    try:
        resp = s.request(
            "POST",
            url,
            json=payload,
            auth=HTTPDigestAuth(device_id, auth_key),
            timeout=timeout,
            verify=False,
        )
    except requests.exceptions.RequestException as e:
        raise TVTransportError(f"pair/grant: {e}") from e

    status = getattr(resp, "status_code", None)
    if status is not None and status >= 400:
        raise TVError(
            f"pair/grant: HTTP {status} — неверный PIN, истёк таймаут "
            f"или не совпала подпись"
        )


def pair(
    host: str,
    port: int = DEFAULT_PORT,
    creds_path: Path | None = None,
    session: Any | None = None,
    pin_reader: Any = None,
) -> Path:
    """Полный цикл. `pin_reader` — функция без аргументов, возвращающая PIN
    (в тестах подменяется, в CLI это input)."""
    device_id = new_device_id()
    logger.info("[TV] pair/request -> %s:%s (device_id=%s)", host, port, device_id)
    data = request_pair(host, device_id, port=port, session=session)

    timeout_s = data.get("timeout", "?")
    print(f"PIN показан на экране телевизора. Времени на ввод: {timeout_s} c.")
    reader = pin_reader or (lambda: input("PIN: ").strip())
    pin = str(reader()).strip()
    if not (pin.isdigit() and len(pin) == 4):
        raise ValueError(f"PIN должен быть 4 цифры, получено {pin!r}")

    grant_pair(
        host,
        device_id,
        data["auth_key"],
        data["timestamp"],
        pin,
        port=port,
        session=session,
    )

    return tv_control.save_creds(
        {
            "username": device_id,
            "password": data["auth_key"],
            "host": host,
            "port": port,
            "paired_at": datetime.now().isoformat(timespec="seconds"),
        },
        creds_path,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    host = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TV_HOST", "")
    if not host:
        print("FAIL: укажите IP телевизора аргументом или задайте TV_HOST в .env")
        return 2
    port = int(os.getenv("TV_PORT", str(DEFAULT_PORT)))

    try:
        path = pair(host, port=port)
    except (TVError, ValueError) as e:
        print(f"FAIL: {e}")
        return 1

    # Пара в stdout не печатается: вывод консоли может попасть в скриншот
    # или в журнал. Печатается только путь и факт успеха.
    print(f"OK: пара сохранена в {path}")
    print("Проверка: python src\\tv_control.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
