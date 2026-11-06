"""garmin_report (29.10.2026): тексты утренней/вечерней сводки из garmin_daily, честные пустоты, раздельные сбои."""
import datetime as dt
import json
import sqlite3

import pytest

import garmin_report as R
import garmin_sync as G

TODAY = dt.date(2026, 10, 29)
T = {"date": "2026-10-29", "steps": 3120, "resting_hr": 52, "sleep_seconds": 25500, "deep_sleep_seconds": 5400,
     "rem_sleep_seconds": 4800, "sleep_score": 78, "hrv_last_night_avg": 47.0, "hrv_weekly_avg": None,
     "hrv_status": "BALANCED", "stress_avg": 25, "stress_max": 80, "body_battery_high": 84, "body_battery_low": 22,
     "activities_json": json.dumps([{"type": "running", "duration_s": 1800, "distance_m": 5012.3, "avg_hr": 148}])}
Y = {"date": "2026-10-28", "steps": 8421, "stress_avg": 31, "activities_json": "[]"}


def _db(rows):
    c = sqlite3.connect(":memory:")
    G.ensure_schema(c)
    for r in rows:
        res = G.DayResult(r["date"], metrics={k: v for k, v in r.items() if k != "date"})
        G.save(c, res)
    return c


def test_morning_text():
    out = R.morning(T, Y)
    assert "Сон 7 ч 05 мин" in out and "глубокий 1 ч 30 мин" in out and "оценка 78" in out
    assert "HRV за ночь 47 мс — в норме" in out and "неделя" not in out      # weeklyAvg пуст — не выдумываем
    assert "Батарея тела: 22 → 84" in out and "Вчера: 8 421 шаг, стресс 31" in out


def test_morning_without_sleep_says_so():
    out = R.morning({"date": "x"}, None)
    assert "Сна за ночь нет" in out and "HRV" not in out and "0" not in out.replace("Утро", "")


def test_evening_text_and_activity():
    out = R.evening(T, None)
    assert "3 120 шагов" in out and "средний 25, максимум 80" in out and "бег 30 мин 5.0 км пульс 148" in out


def test_evening_no_data():
    out = R.evening(None)
    assert "данных Garmin нет" in out and "Тренировок сегодня нет" in out


def test_run_syncs_then_reports_from_db():
    c = _db([T, Y])
    calls = []
    out = R.run("morning", sync=lambda d, n, conn=None: calls.append((d, n)), conn=c, today=TODAY)
    assert calls == [("2026-10-29", 2)] and "Сон 7 ч 05 мин" in out


def test_run_auth_needed_is_explicit():
    def sync(*a, **k):
        raise G.GarminAuthNeeded("нет токенов")
    out = R.run("evening", sync=sync, conn=_db([]), today=TODAY)
    assert "вход истёк" in out and "garmin_sync.py login" in out


def test_run_network_failure_shows_saved_data_and_says_so():
    def sync(*a, **k):
        raise ConnectionError("нет сети")
    out = R.run("evening", sync=sync, conn=_db([T]), today=TODAY)
    assert "3 120 шагов" in out and "Облако Garmin недоступно (ConnectionError)" in out


def test_unknown_hrv_status_shown_raw():
    out = R.morning(dict(T, hrv_status="SOMETHING_NEW"), None)
    assert "SOMETHING_NEW" in out


def test_local_time_has_zone():
    t = R.local_time(8)
    assert t.hour == 8 and t.tzinfo is not None


def test_enabled_follows_token_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OKO_GARMIN_TOKENS", str(tmp_path / ".garmin_tokens.json"))
    assert R.enabled() is False
    (tmp_path / ".garmin_tokens.json").write_text("{}", encoding="utf-8")
    assert R.enabled() is True


def test_hrv_status_none_is_explained_not_raw():
    """29.10 живьём: «HRV за ночь 46 мс — NONE». NONE у Garmin — личная норма ещё не вычислена."""
    out = R.morning(dict(T, hrv_status="NONE"), None)
    assert "NONE" not in out and "норма ещё не определена" in out


@pytest.mark.parametrize("n,word", [(1, "шаг"), (9682, "шага"), (8421, "шаг"), (3120, "шагов"), (11, "шагов"),
                                    (12, "шагов"), (104, "шага"), (0, "шагов")])
def test_steps_word(n, word):
    assert R.steps_word(n) == word


# --- сравнение с вчера (Оператор 29.10 10:12) ---
YD = {"sleep_seconds": 24000, "sleep_score": 81, "hrv_last_night_avg": 43.0, "resting_hr": 60,
      "body_battery_high": 90, "steps": 9682, "stress_avg": 22,
      "activities_json": json.dumps([{"type": "running", "duration_s": 1800}])}


def test_morning_compares_with_yesterday():
    out = R.morning(T, YD)
    assert "Сон 7 ч 05 мин (вчера 6 ч 40 мин, +25 мин)" in out
    assert "оценка 78 (вчера 81, −3)" in out
    assert "HRV за ночь 47 мс (вчера 43 мс, +4)" in out
    assert "Пульс покоя 52 (вчера 60, −8)" in out
    assert "(вчера максимум 90, −6)" in out


def test_evening_compares_with_yesterday_full_day():
    out = R.evening(T, YD)
    assert "3 120 шагов (вчера за день 9 682, −6 562)" in out
    assert "Стресс: средний 25 (вчера 22, +3), максимум 80" in out
    assert "вчера: бег 30 мин" in out


def test_no_comparison_when_yesterday_missing():
    for out in (R.morning(T, {}), R.evening(T, {})):
        assert "вчера" not in out.lower().replace("вчера:", "")


def test_duration_delta_over_an_hour():
    assert R._vs_dur(7 * 3600, 5 * 3600 + 30 * 60) == " (вчера 5 ч 30 мин, +1 ч 30 мин)"
    assert R._vs_dur(6 * 3600, 6 * 3600) == " (вчера 6 ч 00 мин, ±0 мин)"


def test_run_evening_gets_yesterday():
    c = _db([T, dict(YD, date="2026-10-28")])
    out = R.run("evening", sync=lambda *a, **k: None, conn=c, today=TODAY)
    assert "вчера за день 9 682" in out
