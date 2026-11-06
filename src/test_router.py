"""ROUTER (этап B, 01.11.2026): маршрутизатор свободного текста и голоса.
Модель смысла под pytest не вызывается: intent подменяется функцией или None."""
import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import capabilities as caps
import router as R
import tv_bot_handler
import tv_control

SRC = Path(__file__).resolve().parent


def _route(text, intent=None, raw=None):
    return R.route(text, raw, intent=intent)


def _no_intent(t):
    raise AssertionError("модель смысла не должна вызываться")


# --- маршрут ---------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "что ты умеешь", "Какие функции тебе доступны?", "kakie funkcii tebe dostupni",
    "у тебя есть функция телевизор", "твои возможности",
])
def test_self(text):
    assert _route(text, _no_intent).kind == R.SELF


@pytest.mark.parametrize("text,kind", [
    ("как я спал", "morning"), ("Как прошла ночь?", "morning"), ("какой у меня пульс покоя", "morning"),
    ("сколько шагов", "evening"), ("мой стресс сегодня", "evening"), ("сводка гармин", "auto"),
])
def test_garmin(text, kind):
    r = _route(text, _no_intent)
    assert (r.kind, r.text) == (R.GARMIN, kind)


@pytest.mark.parametrize("text,cmd", [
    ("подсветка музыка", "amb_audio"), ("Включи подсветку под музыку", "amb_audio"),
    ("подсветка красная", "amb_colour"), ("яркость минус 3", "amb_step"),
    ("подсветка выключи", "amb_off"), ("громкость плюс 5", "volume_up"),
    ("выключи звук", "mute_on"), ("выключи телевизор", "standby"),
])
def test_tv_rules(text, cmd):
    r = _route(text, _no_intent)
    assert r.kind == R.TV and r.reason == f"tv_rule:{cmd}" and r.note == ""


def test_tv_power_on_goes_to_wake_on_lan():
    r = _route("включи телевизор", _no_intent)
    assert r.kind == R.TV and r.reason == "tv_rule:power_on"


@pytest.mark.parametrize("text", [
    "вниз",                                   # кнопки -- только в режиме /tv
    "вернись назад к теме",
    "какой статус системы",
    "говори тише",                            # «тише» без «громкость»
    "заглуши мотор",                          # mute без «звук»
    "что такое амбилайт",
    "подсветка клавиатуры не работает",
    "у меня телефон с подсветкой",
    "какая громкость лучше для кино",         # 03.11: «какая громкость сейчас» -- TV_STATE (test_tv_state)
    "почему громкость скачет?",               # D2: вопрос рассказа -- мозгу
    "открой ютуб",                            # отказ приложений -- не повод перехватывать разговор
    "расскажи про подсветку",
    "подсветка у монитора была бы кстати но я пока не решил какую взять в итоге",
    "привет",
])
def test_brain(text):
    assert _route(text, _no_intent).kind == R.BRAIN


def test_live_case_colour_by_rule_without_intent():
    """Живой случай 30.10 15:27 «Подсветка жёлтая»: с 02.11 (TV_HUES_FORMS) -- правило, модель не зовётся
    (в игровом режиме модели смысла нет)."""
    calls = []
    r = _route("Подсветка жёлтая", lambda t: calls.append(t) or "подсветка красный")
    assert r.kind == R.TV and r.reason == "tv_rule:amb_colour"
    assert calls == []


def test_intent_only_with_subject_and_rechecked():
    calls = []

    def intent(t):
        calls.append(t)
        return "подсветка желтый"

    r = _route("Подсветка цвета солнца", intent)   # правила не знают -> модель смысла, ответ перепроверен
    assert r.kind == R.TV and r.text == "подсветка желтый"
    assert r.note == "Понял как «подсветка желтый». " and r.reason == "tv_intent:amb_colour"
    assert calls == ["подсветка цвета солнца"]


def test_intent_light_word():
    r = _route("свет под музыку", lambda t: "подсветка музыка")
    assert r.kind == R.TV and r.reason == "tv_intent:amb_audio"


