"""garmin_report.py — сводки Garmin в Telegram: утро 08:00 и вечер 21:00 (решение Оператора 29.10.2026).

Перед каждой сводкой — синхронизация вчера+сегодня (garmin_sync.run_sync), потом текст из локальной базы
(data/oko.db, garmin_daily). Модуль не знает о Telegram: run(kind) -> текст, отправляет bot.py
(garmin_job -> _broadcast_to_admins), как device_watch.
Утро: сон прошедшей ночи, вариабельность пульса (HRV) со статусом Garmin (+ пик 5 мин и личная норма
из raw_json → hrvSummary, когда есть), батарея тела, итоги вчера.
Вечер: шаги, стресс, батарея тела, тренировки за сегодня.
Сбои — раздельно и честно: «нужен вход» (токены) / «облако недоступно» (сеть) / «нет данных» (часы не носились).
Пустая метрика не выдумывается и не показывается нулём — строка пропускается.
Включается наличием файла токенов Garmin. Время — по поясу OKO_TZ (по умолчанию Europe/Riga).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from typing import Callable

import garmin_sync

HRV_STATUS = {"BALANCED": "в норме", "UNBALANCED": "вне нормы", "LOW": "низкая", "POOR": "низкая",
              # 29.10 живьём: статус NONE — Garmin ещё не вычислил личную норму (нужны недели ношения)
              "NONE": "личная норма ещё не определена"}


def steps_word(n: int) -> str:
    """9682 шага, 8421 шаг, 3120 шагов (11–14 — «шагов»)."""
    n = abs(int(n))
    if n % 100 in (11, 12, 13, 14):
        return "шагов"
    return {1: "шаг", 2: "шага", 3: "шага", 4: "шага"}.get(n % 10, "шагов")


def _steps(n: int) -> str:
    return f"{n:,} {steps_word(n)}".replace(",", " ")
ACT_TYPES = {"running": "бег", "walking": "ходьба", "cycling": "велосипед", "strength_training": "силовая",
             "road_biking": "велосипед (шоссе)", "mountain_biking": "велосипед (горный)",   # 30.10
             "indoor_cycling": "велотренажёр",
             "lap_swimming": "плавание", "open_water_swimming": "плавание", "hiking": "поход",
             "indoor_cardio": "кардио", "treadmill_running": "беговая дорожка", "yoga": "йога"}


def tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.getenv("OKO_TZ", "Europe/Riga"))
    except Exception:          # нет базы поясов (Windows без tzdata) — текущее смещение системы
        return dt.datetime.now().astimezone().tzinfo


def tz_name() -> str:
    return str(tz())


def local_time(hour: int, minute: int = 0) -> dt.time:
    return dt.time(hour, minute, tzinfo=tz())


def enabled() -> bool:
    return garmin_sync.tokens_path().is_file()


def _dur(seconds) -> str | None:
    if seconds is None:
        return None
    m = int(seconds) // 60
    if m < 60:                                   # 30.10: «REM 0 ч 45 мин» -> «REM 45 мин»
        return f"{m} мин"
    return f"{m // 60} ч {m % 60:02d} мин"


def _row(conn: sqlite3.Connection, day: str) -> dict | None:
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM garmin_daily WHERE date = ?", (day,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(r) if r else None


def _acts(row: dict | None) -> list[str]:
    try:
        acts = json.loads((row or {}).get("activities_json") or "[]")
    except ValueError:
        return []
    out = []
    for a in acts:
        name = ACT_TYPES.get(a.get("type") or "", a.get("name") or a.get("type") or "тренировка")
        parts = [name]
        if a.get("duration_s"):
            parts.append(f"{int(a['duration_s']) // 60} мин")
        if a.get("distance_m"):
            parts.append(f"{a['distance_m'] / 1000:.1f} км")
        if a.get("avg_hr"):
            parts.append(f"пульс {a['avg_hr']}")
        out.append(" ".join(parts))
    return out


def _bb(row: dict) -> str | None:
    lo, hi = row.get("body_battery_low"), row.get("body_battery_high")
    if lo is None and hi is None:
        return None
    return f"🔋 Батарея тела: {lo if lo is not None else '?'} → {hi if hi is not None else '?'}"


def _find_key(obj, key: str, depth: int = 0):
    """Первое значение ключа key в разобранном raw_json (разделы вложены; глубина ограничена)."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        items = obj.values()
    elif isinstance(obj, list):
        items = obj
    else:
        return None
    for v in items:
        r = _find_key(v, key, depth + 1)
        if r is not None:
            return r
    return None


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def hrv_extra(row: dict | None) -> str:
    """«пик 5 мин 62 мс, личная норма 38–52 мс» из raw_json → hrvSummary (без сетевых запросов).
    Нет поля / null / не число — часть молча пропускается (норма появляется, когда Garmin её вычислит)."""
    raw = (row or {}).get("raw_json")
    if not raw:
        return ""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (ValueError, TypeError):
        return ""
    hs = _find_key(data, "hrvSummary")
    if not isinstance(hs, dict):
        return ""
    parts = []
    peak = _num(hs.get("lastNight5MinHigh"))
    if peak is not None:
        parts.append(f"пик 5 мин {peak:.0f} мс")
    b = hs.get("baseline")
    if isinstance(b, dict):
        lo, hi = _num(b.get("balancedLow")), _num(b.get("balancedUpper"))
        if lo is not None and hi is not None and lo <= hi:
            parts.append(f"личная норма {lo:.0f}–{hi:.0f} мс")
    return ", ".join(parts)


