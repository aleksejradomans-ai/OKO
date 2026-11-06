"""TV_STATE (03.11.2026, принцип Оператора): о состоянии ТВ система говорит только «последнее, что ТВ сообщил
после моей команды» -- с временем, без модели. Живой случай 01:22: факт «режим один цвет (статичный)» был в
контексте, мозг ответил «режим подсветки музыки»."""
from pathlib import Path

import pytest

import router as R

SRC = Path(__file__).resolve().parent


@pytest.mark.parametrize("q", [
    "v kakom rezime sejcas rabotaet moj tv",      # живой случай 01:22
    "v kakom rezime rabotaet televizor",          # 00:13 / 00:35
    "в каком режиме работает телевизор",
    "какой режим подсветки?",
    "что с подсветкой",
    "какая громкость сейчас",
    "подсветка работает?",
    "tv rabotaet?",
    "какого цвета подсветка сейчас",
])
def test_state_question_routes_without_model(q):
    def no_intent(t):
        raise AssertionError("модель смысла не должна вызываться")
    assert R.route(q, q, no_intent).kind == R.TVSTATE


@pytest.mark.parametrize("q,kind", [
    ("подсветка music?", R.HINT),                 # D2: вопрос без признака состояния -- подсказка
    ("как сделать подсветку под музыку?", R.HINT),
    ("почему подсветка мигает?", R.BRAIN),        # рассказ
    ("что такое амбилайт?", R.BRAIN),
    ("подсветка клавиатуры не работает?", R.BRAIN),   # другое устройство
    ("какой статус системы", R.BRAIN),            # нет предмета ТВ
    ("подсветка музыка", R.TV),                   # команда
    ("включи подсветку как в кино", R.TV),
    ("включи подсветку, если сейчас темно", R.TV),    # просьба -- не вопрос
    ("что умеешь?", R.SELF),
])
def test_other_routes(q, kind):
    assert R.route(q, q, None).kind == kind


EV = [
    {"ts": "2026-11-02T23:19:36+02:00", "src": "tv", "text": "Подсветка: FOLLOW_AUDIO/ENERGY_ADAPTIVE_BRIGHTNESS"},
    {"ts": "2026-11-02T23:30:00+02:00", "src": "tv", "text": "Громкость: 20/60"},
    {"ts": "2026-11-03T01:13:05+02:00", "src": "tv",
     "text": "Понял как «подсветка зелёная». Подсветка: режим один цвет (статичный), цвет зеленая, hue 85, sat 255, bri 255"},
    {"ts": "2026-11-03T01:14:00+02:00", "src": "tv", "text": "📺 Телевизор не отвечает"},     # сбой -- не состояние
]


def test_state_text_latest_with_time_and_honesty():
    out = R.tv_state_text(EV)
    assert "не опрашивал" in out and "Последнее" in out
    assert "03.11 01:13 — Подсветка: режим один цвет (статичный)" in out
    assert "02.11 23:30 — Громкость: 20/60" in out
    assert "FOLLOW_AUDIO" not in out              # старое состояние подсветки вытеснено новым
    assert "Понял как" not in out and "не отвечает" not in out
    assert "пультом" in out                       # человек знает, чего система не видит


def test_state_text_empty():
    out = R.tv_state_text([])
    assert "не знаю" in out and "не опрашиваю" in out


def test_state_text_is_not_fake_and_not_hint_api():
    out = R.tv_state_text(EV)
    assert R.fake_api(out) is None


def _func(text, name):
    return text.split(f"async def {name}(", 1)[1].split("\nasync def ", 1)[0].split("\ndef ", 1)[0]


def test_bot_tvstate_from_event_log_without_history():
    body = _func((SRC / "bot.py").read_text(encoding="utf-8-sig"), "_route_reply")
    i = body.index("router.TVSTATE")
    branch = body[i:body.index("router.DEVICES")]
    assert 'event_log.recent(' in branch and 'src="tv"' in branch and "tv_state_text" in branch
    assert "_save_exchange" not in branch and "_event(" not in branch


