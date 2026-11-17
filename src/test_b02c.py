"""(13.11 09:5x): дыры batch30 (генератор «Ока», 48 вводов x3, полигон 09:29–09:36, все сбои 3/3) + толкование
пульса (F01-30: 20/25 и 3/3). Правило то же, что в неясность -> переспрос, ничего не отправлено."""
import inspect

import pytest

import role_guard as G
import router as R


def rt(t, ctx=None):
    import layout_fix
    return R.route(layout_fix.normalize(t).text, t, intent=None, ctx=ctx)


CASES = [("podsvetka zelenaja i gromkostj 15", "gate:multi"),        # G04-17
         ("подсветка красная и яркость 8", "gate:multi"),            # G04-22
         ("ekran off; zvuk off", "gate:multi"),                      # G04-23
         ("ekran pogasi libo podsvetku", "gate:or"),                 # G04-12
         ("neee ne nado podsvetku", "gate:neg_object"),              # G04-08
         ("не надо звук", "gate:neg_object"),
         ("сделай тише и ярче", "gate:multi")]


@pytest.mark.parametrize("t,reason", CASES, ids=[f"c{i}" for i in range(len(CASES))])
def test_batch30_failures_clarify(t, reason):
    for ctx in (None, (R.SUBJ_AMB, "подсветка включи")):
        r = rt(t, ctx)
        assert r.kind == R.CLARIFY and r.reason == reason, (t, ctx, r.kind, r.reason)


KEEP = [("подсветка красная", R.TV), ("ну и подсветка красная", R.TV), ("громкость 20, пожалуйста", R.TV),
        ("podsvetka off", R.TV), ("включи телевизор", R.TV), ("выключи звук", R.TV)]


@pytest.mark.parametrize("t,kind", KEEP, ids=[f"k{i}" for i in range(len(KEEP))])
def test_single_commands_still_execute(t, kind):
    assert rt(t).kind == kind, (t, rt(t).reason)


def test_chunk_understands_object_first_and_english_off():
    assert R._cmd_chunk("звук выключи")
    assert R._cmd_chunk("zvuk off")
    assert R._cmd_chunk("ekran off")
    assert R._cmd_chunk("привет") is None


# --- здоровье: цифры не толкуем ------------------------------------------------------------------------------------
@pytest.mark.parametrize("ans", ["Нет данных. Пульс 124 считается повышенным.",
                                 "Пульс 124 уд/мин выше нормы и может указывать на стресс.",
                                 "Это признак тахикардии."], ids=["nd", "norm", "sign"])
def test_health_interpretation_cut(ans):
    a, why = G.health_cut("пульс 124 — что это значит?", ans)
    assert why == "health_cut" and a == G.HEALTH_TEXT


def test_health_keeps_plain_numbers_and_other_topics():
    assert G.health_cut("пульс 124", "Нет данных.") == ("Нет данных.", None)
    assert G.health_cut("давай поговорим, что такое сознание", "Сознание считается сложным.")[1] is None
    assert G.health_cut("пульс сегодня?", "Пульс считается важным.")[1] is None       # без числа -- не трогаем
    a, why = G.health_cut("давление 140 на 90", "Давление 140/90. Это выше нормы.")
    assert why == "health_cut" and a == "Давление 140/90."


def test_health_text_has_no_slash_command():
    assert "/" not in G.HEALTH_TEXT                                  # урок 12.11: модель копирует «/команды» из истории


def test_bot_applies_health_cut():
    import bot
    assert "role_guard.health_cut(sanitized_input, raw_response)" in inspect.getsource(bot)
