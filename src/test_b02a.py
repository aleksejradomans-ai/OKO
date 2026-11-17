"""(12.11, «бот слепой», Оператор 17:17; порядок: единый журнал, каталог, словарь).
Живой диалог 17:04–17:16: «vkljuci zeltuju» / «ubavj na 3» / «a bez /tv ti neumeew?» -> «Не выполнялось. Используй /tv».
Контекст (цвет/режим к подсветке), «верни обратно» и модель смысла в обычном режиме -- «расстояние контекста»+ (ловушки), не здесь."""
import asyncio
import inspect

import pytest

import bot
import capabilities as caps
import event_log
import router as R
import tv_nav


# --- A. каталог: модель не «слепая» и не навязывает /tv ---------------------------------------------------------

def test_rule_points_to_facts_not_blindness():
    assert "не видишь" not in caps.RULE
    assert "блока фактов" in caps.RULE
    assert event_log.KIND_NOTE["verified"] in caps.RULE          # те же слова, что модель видит в фактах
    assert "неизвестно" in caps.RULE
    assert "(сделал/включил)" in caps.RULE                    # «сторож выдуманного статуса ТВ»/QFIX: запрет утверждать исполнение остаётся
    assert "без /tv" in caps.RULE


def test_tv_brief_has_no_tv_push():
    tv = next(c for c in caps.DECLARED if c.key == "tv")
    assert "/tv" not in tv.brief and "не видишь" not in tv.brief
    assert "нельзя" in tv.brief


def test_catalog_fits_and_is_not_status_shaped():
    cat = caps.build_catalog()
    assert caps.RULE in cat
    assert R.fake_status(cat) is None, R.fake_status(cat)


# --- B. латиница = кириллица для слов шага/света -------------------------------------------------------------

@pytest.mark.parametrize("lat,cyr", [("ubavj", "убавь"), ("pribavj", "прибавь"), ("sbavj", "сбавь"),
                                     ("prigluwi", "приглуши"), ("svet", "свет"), ("potemnee", "потемнее"),
                                     ("posvetlee", "посветлее")], ids=["ubav", "pribav", "sbav", "priglush", "svet",
                                                                       "potemnee", "posvetlee"])
def test_latin_step_words_canonical(lat, cyr):
    assert tv_nav.canonical(lat) == cyr


def test_live_ubavj_na_3_follows_backlight_context():
    ctx = (R.SUBJ_AMB, "подсветка включи")
    r = R.route("ubavj na 3", "ubavj na 3", intent=None, ctx=ctx)
    assert r.kind == R.TV and r.text == "яркость убавь на 3", (r.kind, r.reason, r.text)
    r0 = R.route("ubavj na 3", "ubavj na 3", intent=None, ctx=None)
    assert r0.kind == R.CLARIFY                                # без контекста -- переспросить, не угадывать


@pytest.mark.parametrize("t", ["ubavj na 3", "sbavj na 2", "prigluwi", "svet krasnij", "potemnee"],
                         ids=["ubav", "sbav", "priglush", "svet", "potemnee"])
def test_latin_routes_like_cyrillic(t):
    cyr = tv_nav.canonical(t)
    for ctx in (None, (R.SUBJ_AMB, "подсветка включи")):
        a, b = R.route(t, t, intent=None, ctx=ctx), R.route(cyr, cyr, intent=None, ctx=ctx)
        assert (a.kind, a.text) == (b.kind, b.text), (t, ctx, a.reason, b.reason)


# --- D. режим /tv пишет события ------------------------------------------------------------------------------

def test_tv_mode_event_written_on_executed(monkeypatch):
    got = []
    monkeypatch.setattr(bot, "_event", lambda src, text, kind=None: got.append((src, text, kind)))
    ok = "Подсветка: режим один цвет (статичный), цвет желтая, hue 40, sat 255, bri 255"
    monkeypatch.setattr(R, "tv_outcome", lambda a: "PASS" if a == ok else None)
    bot._tv_mode_event(ok)
    bot._tv_mode_event("Не выполнено: не понял.")
    assert len(got) == 1 and got[0][0] == "tv" and got[0][1] == ok
    assert got[0][2] == R.tv_kind(ok, "PASS")


def test_tv_mode_event_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk")
    monkeypatch.setattr(R, "tv_outcome", lambda a: "PASS")
    monkeypatch.setattr(bot, "_event", boom)
    bot._tv_mode_event("Подсветка: включена (On)")             # не падает


def test_tv_answer_records_event(monkeypatch):
    seen = []
    monkeypatch.setattr(bot.tv_bot_handler, "handle", lambda uid, text, factory: "Подсветка: включена (On)")
    monkeypatch.setattr(bot.tv_bot_handler, "make_factory", lambda: None)
    monkeypatch.setattr(bot, "_tv_mode_event", lambda a: seen.append(a))
    out = asyncio.run(bot._tv_answer(1, "подсветка включи"))
    assert out == "Подсветка: включена (On)" and seen == [out]


def test_handle_tv_records_event_in_source():
    src = inspect.getsource(bot.handle_tv)
    assert "_tv_mode_event(answer)" in src
