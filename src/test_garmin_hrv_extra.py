"""Пик HRV за 5 мин и личная норма из raw_json → hrvSummary в утренней сводке (29.10.2026)."""
import json

import garmin_report as gr


def _row(hs, wrap="hrv", **extra):
    row = {"hrv_last_night_avg": 45.0, "hrv_status": "BALANCED", "sleep_seconds": 7 * 3600}
    row["raw_json"] = json.dumps({wrap: {"hrvSummary": hs}})
    row.update(extra)
    return row


def test_peak_and_baseline():
    r = _row({"lastNight5MinHigh": 62, "baseline": {"balancedLow": 38, "balancedUpper": 52.4}})
    assert gr.hrv_extra(r) == "пик 5 мин 62 мс, личная норма 38–52 мс"


def test_baseline_null_only_peak():
    r = _row({"lastNight5MinHigh": 62, "baseline": None, "status": "NONE"})
    assert gr.hrv_extra(r) == "пик 5 мин 62 мс"


def test_baseline_partial_or_inverted_skipped():
    assert gr.hrv_extra(_row({"baseline": {"balancedLow": 38}})) == ""
    assert gr.hrv_extra(_row({"baseline": {"balancedLow": 60, "balancedUpper": 40}})) == ""


def test_zero_peak_kept_bool_rejected():
    assert gr.hrv_extra(_row({"lastNight5MinHigh": 0})) == "пик 5 мин 0 мс"
    assert gr.hrv_extra(_row({"lastNight5MinHigh": True})) == ""


def test_no_raw_or_garbage():
    assert gr.hrv_extra(None) == ""
    assert gr.hrv_extra({"raw_json": None}) == ""
    assert gr.hrv_extra({"raw_json": "{not json"}) == ""
    assert gr.hrv_extra({"raw_json": json.dumps({"sleep": {"x": 1}})}) == ""
    assert gr.hrv_extra({"raw_json": json.dumps({"hrv": {"hrvSummary": "строка"}})}) == ""


def test_nested_any_section():
    r = _row({"lastNight5MinHigh": 70}, wrap="sections")
    r["raw_json"] = json.dumps({"sections": [{"hrv": {"hrvSummary": {"lastNight5MinHigh": 70}}}]})
    assert gr.hrv_extra(r) == "пик 5 мин 70 мс"


def test_morning_line_appended_under_hrv():
    r = _row({"lastNight5MinHigh": 62, "baseline": {"balancedLow": 38, "balancedUpper": 52}})
    text = gr.morning(r, None)
    hrv_line = [l for l in text.split("\n💓")[1:]][0]
    assert "\n    пик 5 мин 62 мс, личная норма 38–52 мс" in hrv_line


def test_morning_no_hrv_avg_no_extra():
    r = _row({"lastNight5MinHigh": 62})
    r["hrv_last_night_avg"] = None
    assert "пик 5 мин" not in gr.morning(r, None)


def test_evening_unchanged():
    r = _row({"lastNight5MinHigh": 62, "baseline": {"balancedLow": 38, "balancedUpper": 52}}, steps=5000)
    assert "пик 5 мин" not in gr.evening(r, None)
