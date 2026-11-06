"""garmin_sync.py — суточные метрики Garmin Connect -> локальная SQLite Ока (29.10.2026).

Принцип: данные о теле принадлежат Оператору; копия живёт у него локально (переносимость, ст. 20 GDPR),
Garmin Connect — только источник. Источник измерен: локального API у часов нет (Wi-Fi — только синхронизация
с облаком, Bluetooth — закрытый протокол), поэтому путь — облако Garmin Connect через библиотеку
`garminconnect` (неофициальная; API сверен с установленной версией 0.3.20, не по памяти).

Вход:
  `python garmin_sync.py login` — один раз из консоли Оператора: почта/пароль из переменных окружения
  GARMIN_EMAIL / GARMIN_PASSWORD (блок кладёт их туда через Read-Host -AsSecureString, в историю PowerShell
  они не попадают), код MFA — запрос в консоли. Токены сохраняет сама библиотека в OKO_GARMIN_TOKENS
  (по умолчанию data/.garmin_tokens.json — папка data вне git) и сама их обновляет; пароль нигде не хранится.
  Дальше `sync` работает только по токенам: нет токенов -> GarminAuthNeeded, а не попытка входа.

Сбор (`python garmin_sync.py sync [--date ГГГГ-ММ-ДД] [--days N]`, по умолчанию сегодня и вчера —
сон и вариабельность пульса за ночь дописываются утром): шаги и пульс покоя, сон, вариабельность пульса (HRV),
стресс, батарея тела, тренировки. Каждый раздел запрашивается ОТДЕЛЬНО: сбой одного (сеть, 429, нет данных)
не мешает остальным и записывается в колонку errors. Повторная синхронизация не затирает уже полученное
значение пустым (COALESCE): неудачный запрос днём не стирает утренний сон.
Сырые ответы каждого раздела сохраняются целиком (raw_json): если Garmin переименует поле, данные не потеряны,
а разбор чинится без повторного запроса. В журнал пишутся только имена разделов и счётчики, не значения.

База: OKO_GARMIN_DB, по умолчанию data/oko.db (отдельный файл; база знаний — knowledge_base.db — не трогается).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("oko.garmin")

SECTIONS = ("summary", "sleep", "hrv", "stress", "body_battery", "activities")
METRICS = ("steps", "resting_hr", "sleep_seconds", "deep_sleep_seconds", "light_sleep_seconds",
           "rem_sleep_seconds", "awake_seconds", "sleep_score", "hrv_last_night_avg", "hrv_weekly_avg",
           "hrv_status", "stress_avg", "stress_max", "body_battery_high", "body_battery_low",
           "body_battery_charged", "body_battery_drained", "activities_count", "activities_json")

SCHEMA = """CREATE TABLE IF NOT EXISTS garmin_daily (
    date TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    steps INTEGER, resting_hr INTEGER,
    sleep_seconds INTEGER, deep_sleep_seconds INTEGER, light_sleep_seconds INTEGER,
    rem_sleep_seconds INTEGER, awake_seconds INTEGER, sleep_score INTEGER,
    hrv_last_night_avg REAL, hrv_weekly_avg REAL, hrv_status TEXT,
    stress_avg INTEGER, stress_max INTEGER,
    body_battery_high INTEGER, body_battery_low INTEGER,
    body_battery_charged INTEGER, body_battery_drained INTEGER,
    activities_count INTEGER, activities_json TEXT,
    raw_json TEXT, errors TEXT
)"""


class GarminAuthNeeded(RuntimeError):
    """Нет действующих токенов: нужен разовый `garmin_sync.py login` из консоли Оператора."""


def _base_dir() -> Path:
    try:
        import config
        return Path(config.BASE_DIR)
    except Exception:
        return Path(__file__).resolve().parent.parent / "data"


def tokens_path() -> Path:
    return Path(os.getenv("OKO_GARMIN_TOKENS") or _base_dir() / ".garmin_tokens.json")


def db_path() -> Path:
    return Path(os.getenv("OKO_GARMIN_DB") or _base_dir() / "oko.db")


# ---------------------------------------------------------------- разбор (чистые функции, без сети)
def _get(d: Any, *path, default=None):
    for k in path:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int) and -len(d) <= k < len(d):
            d = d[k]
        else:
            return default
        if d is None:
            return default
    return d


def _int(v) -> int | None:
    try:
        return None if v is None or isinstance(v, bool) else int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def parse_summary(raw) -> dict:
    return {"steps": _int(_get(raw, "totalSteps")), "resting_hr": _int(_get(raw, "restingHeartRate")),
            "stress_avg": _int(_get(raw, "averageStressLevel")), "stress_max": _int(_get(raw, "maxStressLevel")),
            "body_battery_high": _int(_get(raw, "bodyBatteryHighestValue")),
            "body_battery_low": _int(_get(raw, "bodyBatteryLowestValue")),
            "body_battery_charged": _int(_get(raw, "bodyBatteryChargedValue")),
            "body_battery_drained": _int(_get(raw, "bodyBatteryDrainedValue"))}


def parse_sleep(raw) -> dict:
    s = _get(raw, "dailySleepDTO") or {}
    return {"sleep_seconds": _int(_get(s, "sleepTimeSeconds")),
            "deep_sleep_seconds": _int(_get(s, "deepSleepSeconds")),
            "light_sleep_seconds": _int(_get(s, "lightSleepSeconds")),
            "rem_sleep_seconds": _int(_get(s, "remSleepSeconds")),
            "awake_seconds": _int(_get(s, "awakeSleepSeconds")),
            "sleep_score": _int(_get(s, "sleepScores", "overall", "value"))}


def parse_hrv(raw) -> dict:
    h = _get(raw, "hrvSummary") or {}
    status = _get(h, "status")
    return {"hrv_last_night_avg": _float(_get(h, "lastNightAvg")),
            "hrv_weekly_avg": _float(_get(h, "weeklyAvg")),
            "hrv_status": str(status) if status is not None else None}


def parse_stress(raw) -> dict:
    return {"stress_avg": _int(_get(raw, "avgStressLevel")), "stress_max": _int(_get(raw, "maxStressLevel"))}


def parse_body_battery(raw) -> dict:
    day = raw[0] if isinstance(raw, list) and raw else {}
    return {"body_battery_charged": _int(_get(day, "charged")), "body_battery_drained": _int(_get(day, "drained"))}


def parse_activities(raw) -> dict:
    if not isinstance(raw, list):
        return {"activities_count": None, "activities_json": None}
    acts = [{"name": _get(a, "activityName"), "type": _get(a, "activityType", "typeKey"),
             "start": _get(a, "startTimeLocal"), "duration_s": _int(_get(a, "duration")),
             "distance_m": _float(_get(a, "distance")), "calories": _int(_get(a, "calories")),
             "avg_hr": _int(_get(a, "averageHR")), "max_hr": _int(_get(a, "maxHR"))}
            for a in raw if isinstance(a, dict)]
    return {"activities_count": len(acts), "activities_json": json.dumps(acts, ensure_ascii=False)}


PARSERS: dict[str, Callable[[Any], dict]] = {
    "summary": parse_summary, "sleep": parse_sleep, "hrv": parse_hrv, "stress": parse_stress,
    "body_battery": parse_body_battery, "activities": parse_activities}


def fetchers(client, day: str) -> dict[str, Callable[[], Any]]:
    """Вызовы garminconnect 0.3.20 (сигнатуры сверены с установленной библиотекой)."""
    return {"summary": lambda: client.get_user_summary(day),
            "sleep": lambda: client.get_sleep_data(day),
            "hrv": lambda: client.get_hrv_data(day),
            "stress": lambda: client.get_stress_data(day),
            "body_battery": lambda: client.get_body_battery(day, day),
            "activities": lambda: client.get_activities_by_date(day, day)}


# ---------------------------------------------------------------- сбор и запись
@dataclass
class DayResult:
    date: str
    metrics: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)

    @property
    def ok_sections(self) -> list[str]:
        return [s for s in SECTIONS if s in self.raw]


def collect(client, day: str) -> DayResult:
    """Все разделы за день; сбой раздела записывается, остальные собираются. Не бросает на сбоях сети."""
    res = DayResult(day)
    for name, call in fetchers(client, day).items():
        try:
            raw = call()
        except Exception as e:      # сеть, 429, 404 «нет данных», авторизация — у раздела, не у всего дня
            res.errors[name] = f"{type(e).__name__}: {str(e)[:160]}"
            continue
        res.raw[name] = raw
        try:
            parsed = PARSERS[name](raw)
        except Exception as e:      # неожиданная форма ответа: сырой ответ сохранён, разбор отмечен
            res.errors[name] = f"разбор: {type(e).__name__}: {str(e)[:120]}"
            continue
        for k, v in parsed.items():          # одно поле из двух разделов (стресс, батарея): первое непустое
            if res.metrics.get(k) is None:
                res.metrics[k] = v
    return res


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    conn.commit()


def save(conn: sqlite3.Connection, res: DayResult, now: str | None = None) -> None:
    """UPSERT; пустое значение не затирает уже сохранённое, сырые разделы сливаются."""
    ensure_schema(conn)
    old = conn.execute("SELECT raw_json FROM garmin_daily WHERE date = ?", (res.date,)).fetchone()
    raw = {}
    if old and old[0]:
        try:
            raw = json.loads(old[0])
        except ValueError:
            raw = {}
    raw.update(res.raw)
    cols = ["date", "fetched_at", *METRICS, "raw_json", "errors"]
    vals = [res.date, now or dt.datetime.now().isoformat(timespec="seconds"),
            *[res.metrics.get(m) for m in METRICS],
            json.dumps(raw, ensure_ascii=False, default=str),
            json.dumps(res.errors, ensure_ascii=False) if res.errors else None]
    upd = ", ".join([f"{m} = COALESCE(excluded.{m}, garmin_daily.{m})" for m in METRICS]
                    + ["fetched_at = excluded.fetched_at", "raw_json = excluded.raw_json", "errors = excluded.errors"])
    conn.execute(f"INSERT INTO garmin_daily ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                 f"ON CONFLICT(date) DO UPDATE SET {upd}", vals)
    conn.commit()


def days_to_sync(date: str | None, days: int, today: dt.date | None = None) -> list[str]:
    end = dt.date.fromisoformat(date) if date else (today or dt.date.today())
    return [(end - dt.timedelta(days=i)).isoformat() for i in range(max(1, days))]


# ---------------------------------------------------------------- вход и клиент
def client_from_tokens(token_file: Path | None = None, factory: Callable | None = None):
    """Клиент только по сохранённым токенам. Нет файла / токены отвергнуты -> GarminAuthNeeded."""
    tf = token_file or tokens_path()
    if not tf.is_file():
        raise GarminAuthNeeded(f"нет токенов ({tf}); нужен разовый вход: garmin_sync.py login")
    if factory is None:
        from garminconnect import Garmin
        factory = Garmin
    client = factory()
    try:
        client.login(str(tf))
    except Exception as e:
        name = type(e).__name__
        if "Authentication" in name:
            raise GarminAuthNeeded(f"токены отвергнуты ({name}); повтори вход: garmin_sync.py login") from e
        raise
    return client


def interactive_login(token_file: Path | None = None) -> None:
    from garminconnect import Garmin
    email, password = os.getenv("GARMIN_EMAIL", "").strip(), os.getenv("GARMIN_PASSWORD", "")
    if not email or not password:
        raise SystemExit("СТОП: задай GARMIN_EMAIL и GARMIN_PASSWORD в окружении этой консоли")
    tf = token_file or tokens_path()
    tf.parent.mkdir(parents=True, exist_ok=True)
    g = Garmin(email=email, password=password, prompt_mfa=lambda: input("Код MFA из почты/приложения: ").strip())
    g.login(str(tf))
    print(f"[вход] OK, токены: {tf} ({'есть' if tf.is_file() else 'НЕ записаны'})")


def run_sync(date: str | None, days: int, client=None, conn: sqlite3.Connection | None = None) -> list[DayResult]:
    client = client or client_from_tokens()
    own = conn is None
    conn = conn or sqlite3.connect(str(db_path()))
    try:
        out = []
        for day in days_to_sync(date, days):
            res = collect(client, day)
            save(conn, res)
            logger.info("[GARMIN] %s: разделы %s, сбоев %d", day, ",".join(res.ok_sections) or "-", len(res.errors))
            out.append(res)
        return out
    finally:
        if own:
            conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    s = sub.add_parser("sync")
    s.add_argument("--date")
    s.add_argument("--days", type=int, default=2)
    a = ap.parse_args()
    if a.cmd == "login":
        interactive_login()
        return 0
    try:
        results = run_sync(a.date, a.days)
    except GarminAuthNeeded as e:
        print(f"[ВХОД НУЖЕН] {e}")
        return 3
    except Exception as e:
        print(f"[ТРАНСПОРТ/ОБЛАКО] {type(e).__name__}: {e}")
        return 2
    print(f"[база] {db_path()}")
    for r in results:
        filled = sum(1 for m in METRICS if r.metrics.get(m) is not None)
        print(f"{r.date}: разделов {len(r.ok_sections)}/{len(SECTIONS)}, метрик заполнено {filled}/{len(METRICS)}"
              + (f", сбои: {', '.join(f'{k} ({v[:60]})' for k, v in r.errors.items())}" if r.errors else ""))
        empty = [m for m in METRICS if r.metrics.get(m) is None]
        if empty:
            print(f"   пусто: {', '.join(empty)}")
    return 0 if all(not r.errors for r in results) else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(main())
