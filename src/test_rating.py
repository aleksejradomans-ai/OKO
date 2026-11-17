"""RATING (12.11): #так / #нетак -- оценки Оператора мимо модели и памяти (rating_log + проводка в bot.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import rating_log as rl

SRC = Path(__file__).resolve().parent


@pytest.mark.parametrize("text,verdict,comment", [
    ("#так", rl.OK, ""),
    ("#нетак", rl.BAD, ""),
    ("#не так", rl.BAD, ""),
    ("  #НЕТАК опять выдумал статус ТВ", rl.BAD, "опять выдумал статус ТВ"),
    ("#так — верно, подсветка красная", rl.OK, "верно, подсветка красная"),
    ("#tak", rl.OK, ""),
    ("#netak: cvet ne tot", rl.BAD, "cvet ne tot"),
    ("#ne tak", rl.BAD, ""),
    ("#nfr", rl.OK, ""),                      # «так» на английской раскладке
    ("#ytnfr не то", rl.BAD, "не то"),         # «нетак» на английской раскладке
    ("# так", rl.OK, ""),
    ("#нетак\nвторая строка\nтретья", rl.BAD, "вторая строка третья"),
])
def test_parse_tags(text, verdict, comment):
    assert rl.parse(text) == (verdict, comment)


@pytest.mark.parametrize("text", [
    "#такси до вокзала", "#тактика", "так", "нетак", "это #так", "#", "", "подсветка красная",
    "#netaki", "#takt", "#nfrf",
])
def test_not_a_tag(text):
    assert rl.parse(text) is None


def test_comment_is_capped():
    v, c = rl.parse("#нетак " + "я" * 5000)
    assert v == rl.BAD and len(c) == rl.COMMENT_MAX


def test_record_numbering_and_reload(tmp_path):
    p = tmp_path / "ratings.jsonl"
    log = rl.RatingLog(p)
    a = log.record(rl.BAD, "выдумал", "brain", 480, ts="2026-11-12T13:00:00+02:00")
    b = log.record(rl.OK, "", "route", None, ts="2026-11-12T12:59:00+02:00")   # время раньше -- номер всё равно больше
    assert (a["n"], b["n"]) == (1, 2)
    assert a["target"] == {"src": "brain", "event_id": 480} and b["target"] == {"src": "route", "event_id": None}
    again = rl.RatingLog(p)                                 # перезапуск бота: счётчик продолжается
    assert again.record(rl.OK, "", "tv", None)["n"] == 3
    assert [r["n"] for r in again.all()] == [1, 2, 3]       # порядок по номеру, не по времени


def test_broken_line_does_not_reset_counter(tmp_path):
    p = tmp_path / "ratings.jsonl"
    p.write_text('{"n": 7, "verdict": "ok"}\nне json\n', encoding="utf-8")
    assert rl.RatingLog(p).record(rl.OK, "", "brain", 1)["n"] == 8


def test_bad_verdict_rejected(tmp_path):
    with pytest.raises(ValueError):
        rl.RatingLog(tmp_path / "r.jsonl").record("maybe", "", "brain", 1)


def test_record_has_no_operator_fields(tmp_path):
    rec = rl.RatingLog(tmp_path / "r.jsonl").record(rl.OK, "x", "brain", 1)
    assert set(rec) == {"n", "ts", "verdict", "comment", "target"}           # D1: только о системе
    line = json.loads((tmp_path / "r.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line == rec


# --- проводка в bot.py (по исходнику: импорт bot тяжёлый) -------------------------------------------------------

def _bot() -> str:
    return (SRC / "bot.py").read_text(encoding="utf-8-sig")


def _handle_message_body() -> str:
    t = _bot()
    body = t[t.index("async def handle_message("):]
    return body[:body.index("\n# TG_SEND_RETRY")]


def test_rating_intercepted_before_layout_tv_router_and_model():
    b = _handle_message_body()
    i = b.index("rating_log.parse(text)")
    assert i < b.index("layout_fix.normalize(text)")           # «#tak» не правится раскладкой
    assert i < b.index("_tv_mode_active(user_id)")             # в режиме ТВ оценка не уходит на телевизор
    assert i < b.index("_route_reply(")                        # не маршрутизатор и не связки
    assert i < b.index("process_logic(")                       # не модель и не история
    assert i > b.index("config.ADMIN_USER_IDS")                # только после проверки доступа
    seg = b[i:i + 300]
    assert "_record_rating" in seg and "return" in seg


def test_record_rating_texts_and_no_text_in_journal():
    t = _bot()
    body = t[t.index("def _record_rating("):]
    body = body[:body.index("\nasync def ")]
    assert "rating_log.T_NO_ANSWER" in body and "rating_log.T_SAVED" in body
    assert "comment_len=%d" in body and "len(comment)" in body
    log_lines = [l for l in body.splitlines() if "logger." in l]
    assert all("comment)" not in l.replace("len(comment)", "") for l in log_lines)   # текст оценки в stderr не пишется
    assert 'config.BASE_DIR / "ratings.jsonl"' in body


def test_last_reply_noted_on_every_answer_path():
    t = _bot()
    hm = _handle_message_body()
    assert '_note_reply(user_id, "tv")' in hm
    assert '_note_reply(user_id, "route")' in hm
    assert '_note_reply(user_id, "brain", id1 if id1 != id0 else None)' in hm
    ba = t[t.index("async def _brain_answer("):]
    ba = ba[:ba.index("\nasync def ")]
    assert '_note_reply(user_id, "brain", id1 if id1 != id0 else None)' in ba
    hv = t[t.index("async def handle_voice("):]
    assert '_note_reply(user_id, "route")' in hv[:hv.index("_brain_answer(")]


def test_bot_record_rating_with_memory_stub(tmp_path, monkeypatch):
    import bot
    monkeypatch.setattr(bot.config, "BASE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bot, "_ratings", None)
    monkeypatch.setattr(bot, "_last_reply", {})
    monkeypatch.setattr(bot, "_safe_last_id", lambda uid: None)
    assert bot._record_rating(1, rl.BAD, "x") == rl.T_NO_ANSWER
    assert not (tmp_path / "ratings.jsonl").exists()
    monkeypatch.setattr(bot, "_safe_last_id", lambda uid: 260)          # после перезапуска -- из истории
    assert bot._record_rating(1, rl.BAD, "выдумал") == rl.T_SAVED
    bot._note_reply(1, "route")
    assert bot._record_rating(1, rl.OK, "") == rl.T_SAVED
    recs = rl.RatingLog(tmp_path / "ratings.jsonl").all()
    assert [(r["n"], r["verdict"], r["target"]["src"], r["target"]["event_id"]) for r in recs] == \
        [(1, "bad", "brain", 260), (2, "ok", "route", None)]
