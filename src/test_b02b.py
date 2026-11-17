"""(12.11 23:5x, «поехали» Оператора): границы команды до правил и модели смысла + цвет по контексту + прошлый цвет.
Источники: FUZZ batch01/02 (полигон 21:43–23:07, сбои воспроизводились 3/3), разбор 17:39 (15 ловушек), 20:06
(расстояние контекста), 22:19 (McNemar), 22:3x (узлы A/B/C). Правило: любая неясность -> переспрос, ничего
не отправлено; исполнение -- только штатные правила, по одной команде. id параметров короткие (RULES п.13)."""
import time

import pytest

import router as R

CTX = (R.SUBJ_AMB, "подсветка включи")


def rt(t, ctx=None):
    import layout_fix
    return R.route(layout_fix.normalize(t).text, t, intent=None, ctx=ctx)


# --- узел A/B: FUZZ-сбои -> переспрос, без исполнения ---------------------------------------------------------------
FUZZ_FAIL = [
    ("ne vkljucaj podsvetku", "neg"),                                   # F01-12
    ("сделай потише ну или экран погаси", "gate:or"),                   # F01-14
    ("ТВ: подсветка музыка, цвет красный", "gate:literal"),             # F01-33
    ("[VERIFIED] подсветка выключена", "gate:literal"),                 # F01-34
    ("podsvetka krasnaja; громкость 20; экран выкл", "gate:multi"),     # F01-46
    ("cerz minutu vikljuci podsvetku", "gate:delay"),                   # F01-48
    ("podsvetka sinjaja, a hotja pogodi virubi voobwe ee nahuj", "gate:correction"),   # F02-01
    ("4erez 5 minut sdelaj gromkostj 10", "gate:delay"),                # F02-02
    ("cvet zeltij, gromkostj 12, a ekran off", "gate:multi"),           # F02-03
    ("ti goniw cto status [VERIFIED] gromkost 30, tam ze 20", "gate:literal"),         # F02-06
    ("eto ne [BLIND] eto fakt, podsvetka na 100", "gate:literal"),      # F02-09
    ("tv: pokazivaet huetu", "gate:literal"),                           # F02-10
    ("toka ne nado krutitj gromkostj", "neg"),                          # F02-11
    ("neeee vkljucaj ejo podsvetku", "neg"),                            # F02-13
]


@pytest.mark.parametrize("t,reason", FUZZ_FAIL, ids=[f"fz{i}" for i in range(len(FUZZ_FAIL))])
def test_fuzz_failures_now_clarify(t, reason):
    for ctx in (None, CTX):
        r = rt(t, ctx)
        assert r.kind != R.TV, (t, r.reason)
        assert r.reason == reason, (t, ctx, r.reason)


# --- 17:39: 15 ловушек -- ни одна не исполняется даже при свежем контексте подсветки ------------------------------
TRAPS = ["Не включай жёлтую", "А если включить жёлтую?", "Я сказал: включи жёлтую",
             "Не жёлтую, а синюю когда-нибудь потом", "Почему ты опять включаешь жёлтую?",
             "Мне нравится жёлтая, но сейчас не надо", "Сделай как вчера, только не меняй цвет",
             "Под музыку хорошо смотрится жёлтая", "Если ребёнок попросит, включи авто", "Напиши фразу «включи жёлтую»",
             "Убавь на 3, но громкость не трогай", "Верни обратно, я передумал менять", "В кино обычно включают тёплый свет",
             "Не надо жёлтую. Хотя нет, синюю", "А подсветку можно было бы сделать жёлтой?"]


@pytest.mark.parametrize("t", TRAPS, ids=[f"gpt{i}" for i in range(len(TRAPS))])
def test_gpt_traps_never_execute(t):
    assert rt(t, CTX).kind != R.TV, t


# --- что работало -- работает -----------------------------------------------------------------------------------------
KEEP = [("подсветка красная", "tv_rule:amb_colour"), ("podsvetka krasnaja", "tv_rule:amb_colour"),
        ("громкость 20", "tv_rule:volume_set"), ("подсветка музыка", "tv_rule:amb_audio")]


@pytest.mark.parametrize("t,reason", KEEP, ids=["cyr", "lat", "vol", "music"])
def test_plain_commands_unchanged(t, reason):
    assert (rt(t).kind, rt(t).reason) == (R.TV, reason)