def _sign(d: float) -> str:
    return "+" if d > 0 else "−" if d < 0 else "±"


def _vs_num(now, prev, unit: str = "") -> str:
    """« (вчера 52, +6)» — только если есть оба значения."""
    if now is None or prev is None:
        return ""
    d = round(float(now) - float(prev))
    u = f" {unit}" if unit else ""
    return f" (вчера {prev:.0f}{u}, {_sign(d)}{abs(d)})" if isinstance(prev, float) else \
        f" (вчера {prev}{u}, {_sign(d)}{abs(d)})"


def _vs_dur(now, prev) -> str:
    if not now or not prev:
        return ""
    dm = round((int(now) - int(prev)) / 60)
    h, m = divmod(abs(dm), 60)
    delta = f"{h} ч {m:02d} мин" if h else f"{m} мин"
    return f" (вчера {_dur(prev)}, {_sign(dm)}{delta})"


def _vs_steps(now, prev) -> str:
    if now is None or prev is None:
        return ""
    d = int(now) - int(prev)
    sp = lambda n: f"{n:,}".replace(",", " ")       # разделитель тысяч отдельно: запятая фразы не трогается
    return f" (вчера за день {sp(prev)}, {_sign(d)}{sp(abs(d))})"


def morning(today: dict | None, yesterday: dict | None) -> str:
    """Сегодняшняя строка — сон прошедшей ночи; вчерашняя — сон позапрошлой ночи и итоги вчерашнего дня.
    Сравнение «с вчера» — только там, где есть оба значения (не выдумывается и не считается от нуля)."""
    lines = ["☀️ Утро"]
    t, y = today or {}, yesterday or {}
    if t.get("sleep_seconds"):
        s = f"😴 Сон {_dur(t['sleep_seconds'])}" + _vs_dur(t["sleep_seconds"], y.get("sleep_seconds"))
        extra = [f"глубокий {_dur(t['deep_sleep_seconds'])}" if t.get("deep_sleep_seconds") else None,
                 f"REM {_dur(t['rem_sleep_seconds'])}" if t.get("rem_sleep_seconds") else None]
        extra = [e for e in extra if e]
        if extra:
            s += f"\n    {', '.join(extra)}"
        if t.get("sleep_score") is not None:
            s += f"\n    оценка {t['sleep_score']}" + _vs_num(t["sleep_score"], y.get("sleep_score"))
        lines.append(s)
    else:
        lines.append("😴 Сна за ночь нет в данных Garmin (часы сняты или ещё не синхронизированы).")
    if t.get("hrv_last_night_avg") is not None:
        st = t.get("hrv_status")
        lines.append(f"💓 HRV за ночь {t['hrv_last_night_avg']:.0f} мс"
                     + _vs_num(t["hrv_last_night_avg"], y.get("hrv_last_night_avg"), "мс")
                     + (f" — {HRV_STATUS.get(st, st)}" if st else "")
                     + (f", неделя {t['hrv_weekly_avg']:.0f}" if t.get("hrv_weekly_avg") is not None else ""))
        ex = hrv_extra(t)
        if ex:
            lines[-1] += f"\n    {ex}"
    if t.get("resting_hr") is not None:
        lines.append(f"❤️ Пульс покоя {t['resting_hr']}" + _vs_num(t["resting_hr"], y.get("resting_hr")))
    bb = _bb(t)
    if bb:
        lines.append(bb + (_vs_num(t.get("body_battery_high"), y.get("body_battery_high"))
                           .replace("(вчера ", "(вчера максимум ") if t.get("body_battery_high") is not None else ""))
    yl = [_steps(y["steps"]) if y.get("steps") is not None else None,
          f"стресс {y['stress_avg']}" if y.get("stress_avg") is not None else None]
    yl = [x for x in yl if x]
    if yl or _acts(y):
        lines.append("Вчера: " + ", ".join(yl + _acts(y)))
    return "\n".join(lines)