# --- 03.11 01:2x: живые фразы яркости (до правки -- мозг и выдумка «установлена на N») ----------------------
@pytest.mark.parametrize("q,reason", [
    ("jarkostj na 6", "tv_rule:amb_brightness_set"),
    ("jarkostj na 100", "tv_rule:amb_brightness_set"),     # предел 10 -- скажет модуль
    ("podsvetka na 6", "tv_rule:amb_brightness_set"),      # раньше -- только через модель смысла
    ("подсветка на 6", "tv_rule:amb_brightness_set"),
    ("jarkostj minus 4", "tv_rule:amb_step"),
    ("jarkostj plus 2", "tv_rule:amb_step"),               # plus -- шаг, не «поставить 2»
    ("tekuwaja jarkostj", "tv_rule:amb_brightness_get"),   # чтение с ТВ, не память
])
def test_live_brightness_phrases_go_to_tv(q, reason):
    def no_intent(t):
        raise AssertionError("модель смысла не должна вызываться")
    r = R.route(q, q, no_intent)
    assert (r.kind, r.reason) == (R.TV, reason), r


def test_bare_step_without_subject_clarifies():
    """«minus 4» без предмета -- не команда ТВ (предмет обязателен) и не мозг: CLARIFY переспрашивает (03.11)."""
    r = R.route("minus 4", "minus 4", None)
    assert (r.kind, r.reason) == (R.CLARIFY, "clarify:level")


@pytest.mark.parametrize("a,word", [
    ("Яркость подсветки музыки на телевизоре установлена на 6. Если нужно изменить значение, используй команды через /tv.",
     "установлена на 6"),
    ("Яркость подсветки музыки на телевизоре установлена на 251 (было 255).", "установлена на 251"),
    ("Громкость увеличена до 30.", "увеличена до 30"),
])
@pytest.mark.parametrize("q", ["jarkostj na 6", "minus 4", "tekuwaja jarkostj"])
def test_brain_set_claim_is_fake(q, a, word):
    assert R.fake_done(q, a) == word


def test_negated_set_claim_not_flagged():
    assert R.fake_done("minus 4", "Яркость не установлена на 6: я не знаю, к чему относится «минус 4».") is None


def test_state_text_level_does_not_replace_mode():
    ev = EV + [{"ts": "2026-11-03T01:25:00+02:00", "src": "tv", "text": "Понял как «подсветка яркость 6». Подсветка: яркость 6 из 10"}]
    out = R.tv_state_text(ev)
    assert "режим один цвет (статичный)" in out and "03.11 01:25 — Подсветка: яркость 6 из 10" in out


def test_state_question_about_brightness():
    assert R.route("какая яркость сейчас", "какая яркость сейчас", None).kind == R.TVSTATE


# --- 03.11 01:3x: калибровка по живой сессии Оператора (транслит, «w» = ш/щ, без слова «подсветка») ---------
def _no_intent(t):
    raise AssertionError("модель смысла не должна вызываться")


@pytest.mark.parametrize("q", [
    "tekuwij cvet podsvetki?",             # было: мозг «установлен как зелёный» (на ТВ был другой)
    "kakaja sejcas podsvetka?",            # было: подсказка
    "jarkostj podsvetki kakaja?",          # было: подсказка
    "na skoljko sejcas stoit jarkostj?",   # было: подсказка
    "kakoj cvet sejcas na tv?",
    "v kakom rezime tv",
])
def test_live_0136_state_questions(q):
    assert R.route(q, q, _no_intent).kind == R.TVSTATE


@pytest.mark.parametrize("q,reason", [
    ("cvet zeltij sejcas", "tv_rule:amb_colour"),       # было: мозг
    ("vkljuci krasnij cvet", "tv_rule:amb_colour"),     # было: мозг «используй /tv»
    ("podsvetku na zelenij", "tv_rule:amb_colour"),
    ("jarkostj cutj viwe", "tv_rule:amb_step"),         # было: только чтение «5 из 10»
    ("jarkostj sdelaj viwe", "tv_rule:amb_step"),
])
def test_live_0136_commands(q, reason):
    r = R.route(q, q, _no_intent)
    assert (r.kind, r.reason) == (R.TV, reason), r


@pytest.mark.parametrize("q,kind", [
    ("какой цвет мне выбрать для стен?", R.BRAIN),     # нет предмета ТВ -- не состояние
    ("какая громкость лучше для кино", R.BRAIN),       # совет -- мозгу
    ("цвет машины красный", R.BRAIN),                  # другое устройство
])
def test_colour_word_does_not_capture_chat(q, kind):
    assert R.route(q, q, None).kind == kind