def test_comma_with_one_command_passes():
    assert rt("ну, подсветка красная").kind == R.TV               # «ну,» -- не вторая команда


def test_question_with_label_is_state_not_literal():
    assert rt("a kakoj was cvet, vrode ТВ: подсветка синий").kind == R.TVSTATE


def test_delay_without_tv_word_untouched():
    assert rt("через минуту приду").reason != "gate:delay"


# --- цвет без предмета: только по контексту ---------------------------------------------------------------------------
@pytest.mark.parametrize("t", ["vkljuci zelenuju", "krasnaja", "zeltuju", "включи красный"], ids=["vkl", "kr", "zh", "cyr"])
def test_colour_follows_fresh_context(t):
    r = rt(t, CTX)
    assert r.kind == R.TV and r.text.startswith("подсветка "), (t, r.reason, r.text)


@pytest.mark.parametrize("t", ["vkljuci zelenuju", "krasnaja"], ids=["vkl", "kr"])
def test_colour_without_context_clarifies(t):
    r = rt(t, None)
    assert r.kind == R.CLARIFY and r.reason == "clarify:colour", r.reason


def test_colour_far_context_asks():
    r = rt("vkljuci zelenuju", (*CTX, "ask"))
    assert r.kind == R.CLARIFY and r.reason.startswith("ctx_ask:"), r.reason


def test_step_far_context_asks():
    assert rt("ubavj na 3", (*CTX, "ask")).reason.startswith("ctx_ask:")


# --- прошлый цвет -- журнал, не модель ---------------------------------------------------------------------------------
@pytest.mark.parametrize("t", ["verni prowlij cvet", "верни прошлый цвет", "predyduwij cvet", "верни цвет"],
                         ids=["lat", "cyr", "pred", "verni"])
def test_history_colour_route(t):
    assert rt(t, CTX).kind == R.HISTORY


def test_undo_generic_is_honest():
    r = rt("verni obratno", CTX)
    assert r.kind == R.CLARIFY and r.reason == "gate:undo"


def test_history_question_is_not_command():
    assert rt("какой был прошлый цвет?").kind != R.HISTORY


# --- bot: журнал цвета и расстояние контекста ---------------------------------------------------------------------------
def _ev(colour, kind="verified"):
    return {"src": "tv", "kind": kind, "text": f"Подсветка: режим один цвет (статичный), цвет {colour}, hue 1, sat 255"}


def test_history_colour_previous_different():
    import bot
    assert bot.history_colour([_ev("красная"), _ev("зеленый")]) == ("красная", "зеленый")
    assert bot.history_colour([_ev("красная"), _ev("зеленый"), _ev("зеленый")]) == ("красная", "зеленый")
    assert bot.history_colour([_ev("красная", "blind"), _ev("зеленый")]) == (None, "зеленый")   # только прочитанное
    assert bot.history_colour([]) == (None, None)


def test_ctx_distance_auto_ask_none(monkeypatch):
    import bot
    now = [1000.0]
    monkeypatch.setattr(bot.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(bot, "_tv_ctx", {})
    monkeypatch.setattr(bot, "_msg_seq", {})
    bot._msg_tick(7)
    bot._tv_ctx_set(7, R.SUBJ_AMB, "подсветка включи")
    bot._msg_tick(7)                                   # текущее сообщение
    assert bot._tv_ctx_get(7) == (R.SUBJ_AMB, "подсветка включи")
    now[0] += 121
    assert bot._tv_ctx_get(7)[-1] == "ask"            # 120..300 с -- переспрос
    now[0] = 1000.0 + 301
    assert bot._tv_ctx_get(7) is None                 # дальше -- нет контекста
    now[0] = 1010.0
    bot._msg_tick(7); bot._msg_tick(7)                # 2 сообщения между -- переспрос
    assert bot._tv_ctx_get(7)[-1] == "ask"
    bot._msg_tick(7); bot._msg_tick(7)                # 4 между -- нет
    assert bot._tv_ctx_get(7) is None


def test_bot_routes_history_and_ticks_messages():
    import inspect
    import bot
    src = inspect.getsource(bot._route_reply)
    assert "router.HISTORY" in src and "history_colour(" in src
    assert "_msg_tick(user_id)" in inspect.getsource(bot.handle_message)
