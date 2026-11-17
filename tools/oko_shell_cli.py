"""oko_shell_cli.py — проверка канала desktop из консоли (SHELL_S1). Ключ берётся из .env в корне репозитория, не печатается.
Запуск:  <python> tools\\oko_shell_cli.py "текст для Ока"
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def env() -> dict:
    out = {}
    p = ROOT / ".env"
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def ask(text: str, timeout: float = 180.0) -> dict:
    e = env()
    key, port = e.get("OKO_SHELL_KEY", ""), int(e.get("OKO_SHELL_PORT", "47321") or 47321)
    if not key:
        return {"ok": False, "error": "no OKO_SHELL_KEY in .env"}
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall((json.dumps({"key": key, "text": text}, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    res = ask(" ".join(sys.argv[1:]) or "привет")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    sys.exit(0 if res.get("ok") else 1)