def test_live_0136_colour_claim_is_fake():
    a = "Цвет подсветки музыки на телевизоре установлен как зелёный. Для изменения цвета используй команды через /tv."
    assert R.fake_done("tekuwij cvet podsvetki", a) == "установлен как зеленый"
    assert R.state_snapshot("tekuwij cvet podsvetki?", a) == "установлен как зеленый"   # на вопрос -- не в историю


# --- CLARIFY (03.11, решение Оператора): неясно, к какой технике -- переспросить, а не выдумывать --------------
@pytest.mark.parametrize("q,key", [
    ("sdelaj tusklee", "level"),          # живой случай 01:3x: «ярче» без предмета не подсветка (test_tv_commands_ambi)
    ("сделай ярче", "level"),
    ("minus 4", "level"),
    ("потише", "level"),
    ("сон", "sleep"),                     # Garmin или таймер сна ТВ
    ("sleep?", "sleep"),
    ("когда закончит?", "done"),          # стиральная или сушилка
    ("сколько осталось", "done"),
    ("выключи", "onoff"),
    ("vykljuchi", "onoff"),
])
def test_clarify(q, key):
    r = R.route(q, q, _no_intent)
    assert (r.kind, r.reason) == (R.CLARIFY, f"clarify:{key}"), r
    assert r.text.startswith("❓ Уточни") and r.text == R.CLARIFY_TEXT[key]


@pytest.mark.parametrize("q,kind", [
    ("как я спал", R.GARMIN),             # предмет назван -- уточнять нечего
    ("когда закончит сушилка", R.DEVICES),
    ("яркость ниже", R.TV),
    ("подсветка выключи", R.TV),
    ("выключи телевизор", R.TV),
    ("говори тише", R.BRAIN),             # просьба к собеседнику
    ("почему мне плохо спится", R.BRAIN), # рассказ
    ("мне снился странный сон про работу и море", R.BRAIN),   # длинная -- разговор
    ("привет", R.BRAIN),
])
def test_clarify_not_when_clear(q, kind):
    assert R.route(q, q, None).kind == kind


def test_clarify_phrases_route_to_named_device():
    """Каждая фраза в «» из уточнения ведёт туда, куда обещано (иначе уточнение -- тоже выдумка)."""
    import re as _re
    for key, text in R.CLARIFY_TEXT.items():
        for ph in _re.findall(r"«([^»]+)»", text):
            if ph == "/tv":
                continue
            assert R.route(ph, ph, None).kind in (R.TV, R.GARMIN, R.DEVICES), (key, ph)


def test_bot_clarify_answered_without_model_and_history():
    body = _func((SRC / "bot.py").read_text(encoding="utf-8-sig"), "_route_reply")
    i = body.index("router.CLARIFY")
    assert i < body.index("router.DEVICES")
    branch = body[i:body.index("router.DEVICES")]
    assert "return r.text" in branch.split("\n        if ", 1)[0] and "_save_exchange" not in branch.split("\n        if ", 1)[0]


# --- CTX_FOLLOW (03.11 02:0x, «нужно связать намерения»): продолжение без предмета -- к прошлой команде ТВ ---------
AMB_CTX = ("подсветка", "сделай подсветку тусклее")
VOL_CTX = ("громкость", "громкость минус 2")


@pytest.mark.parametrize("q,ctx,reason,text", [
    ("sdelaj ewe", AMB_CTX, "ctx:tv_rule:amb_dimmer", "сделай подсветку тусклее"),   # было: мозг «Не распознал»
    ("vikljuci ee", AMB_CTX, "ctx:tv_rule:amb_off", "подсветка выключи"),            # было: мозг «Выключи телевизор.»
    ("vkljuci ee", AMB_CTX, "ctx:tv_rule:amb_on", "подсветка включи"),
    ("sdelaj ee jarce", AMB_CTX, "ctx:tv_rule:amb_brighter", "подсветка ярче"),      # было: уточнение
    ("minus 2", AMB_CTX, "ctx:tv_rule:amb_step", "яркость минус 2"),
    ("ewe", VOL_CTX, "ctx:tv_rule:volume_down", "громкость минус 2"),
    ("tiwe", VOL_CTX, "ctx:tv_rule:volume_down", "громкость тише"),
    ("vikljuci ego", VOL_CTX, "ctx:tv_rule:mute_on", "выключи звук"),
])
def test_ctx_follow(q, ctx, reason, text):
    r = R.route(q, q, _no_intent, ctx)
    assert (r.kind, r.reason, r.text) == (R.TV, reason, text), r
    assert r.note == f"Понял как «{text}». "


