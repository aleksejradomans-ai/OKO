"""garmin_sync (29.10.2026): разбор без сети, частичный сбой разделов, запись без затирания, вход только по токенам.
Облако Garmin подменено; формы ответов — образцы полей, которые разбирает модуль (живой прогон их подтвердит:
модуль печатает, какие метрики пришли пустыми, а сырой ответ сохраняется целиком)."""
import json
import sqlite3

import pytest

import garmin_sync as G

DAY = "2026-10-28"
SUMMARY = {"totalSteps": 8421, "restingHeartRate": 52, "averageStressLevel": 31, "maxStressLevel": 88,
           "bodyBatteryHighestValue": 84, "bodyBatteryLowestValue": 22, "bodyBatteryChargedValue": 61,
           "bodyBatteryDrainedValue": 63}
SLEEP = {"dailySleepDTO": {"sleepTimeSeconds": 25200, "deepSleepSeconds": 5400, "lightSleepSeconds": 14400,
                           "remSleepSeconds": 5400, "awakeSleepSeconds": 900,
                           "sleepScores": {"overall": {"value": 78}}}}
HRV = {"hrvSummary": {"lastNightAvg": 47, "weeklyAvg": 51, "status": "BALANCED"}}
STRESS = {"avgStressLevel": 30, "maxStressLevel": 90}
BB = [{"charged": 60, "drained": 64}]
ACTS = [{"activityName": "Бег", "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-10-28 07:10:00",
         "duration": 1800.4, "distance": 5012.3, "calories": 410, "averageHR": 148, "maxHR": 171}]


class FakeClient:
    def __init__(self, fail=(), data=None):
        self.fail = set(fail)
        self.data = data or {"summary": SUMMARY, "sleep": SLEEP, "hrv": HRV, "stress": STRESS,
                             "body_battery": BB, "activities": ACTS}
        self.calls = []

    def _r(self, name, *args):
        self.calls.append((name, args))
        if name in self.fail:
            raise ConnectionError(f"сеть недоступна ({name})")
        return self.data[name]

    def get_user_summary(self, d): return self._r("summary", d)
    def get_sleep_data(self, d): return self._r("sleep", d)
    def get_hrv_data(self, d): return self._r("hrv", d)
    def get_stress_data(self, d): return self._r("stress", d)
    def get_body_battery(self, a, b=None): return self._r("body_battery", a, b)
    def get_activities_by_date(self, a, b=None): return self._r("activities", a, b)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def _row(conn, day=DAY):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM garmin_daily WHERE date = ?", (day,)).fetchone()


def test_full_day_parsed_and_saved(conn):
    res = G.collect(FakeClient(), DAY)
    assert res.errors == {} and res.ok_sections == list(G.SECTIONS)
    G.save(conn, res, now="2026-10-29T09:00:00")
    r = _row(conn)
    assert (r["steps"], r["resting_hr"], r["sleep_seconds"], r["sleep_score"]) == (8421, 52, 25200, 78)
    assert (r["hrv_last_night_avg"], r["hrv_status"]) == (47.0, "BALANCED")
    assert (r["stress_avg"], r["stress_max"]) == (31, 88)            # первое непустое — из сводки дня
    assert (r["body_battery_high"], r["body_battery_charged"]) == (84, 61)
    acts = json.loads(r["activities_json"])
    assert r["activities_count"] == 1 and acts[0]["type"] == "running" and acts[0]["duration_s"] == 1800
    assert set(json.loads(r["raw_json"])) == set(G.SECTIONS) and r["errors"] is None


def test_requests_use_the_day(conn):
    c = FakeClient()
    G.collect(c, DAY)
    assert ("body_battery", (DAY, DAY)) in c.calls and ("activities", (DAY, DAY)) in c.calls


def test_network_failure_of_sections_is_partial_not_crash(conn):
    res = G.collect(FakeClient(fail={"sleep", "hrv"}), DAY)
    assert set(res.errors) == {"sleep", "hrv"} and "ConnectionError" in res.errors["sleep"]
    G.save(conn, res)
    r = _row(conn)
    assert r["steps"] == 8421 and r["sleep_seconds"] is None
    assert set(json.loads(r["errors"])) == {"sleep", "hrv"}


