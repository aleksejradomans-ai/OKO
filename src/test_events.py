"""EVENTS (этап C, 01.11.2026): журнал событий модулей, состояние техники, факты для мозга."""
import datetime as dt
import json
from pathlib import Path

import pytest

import device_watch as dw
import event_log as el
import router as R
import samsung_names_ru

SRC = Path(__file__).resolve().parent
TZ = dt.timezone(dt.timedelta(hours=2))
NOW = dt.datetime(2026, 11, 1, 16, 40, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _clean_default():
    el.reset()
    yield
    el.reset()


def _ts(h, m, day=1):
    return dt.datetime(2026, 11, day, h, m, tzinfo=TZ).isoformat(timespec="seconds")


# --- EventLog -----------------------------------------------------------------

def test_record_recent_and_reload(tmp_path):
    p = tmp_path / "data" / "events.jsonl"
    log = el.EventLog(p)
    log.record("devices", "♨️ Сушильная: запущена «Эко-хлопок», закончит около 17:44.", ts=_ts(16, 30))
    log.record("tv", "Подсветка: FOLLOW_AUDIO", ts=_ts(16, 35))
    assert [r["src"] for r in log.recent(now=NOW)] == ["devices", "tv"]
    assert [r["src"] for r in el.EventLog(p).recent(now=NOW)] == ["devices", "tv"]
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2          # append-only


def test_recent_filters(tmp_path):
    log = el.EventLog(tmp_path / "e.jsonl")
    log.record("garmin", "старое", ts="2026-10-30T10:00:00+02:00")
    for i in range(12):
        log.record("tv", f"t{i}", ts=_ts(16, i))
    log.record("devices", "d", ts=_ts(16, 20))
    assert len(log.recent(n=8, now=NOW)) == 8
    assert log.recent(n=8, now=NOW)[-1]["src"] == "devices"
    assert [r["text"] for r in log.recent(src="devices", now=NOW)] == ["d"]
    assert not any(r["src"] == "garmin" for r in log.recent(n=50, now=NOW))   # старше суток


def test_text_cleaned_and_capped(tmp_path):
    log = el.EventLog(tmp_path / "e.jsonl")
    rec = log.record("garmin", "☀️ Утро\n😴 Сон 7 ч\n" + "x" * 500, ts=_ts(8, 0))
    assert "\n" not in rec["text"] and len(rec["text"]) == el.MAX_TEXT
    assert rec["text"].startswith("☀️ Утро | 😴 Сон 7 ч | ")


def test_tail_load_skips_cut_first_line_and_bad_lines(tmp_path, monkeypatch):
    p = tmp_path / "e.jsonl"
    good = json.dumps({"ts": _ts(16, 0), "src": "tv", "text": "ok"}, ensure_ascii=False)
    p.write_text("\n".join([good] * 50 + ["{oops", json.dumps({"src": "tv"})] + [good]) + "\n",
                 encoding="utf-8")
    monkeypatch.setattr(el, "TAIL_BYTES", 200)
    log = el.EventLog(p)
    assert log.bad_lines == 2
    assert all(r["text"] == "ok" for r in log.recent(n=100, now=NOW))


def test_write_failure_keeps_memory(tmp_path):
    d = tmp_path / "dir_as_file"
    d.write_text("x")
    log = el.EventLog(d / "e.jsonl")                                       # родитель -- файл
    log.record("tv", "Подсветка: On", ts=_ts(16, 0))
    assert log.recent(now=NOW)[0]["text"] == "Подсветка: On"


def test_module_silent_before_init(tmp_path):
    el.record("tv", "x")                                                   # no-op, без исключения
    assert el.recent() == [] and el.facts_block(now=NOW) == ""


def test_module_record_never_raises(tmp_path):
    el.init(tmp_path / "e.jsonl")
    el.record("", "пустой src")                                            # ValueError внутри -> WARNING
    assert el.recent(now=NOW) == []


# --- блок фактов ----------------------------------------------------------------

def test_facts_block(tmp_path):
    el.init(tmp_path / "e.jsonl")
    el._DEFAULT.record("devices", "♨️ Сушильная: запущена «Эко-хлопок», закончит около 17:44.", ts=_ts(16, 30))
    text = el.facts_block("Сушильная: идёт «Эко-хлопок», по плану до 17:44.", now=NOW)
    lines = text.splitlines()
    assert lines[0].endswith("Сейчас 01.11 16:40.")
    assert lines[1] == "Техника сейчас: Сушильная: идёт «Эко-хлопок», по плану до 17:44."
    assert lines[3] == "[01.11 16:30] Техника: ♨️ Сушильная: запущена «Эко-хлопок», закончит около 17:44."


def test_facts_block_latest_per_source(tmp_path):
    el.init(tmp_path / "e.jsonl")
    for i, src in enumerate(["tv", "tv", "garmin", "tv", "devices", "tv"]):
        el._DEFAULT.record(src, f"{src}{i}", ts=_ts(16, 10 + i))
    lines = el.facts_block(now=NOW).splitlines()
    ev = lines[2:]
    assert [l.split("] ", 1)[1] for l in ev] == ["Garmin: garmin2", "Техника: devices4", "ТВ: tv5"]


def test_facts_block_only_state():
    assert el.facts_block("Стиральная: не работает.", now=NOW).splitlines()[1] == "Техника сейчас: Стиральная: не работает."


# --- device_watch.state_text ------------------------------------------------------

def _snap(kind, machine, cycle=None, completion=None):
    return dw.Snapshot(kind=kind, label=kind, machine=machine, cycle=cycle, completion=completion)


def test_state_text_empty_before_first_poll():
    assert dw.state_text(dw.Monitor(token="t")) == ""
    assert dw.state_text(None) == "" or dw._MONITOR is not None


def test_state_text_running_and_stopped():
    m = dw.Monitor(token="t")
    m.watcher.feed([_snap("washer", "stop"), _snap("dryer", "stop")])
    end = "2026-11-01T15:44:00Z"
    m.watcher.feed([_snap("washer", "stop"), _snap("dryer", "run", "cotton", end)])
    hhmm = dt.datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    name = samsung_names_ru.name_ru("dryer", "cotton")
    assert dw.state_text(m) == f"Сушильная: идёт «{name}», по плану до {hhmm}. Стиральная: не работает."


def test_state_text_marks_broken_link():
    m = dw.Monitor(token="t")
    m.watcher.feed([_snap("washer", "stop")])
    m.transport_fails = 3
    assert "связь с облаком SmartThings сейчас нарушена" in dw.state_text(m)


# --- маршрут devices ----------------------------------------------------------------

@pytest.mark.parametrize("text", ["когда закончит сушилка", "что со стиральной", "Сколько осталось стирке?",
                                  "сушка идёт?", "стиралка работает"])
def test_route_devices(text):
    assert R.route(text, intent=None).kind == R.DEVICES


@pytest.mark.parametrize("text", ["сушилка сломалась", "купить стиральную машину", "техника безопасности"])
def test_route_devices_needs_state_cue(text):
    assert R.route(text, intent=None).kind == R.BRAIN


# --- подключение в bot.py -------------------------------------------------------------

def _bot():
    return (SRC / "bot.py").read_text(encoding="utf-8-sig")


def _func(text, name, sync=False):
    head = f"def {name}(" if sync else f"async def {name}("
    return text.split(head, 1)[1].split("\nasync def ", 1)[0].split("\ndef ", 1)[0]


def test_bot_process_logic_appends_facts_before_governor():
    body = _func(_bot(), "process_logic")
    assert body.index("_home_facts()") < body.index("governor.execute_request(")


def test_bot_jobs_record_events():
    text = _bot()
    for job, src in (("device_watch_job", "devices"), ("net_context_job", "net"), ("garmin_job", "garmin")):
        assert f'_event("{src}", text)' in _func(text, job), job


def test_bot_route_reply_devices_and_events():
    body = _func(_bot(), "_route_reply")
    assert "router.DEVICES" in body and "_devices_answer()" in body
    assert body.index("_event(fn, answer)") > body.index("outcome=%s")


def test_bot_post_init_inits_event_log():
    assert "event_log.init" in _func(_bot(), "post_init")


def test_bot_discuss_has_no_facts():
    assert "_home_facts" not in _func(_bot(), "process_discuss_logic")