@pytest.mark.parametrize("q", ["kak dela", "pocemu ona migaet", "ewe raz rasskazi", "говори тише", "а она красивая?"])
def test_ctx_does_not_capture_chat(q):
    assert R.route(q, q, None, AMB_CTX).kind != R.TV


def test_ctx_named_subject_wins():
    r = R.route("gromkostj minus 2", "gromkostj minus 2", None, AMB_CTX)
    assert r.reason == "tv_rule:volume_down"


def test_without_ctx_asks():
    assert R.route("sdelaj ewe", "sdelaj ewe", None, None).reason == "clarify:again"
    assert R.route("vikljuci ee", "vikljuci ee", None, None).reason == "clarify:onoff"


@pytest.mark.parametrize("reason,subj", [
    ("tv_rule:amb_step", "подсветка"), ("ctx:tv_rule:amb_off", "подсветка"), ("typo:tv_intent:amb_colour", "подсветка"),
    ("tv_rule:volume_up", "громкость"), ("tv_rule:mute_on", "громкость"), ("tv_rule:standby", None), ("brain", None),
])
def test_tv_subject(reason, subj):
    assert R.tv_subject(reason) == subj


def test_bot_ctx_wiring():
    text = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    body = _func(text, "_route_reply")
    assert body.count("_tv_ctx_get(user_id)") == 2                     # оба вызова route -- с контекстом
    i = body.index("_tv_ctx_set(")
    assert body.index('fn = "tv"') < i and 'outcome in ("PASS", "WARN")' in body[i - 120:i]
    assert "TV_CTX_TTL_S = 600" in text


@pytest.mark.parametrize("q,ctx,text", [
    ("sdelaj ewe na 5", ("подсветка", "подсветка тусклее"), "яркость минус 5"),   # живой 02:28: было «яркость на 5»
    ("ewe na 3", ("подсветка", "яркость плюс 2"), "яркость плюс 3"),
    ("ewe 4", ("громкость", "громкость тише"), "громкость минус 4"),
    ("sdelaj ewe na 5", ("подсветка", "подсветка красная"), "яркость на 5"),     # у прошлой команды нет направления
])
def test_again_number_inherits_direction(q, ctx, text):
    r = R.route(q, q, _no_intent, ctx)
    assert r.kind == R.TV and r.text == text, r


# --- SPLIT_WORD (03.11 11:3x): пробел внутри слова-предмета ------------------------------------------------
@pytest.mark.parametrize("q,reason", [
    ("podsv etka pod muziku", "split:tv_rule:amb_audio"),     # живой: было -- мозг, выдумка «hue 85» (D_GUARD)
    ("под светка красная", "split:tv_rule:amb_colour"),
    ("gromk ost 20", "split:tv_rule:volume_set"),
])
def test_split_subject_word(q, reason):
    r = R.route(q, q, _no_intent)
    assert (r.kind, r.reason) == (R.TV, reason), r
    assert r.note.startswith("Понял как «")


@pytest.mark.parametrize("q", ["kak dela", "как раз", "по дороге домой", "под столом светло"])
def test_split_not_on_chat(q):
    assert R.route(q, q, None).kind != R.TV


def test_outcome_warn_when_camera_says_screen_still_on():
    """CAM_VERIFY (03.11): Standby отправлен, а по кадру экран не погас -- в реестр WARN, не PASS."""
    ok = "Отправил: Режим ожидания (телевизор не подтверждает нажатия — смотри на экран)\n📷 По кадру камеры через 3 с: экран погас (яркость 8/255)."
    bad = "Отправил: Режим ожидания (телевизор не подтверждает нажатия — смотри на экран)\n⚠️ По кадру камеры через 6 с экран НЕ погас (яркость 120/255) — проверь ТВ."
    assert R.tv_outcome(ok) == "PASS"
    assert R.tv_outcome(bad) == "WARN"