@pytest.mark.parametrize("phrase", ["вниз 3", "статус", "громче 3", "открой ютуб", None, "ерунда"])
def test_intent_result_outside_class_goes_to_brain(phrase):
    assert _route("подсветка как в клубе", lambda t: phrase).kind == R.BRAIN


def test_translit_raw_variant(monkeypatch):
    import tv_nav
    monkeypatch.setattr(tv_nav, "canonical", lambda s: "подсветка музыка")
    r = R.route("здесь мусор раскладки", raw="podsvetka muzyka", intent=_no_intent)
    assert r.kind == R.TV


def test_translit_when_layout_fix_left_text_unchanged(monkeypatch):
    """01.11 глобальный тест: layout_fix не трогает транслит -> raw == text; вариант по звучанию обязателен."""
    import tv_nav
    monkeypatch.setattr(tv_nav, "canonical", lambda s: "подсветка зеленая")
    assert R.route("podsvetka zelenaja", raw="podsvetka zelenaja", intent=_no_intent).kind == R.TV
    assert R.route("podsvetka zelenaja", intent=_no_intent).kind == R.TV       # голос / без raw


def test_translit_only_for_latin(monkeypatch):
    import tv_nav
    monkeypatch.setattr(tv_nav, "canonical", lambda s: (_ for _ in ()).throw(AssertionError("canonical")))
    assert R._translit("привет") == "привет"


# --- drift: каталог против маршрутизатора -----------------------------------

KEY_TO_KIND = {"tv": R.TV, "garmin": R.GARMIN, "self": R.SELF, "devices": R.DEVICES}


def test_drift_catalog_route_examples():
    seen = 0
    for c in caps.DECLARED:
        for ex in c.route_examples:
            assert _route(ex, _no_intent).kind == KEY_TO_KIND[c.key], ex
            seen += 1
    assert seen >= 5


def test_drift_catalog_quotes_outside_tv_are_routed():
    """Каждая фраза в «» у garmin/self -- пример маршрутизатора (иначе каталог обещает то, чего нет)."""
    import re
    for c in caps.DECLARED:
        if c.key in ("garmin", "self", "devices"):
            for q in re.findall(r"«([^»]+)»", c.text):
                assert q in c.route_examples, (c.key, q)


# --- исполнение ТВ: исход по ответу настоящего tv_bot_handler ---------------

class _TV:
    host = "10.0.0.41"
    moved_from = None

    def __init__(self, style="FOLLOW_AUDIO"):
        self.style = style
        self.set_ambilight_power = MagicMock()
        self.set_ambilight_style = MagicMock()

    def get_ambilight_config(self):
        return {"styleName": self.style, "menuSetting": "ENERGY_ADAPTIVE_BRIGHTNESS"}


def _run_tv(factory, text="подсветка музыка", user=1):
    r = R.Route(R.TV, text)
    handle = lambda uid, t, f: tv_bot_handler.handle(uid, t, f, pending=tv_bot_handler.PendingStore(),
                                                     admin_ids={1})
    return R.run_tv(user, r, handle, factory)


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch, tmp_path):
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")
    monkeypatch.setenv("TV_MACROS_PATH", str(tmp_path / "tv_macros.json"))   # не читать живые пункты


def test_run_tv_pass():
    reply, outcome = _run_tv(lambda: _TV())
    assert outcome == "PASS" and reply.startswith("Подсветка")


def test_run_tv_warn_on_readback_mismatch():
    reply, outcome = _run_tv(lambda: _TV(style="FOLLOW_VIDEO"))
    assert outcome == "WARN" and "телевизор сообщает" in reply


@pytest.mark.parametrize("exc", [
    tv_control.TVNotPaired("x"), tv_control.TVAuthError("x"), tv_control.TVTransportError("x"),
    tv_control.TVError("x"), ValueError("TV_HOST"), RuntimeError("boom"),
])
def test_run_tv_fail_on_every_error_branch(exc):
    def factory():
        raise exc
    reply, outcome = _run_tv(factory)
    assert outcome == "FAIL", reply