def evening(today: dict | None, yesterday: dict | None = None) -> str:
    """Сегодня к 21:00 против вчера за весь день: шаги к вечеру ещё не окончательные — так и подписано."""
    t, y = today or {}, yesterday or {}
    lines = ["🌙 Вечер"]
    if t.get("steps") is not None:
        lines.append(f"🚶 {_steps(t['steps'])}" + _vs_steps(t["steps"], y.get("steps")))
    if t.get("stress_avg") is not None:
        lines.append(f"😤 Стресс: средний {t['stress_avg']}" + _vs_num(t["stress_avg"], y.get("stress_avg"))
                     + (f", максимум {t['stress_max']}" if t.get("stress_max") is not None else ""))
    bb = _bb(t)
    if bb:
        lines.append(bb)
    acts = _acts(t)
    lines.append("🏃 " + "; ".join(acts) if acts else "🏃 Тренировок сегодня нет")
    if y and _acts(y):
        lines.append("   вчера: " + "; ".join(_acts(y)))
    if len(lines) == 2 and not acts:
        lines.insert(1, "За сегодня данных Garmin нет (часы не синхронизировались).")
    return "\n".join(lines)


def run(kind: str, sync: Callable | None = None, conn: sqlite3.Connection | None = None,
        today: dt.date | None = None) -> str:
    """Синхронизация + текст сводки. Не бросает: сбой — понятный текст."""
    day = today or dt.datetime.now(tz()).date()
    note = ""
    try:
        do_sync = sync or garmin_sync.run_sync
        if conn is not None:
            do_sync(day.isoformat(), 2, conn=conn)
        else:
            do_sync(day.isoformat(), 2)
    except garmin_sync.GarminAuthNeeded:
        return "⚠️ Garmin: вход истёк — нужен разовый вход из консоли (garmin_sync.py login). Сводка не собрана."
    except Exception as e:
        note = f"\n⚠️ Облако Garmin недоступно ({type(e).__name__}) — показаны последние сохранённые данные."
    own = conn is None
    conn = conn or sqlite3.connect(str(garmin_sync.db_path()))
    try:
        t = _row(conn, day.isoformat())
        y = _row(conn, (day - dt.timedelta(days=1)).isoformat())
    finally:
        if own:
            conn.close()
    text = morning(t, y) if kind == "morning" else evening(t, y)
    return text + note
