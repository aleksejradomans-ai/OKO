"""layer_digest.py -- B31 ночной счёт слоёв L0..L7 (задача планировщика 03:27, решение Оператора 21.11 20:08 Q5).

  python tools/layer_digest.py [--db data/state/layer_events.db] [--qs data/state/quarantine_state.db] [--dry-run]
  python tools/layer_digest.py --register [--window 7d|1d]     таблица как в REGISTER_L0_L7 (только числа)

Печатает одну строку JSON (только числа и коды); весь вывод -- UTF-8 байтами, строки через LF, на любой платформе. e_k = fail / input, c_k = catch / (catch + fail); деление на ноль ->
null (NO_DATA, не 0); input < 70 -> decision NO_DATA; input = 0 за сутки при живом боте -> слой в списке sterile
(код STERILE в отчёте). Живой бот: свежий heartbeat (data/bot.heartbeat моложе 15 мин) или любые события за сутки.
Итоги -> таблица layer_digest в quarantine_state.db (повтор за те же сутки заменяет строки). Код выхода 0 при успехе.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import layer_events  # noqa: E402

HEARTBEAT_FRESH_S = 15 * 60


def out_text(s: str) -> None:
    """Вывод -- UTF-8 байтами, мимо кодировки локали и перевода строк потока (B31 попытка 2: на Windows stdout,
    отданный в файл / трубу, кодировался cp1251 -- кириллица реестра ломалась у читателя UTF-8)."""
    sys.stdout.flush()
    sys.stdout.buffer.write(s.encode("utf-8"))
    sys.stdout.buffer.flush()


def out_line(s: str) -> None:
    out_text(s + "\n")


def _qs_default() -> Path:
    env = os.getenv("OKO_QUARANTINE_STATE_DB", "").strip()
    return Path(env) if env else ROOT / "data" / "state" / "quarantine_state.db"


def bot_alive(heartbeat: Path, now: dt.datetime) -> "bool | None":
    try:
        age = now.timestamp() - heartbeat.stat().st_mtime
    except OSError:
        return None                       # нет файла -> решает наличие событий
    return age <= HEARTBEAT_FRESH_S


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="layer_digest")
    ap.add_argument("--db", default=None)
    ap.add_argument("--qs", default=None)
    ap.add_argument("--heartbeat", default=str(ROOT / "data" / "bot.heartbeat"))
    ap.add_argument("--now", default=None, help="ISO время для проверки (по умолчанию -- сейчас)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--window", choices=("1d", "7d"), default="7d")
    a = ap.parse_args(argv)
    now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now()
    db = Path(a.db) if a.db else layer_events.default_db()
    try:
        d = layer_events.digest(db, now=now, bot_alive=bot_alive(Path(a.heartbeat), now))
    except Exception as e:  # noqa: BLE001
        out_line(json.dumps({"error": type(e).__name__}))
        return 2
    if a.register:
        out_text(layer_events.register_table(d, a.window))
        return 0
    stored = 0
    if not a.dry_run:
        try:
            stored = layer_events.store_digest(d, Path(a.qs) if a.qs else _qs_default(), now=now)
        except Exception as e:  # noqa: BLE001
            out_line(json.dumps({"error": "store_" + type(e).__name__}))
            return 3
    out = {"day": d["day"], "bot_alive": d["bot_alive"], "sterile": d["sterile"], "codes_sterile": "STERILE" if d["sterile"] else None,
           "lost_1d": d["lost_1d"], "lost_7d": d["lost_7d"], "stored_rows": stored, "min_n": d["min_n"],
           "layers_1d": {L: {k: r[k] for k in ("input", "fail", "catch", "e_k", "c_k", "decision", "parts", "out_of_rate")} for L, r in d["layers_1d"].items()},
           "layers_7d": {L: {k: r[k] for k in ("input", "fail", "catch", "e_k", "c_k", "decision", "parts", "out_of_rate")} for L, r in d["layers_7d"].items()}}
    out_line(json.dumps(out, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