def test_run_tv_not_executed():
    assert _run_tv(lambda: _TV(), user=2)[1] is None                         # не админ
    assert _run_tv(lambda: _TV(), text="выключи телевизор")[1] is None       # ждёт подтверждения
    tv = _TV()
    tv.wake = MagicMock()
    assert _run_tv(lambda: tv, text="включи телевизор")[1] == "PASS"         # 03.11: пакет отправлен
    tv.wake.assert_called_once()


def test_tv_outcome_moved_note_is_not_failure():
    reply = "Подсветка: FOLLOW_AUDIO\n📺 Телевизор сменил адрес в сети (a → b) — нашёл его по MAC и запомнил."
    assert R.tv_outcome(reply) == "PASS"


def test_run_tv_keeps_intent_note():
    r = R.Route(R.TV, "подсветка музыка", note="Понял как «подсветка музыка». ")
    reply, outcome = R.run_tv(1, r, lambda u, t, f: "Подсветка: FOLLOW_AUDIO/X", None)
    assert reply.startswith("Понял как") and outcome == "PASS"


# --- Garmin -------------------------------------------------------------------

def test_garmin_kind_auto_by_hour():
    r = R.Route(R.GARMIN, "auto")
    assert R.garmin_kind(r, dt.datetime(2026, 11, 1, 9)) == "morning"
    assert R.garmin_kind(r, dt.datetime(2026, 11, 1, 16)) == "evening"
    assert R.garmin_kind(R.Route(R.GARMIN, "evening"), dt.datetime(2026, 11, 1, 9)) == "evening"


@pytest.mark.parametrize("reply,outcome", [
    ("☀️ Утро\n😴 Сон 7 ч 10 мин", "PASS"),
    ("⚠️ Garmin: вход истёк — нужен разовый вход", "FAIL"),
    ("🌙 Вечер\n⚠️ Облако Garmin недоступно (ConnectError) — показаны последние", "WARN"),
])
def test_garmin_outcome(reply, outcome):
    assert R.run_garmin(R.Route(R.GARMIN, "morning"), lambda k: reply)[1] == outcome


def test_garmin_outcome_matches_real_texts():
    """Тексты сбоев garmin_report -- источник для garmin_outcome (дрейф ловится здесь)."""
    import inspect
    import garmin_report
    src = inspect.getsource(garmin_report.run)
    assert "вход истёк" in src and "недоступно" in src


# --- реестр -------------------------------------------------------------------

def test_should_record():
    today = "2026-11-01"
    assert R.should_record(None, "PASS", today)
    assert not R.should_record({"status": "PASS", "ts": "2026-11-01T10:00:00+02:00"}, "PASS", today)
    assert R.should_record({"status": "PASS", "ts": "2026-11-01T10:00:00+02:00"}, "FAIL", today)
    assert R.should_record({"status": "PASS", "ts": "2026-10-31T10:00:00+02:00"}, "PASS", today)


# --- подключение в bot.py -----------------------------------------------------

def _bot():
    return (SRC / "bot.py").read_text(encoding="utf-8-sig")


def _func(text, name):
    return text.split(f"async def {name}(", 1)[1].split("\nasync def ", 1)[0].split("\ndef ", 1)[0]


def test_bot_voice_stub_removed_and_routed():
    text = _bot()
    assert "Голосом пока управляется телевизор" not in text
    body = _func(text, "handle_voice")
    assert body.index("_route_reply") < body.index("_brain_answer")


def test_bot_message_routes_before_brain_and_not_in_discuss():
    body = _func(_bot(), "handle_message")
    i = body.index("_route_reply")
    assert i < body.index("process_logic(")
    assert "if user_id not in discussion_sessions:" in body[:i]
    assert body.index("_tv_mode_active(user_id)") < i            # режим /tv -- раньше маршрутизатора
