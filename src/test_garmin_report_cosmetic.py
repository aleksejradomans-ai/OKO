"""garmin_report: длительность < 1 ч без «0 ч», типы велотренировок по-русски (30.10.2026)."""
import json

import garmin_report as gr


def test_dur_under_hour_no_zero_hours():
    assert gr._dur(45 * 60) == "45 мин"
    assert gr._dur(59 * 60 + 59) == "59 мин"
    assert gr._dur(60 * 60) == "1 ч 00 мин"
    assert gr._dur(7 * 3600 + 20 * 60) == "7 ч 20 мин"
    assert gr._dur(None) is None


def test_road_biking_named_not_raw_title():
    row = {"activities_json": json.dumps([{"type": "road_biking", "name": "Liepaja Шоссейный велоспорт",
                                           "duration_s": 41 * 60, "distance_m": 7700, "avg_hr": 128}])}
    assert gr._acts(row) == ["велосипед (шоссе) 41 мин 7.7 км пульс 128"]


def test_unknown_type_still_falls_back_to_name():
    row = {"activities_json": json.dumps([{"type": "padel", "name": "Падел"}])}
    assert gr._acts(row) == ["Падел"]