def test_all_sections_fail_still_records_day(conn):
    res = G.collect(FakeClient(fail=set(G.SECTIONS)), DAY)
    G.save(conn, res)
    r = _row(conn)
    assert r["steps"] is None and len(json.loads(r["errors"])) == len(G.SECTIONS)


def test_resync_failure_does_not_erase_earlier_values(conn):
    G.save(conn, G.collect(FakeClient(), DAY))
    G.save(conn, G.collect(FakeClient(fail={"sleep"}), DAY))
    r = _row(conn)
    assert r["sleep_seconds"] == 25200 and "sleep" in json.loads(r["raw_json"])
    assert "sleep" in json.loads(r["errors"])


def test_newer_value_replaces_older(conn):
    G.save(conn, G.collect(FakeClient(), DAY))
    newer = dict(SUMMARY, totalSteps=12000)
    G.save(conn, G.collect(FakeClient(data={"summary": newer, "sleep": SLEEP, "hrv": HRV, "stress": STRESS,
                                            "body_battery": BB, "activities": ACTS}), DAY))
    assert _row(conn)["steps"] == 12000


@pytest.mark.parametrize("raw", [None, {}, [], "мусор", {"dailySleepDTO": None}, {"hrvSummary": {"lastNightAvg": "x"}}])
def test_parsers_survive_odd_shapes(raw):
    for parse in G.PARSERS.values():
        out = parse(raw)
        assert isinstance(out, dict)


def test_hrv_none_is_no_data_not_error():
    """get_hrv_data возвращает None, если за ночь нет данных (сигнатура библиотеки: dict | None)."""
    res = G.collect(FakeClient(data={"summary": SUMMARY, "sleep": SLEEP, "hrv": None, "stress": STRESS,
                                     "body_battery": BB, "activities": []}), DAY)
    assert "hrv" not in res.errors and res.metrics["hrv_last_night_avg"] is None
    assert res.metrics["activities_count"] == 0


def test_schema_idempotent(conn):
    G.ensure_schema(conn)
    G.ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(garmin_daily)")}
    assert {"date", "steps", "sleep_score", "hrv_last_night_avg", "body_battery_high", "raw_json", "errors"} <= cols


def test_days_to_sync():
    import datetime as dt
    assert G.days_to_sync(None, 2, today=dt.date(2026, 10, 29)) == ["2026-10-29", "2026-10-28"]
    assert G.days_to_sync("2026-10-01", 1) == ["2026-10-01"]


def test_no_token_file_means_login_needed_not_password_attempt(tmp_path):
    called = []
    with pytest.raises(G.GarminAuthNeeded):
        G.client_from_tokens(tmp_path / "nope.json", factory=lambda: called.append(1))
    assert called == []


def test_rejected_tokens_mean_login_needed(tmp_path):
    tf = tmp_path / ".garmin_tokens.json"
    tf.write_text("{}", encoding="utf-8")

    class GarminConnectAuthenticationError(Exception):
        pass

    class Bad:
        def login(self, path):
            raise GarminConnectAuthenticationError("expired")

    with pytest.raises(G.GarminAuthNeeded):
        G.client_from_tokens(tf, factory=Bad)


def test_tokens_login_uses_file_path(tmp_path):
    tf = tmp_path / ".garmin_tokens.json"
    tf.write_text("{}", encoding="utf-8")
    seen = []

    class Ok:
        def login(self, path):
            seen.append(path)

    G.client_from_tokens(tf, factory=Ok)
    assert seen == [str(tf)]


def test_run_sync_logs_no_metric_values(conn, caplog):
    caplog.set_level("INFO", logger="oko.garmin")
    G.run_sync("2026-10-28", 1, client=FakeClient(), conn=conn)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "8421" not in text and "25200" not in text and "[GARMIN]" in text


def test_default_paths_inside_data(monkeypatch, tmp_path):
    monkeypatch.delenv("OKO_GARMIN_TOKENS", raising=False)
    monkeypatch.delenv("OKO_GARMIN_DB", raising=False)
    assert G.tokens_path().name == ".garmin_tokens.json" and G.db_path().name == "oko.db"
    monkeypatch.setenv("OKO_GARMIN_DB", str(tmp_path / "x.db"))
    assert G.db_path() == tmp_path / "x.db"
