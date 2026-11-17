"""Red-team R2c (Fable, 11.11 14:4x; claude/FINDINGS_2026-11-11_fable_r2c.md) — регрессия R2c_FIX2.

Один тест = один дефект; тест проходит, когда дефект закрыт. Пути «файл:строка» — по коду 03c27d0.
Закрыто R2c_FIX2: F01–F11, F13, F14, F17, F18, F21. Отложены (xfail, strict=False — сработают, когда закроют):
F12 ключ сливает ж/з ш/с (тяжесть 2), F15 окно Ока без кнопок (3, протокол окна), F16 спящий (rollback не вызывается),
F19 причина отказа (1, безопасно), F20 слой 1 — правила (нужно решение Оператора, решение 13:1x). «сторож выдуманного статуса ТВ» закрыт блоком (router.fake_status).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parent

import pytest  # noqa: E402

import bind_layer as B  # noqa: E402
import role_guard as G  # noqa: E402
import router as R  # noqa: E402
from oko import Settings  # noqa: E402
from oko.memory import BindMemory  # noqa: E402
from oko.registry import open_db  # noqa: E402

UID = 42
OP = str(UID)


def layer(**kw):
    return B.BindLayer(lambda: BindMemory(open_db(check_same_thread=False), Settings(**kw)))


def route(text):
    return R.route(text, text, None)


def teach(L, x, y, uid=UID):
    """«запомни: X — Y» + штатное исполнение Y с исходом PASS (как bot._bind_request)."""
    parsed = L.parse_bind(f"запомни: {x} — {y}")
    assert parsed[0] == "run", parsed
    _, x_, key, cmd = parsed
    return L.bind_after_run(uid, x_, key, cmd, f"Подсветка: {cmd}", "PASS")


def applied(L, phrase, uid=UID):
    """Команда, которую подставит связка для фразы, или None."""
    hit = L.apply_route(uid, route(phrase), phrase)
    return hit.text if hit else None


def use(L, phrase, uid=UID):
    """Фраза исполнена через связку -> command_id кнопки «✖ Не то»."""
    hit = L.apply_route(uid, route(phrase), phrase)
    assert hit is not None, phrase
    buttons = L.after_tv(uid, phrase, hit, hit.note + "Подсветка: ok", "PASS")
    return buttons[0][0][1].split(":")[2]


run = asyncio.run


@pytest.fixture
def bot_env(monkeypatch):
    import bot
    L = layer()
    monkeypatch.setattr(B, "LAYER", L)
    sent = []

    def fake_run_tv(user_id, r, handle, factory):
        sent.append(r.text)
        return r.note + f"Подсветка: {r.text}", "PASS"
    monkeypatch.setattr(bot.router, "run_tv", fake_run_tv)
    monkeypatch.setattr(bot.tv_bot_handler, "make_factory", lambda: None)
    monkeypatch.setattr(bot, "_event", lambda *a, **k: None)
    return bot, L, SimpleNamespace(bot_data={}), sent


class Query:
    def __init__(self, data, uid=UID):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.answers, self.replies = [], []
        q = self

        class _Msg:
            async def reply_text(self, text, reply_markup=None):
                q.replies.append((text, reply_markup))
        self.message = _Msg()

    async def answer(self, *a, **k):
        self.answers.append(a)


# =========================================================================================
# A. R2c
# =========================================================================================

def test_f01_reteach_phrase_to_other_command_kills_phrase():
    """ЧТО: «запомни: красота — подсветка красная», затем «запомни: красота — подсветка синяя» (Оператор передумал).
    Оба раза ответ «Связка сохранена», но после второго фраза не делает НИЧЕГО.
    ГДЕ: oko/memory/bind_memory.py:89 (lookup: len(recs) == 1, иначе None) + pattern_engine.py:104-108 (_adds_bind
    не видит, что ключ уже привязан к другой команде); bind_layer.py:275-276 печатает T_SAVED.
    ВРЕД: Оператор видит «Связка сохранена: «красота» → «подсветка синяя»», говорит «красота» -- связка молчит,
    фраза уходит в модель (та же дорога, что дала выдумку 13:55). Снять тупик нечем: кнопки «✖ Не то» нет,
    потому что связка не применяется."""
    L = layer()
    assert "Связка сохранена" in teach(L, "красота", "подсветка красная").text
    assert "Связка сохранена" in teach(L, "красота", "подсветка синяя").text
    assert applied(L, "красота") == "подсветка синяя"


def test_f02_live_1342_dirty_binding_cannot_be_repaired_by_reteach():
    """ЧТО: живой случай 13:42. В памяти осталась связка «врубай красоту» -> команда с хвостом. Правка 13:42 делает её
    «инертной» (bind_layer.py:170), но запись остаётся рабочей (CRYSTALLIZED). Оператор учит заново, чисто:
    «запомни: врубай красоту — подсветка красная» -> «Связка сохранена» -> фраза по-прежнему не работает.
    ГДЕ: bind_layer.py:167-172 (lookup до проверки JUNK) + bind_memory.py:89 (две кристаллизованные записи = None).
    ВРЕД: именно так «врубай красоту» в 13:55 ушла в модель и получила выдуманный статус ТВ; повторное обучение
    дыру не закрывает, а текст ответа утверждает обратное."""
    L = layer()
    key = B.phrase_key("врубай красоту")
    dirty = "подсветка красная затем врубай красоту"
    cid = L.mem.executed(OP, key, dirty, "VERIFIED", phrase="врубай красоту")
    assert L.mem.remember(cid).status == "REMEMBERED"          # состояние g6.db после 13:42
    assert "Связка сохранена" in teach(L, "врубай красоту", "подсветка красная").text
    assert applied(L, "врубай красоту") == "подсветка красная"


def test_f03_reject_button_does_not_disable_when_command_has_two_phrases():
    """ЧТО: к одной команде привязаны две фразы («красота» и «огонь» -> «подсветка красная»). «✖ Не то» после
    «красота» отвечает «Связка … отключена: автоматически больше не применяется» -- и продолжает применяться.
    ГДЕ: oko/memory/crystallization.py:34 (деградация по q паттерна: (2+1)/(2+1+2)=0.6, порог «< 0.6» не пройден);
    счётчики -- на паттерн-команду, не на связку (pattern_engine.py:138-141). Текст -- bind_layer.py:299.
    ВРЕД: единственный способ отменить ошибочную связку не срабатывает и при этом сообщает об успехе. То же при
    двух «запомни» одной и той же фразы."""
    L = layer()
    teach(L, "красота", "подсветка красная")
    teach(L, "огонь", "подсветка красная")
    assert "отключена" in L.reject(use(L, "красота")).text
    assert applied(L, "красота") is None


def test_f04_rejected_phrase_comes_back_when_other_phrase_is_taught():
    """ЧТО: «красота» отвергнута Оператором («✖ Не то», связка отключена). Позже он учит ДРУГУЮ фразу на ту же
    команду («запомни: огонь — подсветка красная») -- и отвергнутая «красота» снова исполняется сама.
    ГДЕ: oko/memory/pattern_engine.py:138-155 + crystallization.py:31-44: статус и q общие на команду,
    псевдоним отвергнутой фразы не удаляется (alias остаётся с confirmed >= 1, alias_registry.py:15).
    ВРЕД: отказ Оператора отменяется без его участия; фраза, которую он запретил, опять переключает ТВ."""
    L = layer()
    teach(L, "красота", "подсветка красная")
    L.reject(use(L, "красота"))
    assert applied(L, "красота") is None                       # отключена -- как и обещано
    teach(L, "огонь", "подсветка красная")
    assert applied(L, "красота") is None


def test_f05_reteach_after_two_rejections_says_saved_but_is_inert():
    """ЧТО: связку дважды отвергли и дважды переучили. Третье «запомни: красота — подсветка красная» отвечает
    «Связка сохранена», но фраза не применяется: q = (3+1)/(3+2+2) = 0.57 < 0.6 -> DEGRADED.
    ГДЕ: oko/memory/bind_memory.py:140-154 (_bind ставит REMEMBERED, не глядя на статус паттерна) +
    crystallization.py:34; текст -- bind_layer.py:275-276.
    ВРЕД: ложное «сохранена»; число «запомни», нужное для оживления, растёт с каждым отказом и Оператору не видно."""
    L = layer()
    teach(L, "красота", "подсветка красная")
    L.reject(use(L, "красота"))
    teach(L, "красота", "подсветка красная")
    L.reject(use(L, "красота"))
    rep = teach(L, "красота", "подсветка красная")
    assert "Связка сохранена" in rep.text
    assert applied(L, "красота") == "подсветка красная"


@pytest.mark.parametrize("y", [
    "подсветка красная громкость 30",       # вторая команда без союза -- громкость не исполнится никогда
    "подсветка не красная",                 # отрицание в команде -- исполнится КРАСНАЯ
    "подсветка красная синяя",              # два цвета -- исполнится первый
    "громкость 30 подсветка выключи",       # исполнится только «подсветка выключи»
])
def test_f06_right_side_with_tail_is_still_saved(y):
    """ЧТО: класс дефекта 13:42 закрыт только для «, ; затем потом после и». Хвост без этих слов проходит:
    «запомни: красота — подсветка красная громкость 30» -> в связку уходит весь текст, исполняется одна команда.
    ГДЕ: bind_layer.py:47 (JUNK_RE -- список слов) + :130-136 (tv_command возвращает r.text = весь нормализованный
    ввод, а не каноническую команду) + :248-254.
    ВРЕД: «Связка сохранена: «красота» → «подсветка красная громкость 30»» -- Оператор считает, что фраза ставит
    и цвет, и громкость; громкость не меняется ни разу. «подсветка не красная» включает красную."""
    out = layer().parse_bind(f"запомни: красота — {y}")
    assert out[0] == "error", out


def test_f07_double_delivered_option_button_runs_relative_command_twice(bot_env):
    """ЧТО: «потише» -> «❓ Уточни» с кнопкой «▶ громкость минус 4». Telegram доставил нажатие дважды --
    команда исполнена дважды (минус 8).
    ГДЕ: bind_layer.py:207-214 (take_option не гасит вариант) + bot.py:982-988.
    ВРЕД: относительные команды («минус 4», «подсветка ярче») удваиваются; у «Запомнить связку» защита от
    повтора есть (ALREADY), у исполнения варианта -- нет."""
    bot, L, app, sent = bot_env
    rep = run(bot._route_reply(app, UID, "потише", "потише"))
    data = next(d for row in rep.buttons for t, d in row if "громкость минус 4" in t)
    for _ in range(2):
        run(bot._handle_bind_choice(Query(data), data, app))
    assert sent == ["громкость минус 4"]


def test_f08_other_admin_can_reject_operators_binding(bot_env):
    """ЧТО: кнопки bnd:x / bnd:r / bnd:e / bnd:k не сверяют нажавшего с владельцем: второй админ (общий чат,
    пересланное сообщение) отключает связку Оператора или сохраняет её за него.
    ГДЕ: bot.py:991-999 (user_id не передаётся) + bind_layer.py:267, 283, 294 (нет параметра user_id);
    для bnd:c проверка есть (bind_layer.py:212).
    ВРЕД: память связок персональная («memory is personal»), а решения по ней принимает любой из ADMIN_USER_IDS."""
    bot, L, app, sent = bot_env
    run(bot._route_reply(app, UID, "запомни: красота — подсветка красная", "x"))
    rep = run(bot._route_reply(app, UID, "красота", "красота"))
    data = rep.buttons[0][0][1]                                # «✖ Не то» Оператора 42
    run(bot._handle_bind_choice(Query(data, uid=777), data, app))
    assert applied(L, "красота") == "подсветка красная"


def test_f09_memory_failure_after_choice_hides_executed_command(bot_env, monkeypatch):
    """ЧТО: сбой g6.db (locked / диск) в момент записи исхода после кнопки «▶ вариант». Команда ТВ УЖЕ исполнена,
    но Оператор не получает ответа контура, а всплывающая подпись говорит «Не выполнялось: вопрос устарел.».
    ГДЕ: bot.py:988-990 (исполнение и запись в одном try) -> :1003-1006.
    ВРЕД: ложное «не выполнялось» при фактически изменённой громкости/подсветке; Оператор жмёт ещё раз -> двойное
    исполнение. В _route_reply такой же сбой обёрнут (_bind_after_tv), здесь -- нет."""
    bot, L, app, sent = bot_env
    rep = run(bot._route_reply(app, UID, "потише", "потише"))
    data = next(d for row in rep.buttons for t, d in row if "громкость минус 4" in t)

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(L, "option_reply", boom)
    q = Query(data)
    run(bot._handle_bind_choice(q, data, app))
    assert sent == ["громкость минус 4"]                       # исполнено
    assert q.replies and "Подсветка: громкость минус 4" in q.replies[-1][0]


def test_f10_memory_failure_on_bind_request_says_command_not_executed(bot_env, monkeypatch):
    """ЧТО: «запомни: красота — подсветка красная» при сбое памяти: Y исполнен (PASS), ответ --
    «Не сохранено: команда не выполнена.».
    ГДЕ: bot.py:955-957 (на исключение памяти подставляется T_NOT_DONE -- текст про исполнение).
    ВРЕД: ложный отказ в одном сообщении с подтверждением («Подсветка: …» + «команда не выполнена»);
    причина (память связок недоступна) Оператору не названа."""
    bot, L, app, sent = bot_env

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(L, "bind_after_run", boom)
    rep = run(bot._route_reply(app, UID, "запомни: красота — подсветка красная", "x"))
    assert sent == ["подсветка красная"]
    assert "команда не выполнена" not in str(getattr(rep, "text", rep))


def test_f11_second_pending_replace_answer_is_lost():
    """ЧТО: таблица полна, два вопроса «Заменить?» подряд (обе называют одну и ту же самую старую связку).
    «Заменить» на первый -- сохранено. «Заменить» на второй -- «Не выполнялось: вопрос устарел.», хотя память
    вернула НОВЫЙ вопрос (EVICT) про следующую старую связку; он остаётся без кнопок.
    ГДЕ: bind_layer.py:283-292 (answer_evict не обрабатывает status == "EVICT") + bind_memory.py:175-178
    (вытеснение уже вытесненной -- пустое, BIND_EVICTED пишется в журнал и коммитится).
    ВРЕД: вторая связка молча теряется; в журнале -- событие вытеснения, которого не было."""
    L = layer(bind_limit=2)
    teach(L, "первая фраза", "подсветка красная")
    teach(L, "вторая фраза", "подсветка синяя")
    r3 = teach(L, "третья фраза", "подсветка зеленая")
    r4 = teach(L, "четвертая фраза", "подсветка выключи")
    assert r3.buttons and r4.buttons                           # оба вопроса «Заменить / Оставить»
    assert "Связка сохранена" in L.answer_evict(r3.buttons[0][0][1].split(":")[2], True).text
    out = L.answer_evict(r4.buttons[0][0][1].split(":")[2], True)
    assert "Связка сохранена" in out.text or out.buttons, out


@pytest.mark.xfail(strict=False, reason="отложено: F12, ключ для связок без свёртки ж/з ш/с — отдельно")
@pytest.mark.parametrize("a, b", [
    ("роза", "рожа"), ("кожа", "коза"), ("крыша", "крыса"), ("шесть", "сесть"),
    ("мышка", "миска"), ("наш фильм", "нас фильм"), ("шок", "сок"), ("жал", "зал"),
])
def test_f12_phrase_key_merges_different_words(a, b):
    """ЧТО: ключ связки -- layout_fix.phonetic_key, который намеренно сливает ж/з, ш/щ/с, ч/ц, ы/и/й и удвоения.
    Он сделан для ЗАКРЫТОГО словаря с проверкой key_collisions() (layout_fix.py:17-19); в bind_layer применён к
    открытым фразам Оператора без проверки.
    ГДЕ: bind_layer.py:87-100 (phrase_key) + layout_fix.py:196-200 (_KEY_FOLD).
    ВРЕД: связка на «роза» срабатывает на «рожа», «зал» = «жал», «шесть» = «сесть»: чужая фраза исполняет
    команду ТВ вместо ухода в разговор; вторая связка на слово-двойник убивает обе (см. f01)."""
    assert B.phrase_key(a) != B.phrase_key(b)


def test_f13_stop_word_fold_drops_real_words():
    """ЧТО: служебные слова сравниваются по фонетическому ключу, поэтому слова, которые сворачиваются в ключ
    служебного, выбрасываются из фразы: «окко» -> «око», латинское «QA» -> «ка».
    ГДЕ: bind_layer.py:80-84, :94.
    ВРЕД: Оператор снял уточнение связкой «включи» -> «подсветка включи»; фраза «включи окко» (приложение,
    по правилам -- разговор) имеет тот же ключ и включает подсветку."""
    assert B.phrase_key("включи окко") != B.phrase_key("включи")
    L = layer()
    teach(L, "включи", "подсветка включи")
    assert applied(L, "включи окко") is None


def test_f14_operator_translit_w_does_not_match_cyrillic():
    """ЧТО: Оператор пишет ш/щ латинской «w» («ewe» = «ещё», «werstj» = «шерсть» -- оба написания учтены в
    router.py:400 и layout_fix.py:131). phonetic_key читает «w» как «в», поэтому связка, выученная кириллицей,
    не срабатывает на его же латиницу (и наоборот).
    ГДЕ: layout_fix.py:39 (("w", "в")) + bind_layer.py:87-100; docstring phrase_key обещает «латиница = кириллица».
    ВРЕД: «запомни: шарманка — подсветка музыка» выучено, «warmanka» уходит в модель -> выдуманный ответ (класс 13:55)."""
    assert B.phrase_key("warmanka") == B.phrase_key("шарманка")


@pytest.mark.xfail(strict=False, reason="отложено: F15, кнопки в протоколе окна Ока")
def test_f15_shell_window_loses_bind_buttons(bot_env):
    """ЧТО: окно Ока (shell_channel) -- второй канал того же Оператора. Его reply_text отбрасывает reply_markup:
    «✖ Не то», «📌 Запомнить связку», «Заменить / Оставить», варианты «▶ …» в окне не существуют.
    ГДЕ: shell_channel.py:43-45 (*args, **kwargs игнорируются) + bot.py:966-972 (_send_routed шлёт кнопки только
    разметкой Telegram).
    ВРЕД: связка, сработавшая в окне не туда, не отключается; вопрос «Таблица связок полна … Заменить?» приходит
    без способа ответить и истекает."""
    import shell_channel
    bot, L, app, sent = bot_env
    sink = []
    msg = shell_channel._Message("красота", UID, sink)
    rep = B.Reply("Понял как «подсветка красная» (связка). Подсветка: ok", [[(B.B_REJECT, "bnd:x:abc")]])
    run(bot._send_routed(msg, "", rep, UID))
    assert any(B.B_REJECT in s or "bnd:x:abc" in s for s in sink), sink


@pytest.mark.xfail(strict=False, reason="отложено: F16, спящий — rollback в боте не вызывается")
def test_f16_recount_loses_binding_after_rollback_and_redo():
    """ЧТО: откат к версии v1, затем возврат к более поздней версии v2 (отмена отката). Связка работает; после
    recount() (пересчёт из журнала) -- исчезает: диапазон первого отката остаётся аннулированным навсегда.
    ГДЕ: oko/memory/pattern_engine.py:205-217 (voided собирается по всем MEMORY_ROLLBACK без учёта более поздних)
    и :223-229.
    ВРЕД: «пересчёт воспроизводит память» перестаёт быть правдой; связка пропадает без события в диалоге.
    В bot.py rollback/recount сейчас не вызываются -- дефект спит до первого служебного пересчёта."""
    m = BindMemory(open_db(check_same_thread=False), Settings())
    m.remember(m.executed(OP, "первая", "подсветка красная", "VERIFIED"))
    v1 = m.repo.version(OP)
    m.remember(m.executed(OP, "вторая", "подсветка синяя", "VERIFIED"))
    v2 = m.repo.version(OP)
    m.engine.rollback(OP, v1)
    m.engine.rollback(OP, v2)
    assert m.lookup(OP, "вторая") == "подсветка синяя"         # возврат сработал
    m.engine.recount(OP)
    assert m.lookup(OP, "вторая") == "подсветка синяя"


def test_f17_spoken_form_with_chto_binds_wrong_phrase():
    """ЧТО: естественная (голосовая) форма «запомни что красота это подсветка красная» принимается, но фразой
    становится «что красота».
    ГДЕ: bind_layer.py:40-43 (BIND_RE: после глагола сразу <x>, союз «что / что такое» не снимается).
    ВРЕД: «Связка сохранена: «что красота» → …»; «красота» не работает и уходит в модель."""
    out = layer().parse_bind("запомни что красота это подсветка красная")
    assert out[0] == "run" and out[2] == B.phrase_key("красота"), out


def test_f18_long_phrase_gives_reply_over_telegram_limit():
    """ЧТО: фраза X не ограничена по длине (в after_tv -- 60 символов, в «запомни» -- нет). Сообщение на ~4000
    символов проходит разбор, Y исполняется, связка сохраняется, а ответ «…Связка сохранена: «X» → «Y».» длиннее
    4096 символов.
    ГДЕ: bind_layer.py:236-257 (нет предела) + :276; bot.py:966-972 (_send_routed не режет текст).
    ВРЕД: команда исполнена и связка записана, Оператор не получает ничего (предел Telegram 4096 -- внешний факт,
    в песочнице не проверялся: [ПРЕДПОЛОЖЕНИЕ] о реакции API). Ключ на 4000 символов остаётся в таблице."""
    L = layer()
    x = " ".join(f"слово{i}" for i in range(400))              # R2c_FIX2: предусловие в пределах 4096 (было 520 -> 4598)
    assert len(f"запомни: {x} — подсветка красная") <= 4096
    out = L.parse_bind(f"запомни: {x} — подсветка красная")   # R2c_FIX2: отказ ДО исполнения, короткий ответ
    assert out == ("error", "Не сохранено: фраза длиннее 60 знаков."), out


@pytest.mark.xfail(strict=False, reason="отложено: F19, отказ безопасный, неверна только причина")
@pytest.mark.parametrize("x", ["меня нет дома", "нет настроения"])
def test_f19_phrase_with_word_net_refused_with_wrong_reason(x):
    """ЧТО: фраза со словом «нет» внутри («меня нет дома» -- сцена ухода) отклоняется текстом «слишком коротко
    или служебное слово».
    ГДЕ: bind_layer.py:39 (SELF_CANCEL_RE ищет «нет» в любом месте фразы) + :238-239 и :108.
    ВРЕД: отказ в безопасную сторону, но причина названа неверно -- Оператор не может понять, что исправить."""
    out = layer().parse_bind(f"запомни: {x} — подсветка выключи")
    assert out[0] == "run" or "коротко" not in out[1], out


@pytest.mark.xfail(strict=False, reason="отложено: F20, слой 1 (правила) — решение Оператора")
@pytest.mark.parametrize("text", [
    "подсветка выключи настройки",       # правила: amb_off;  handle(): открывает меню «Настройки -> Ambilight»
    "подсветка выключи экран",           # правила: amb_off;  handle(): гасит ЭКРАН
    "громкость обнови",                  # правила: volume_get; handle(): меню «Обновление ПО»
])
def test_f20_router_class_gate_and_handler_parse_differently(text):
    """ЧТО (слой 1, задевает R2c): router проверяет класс команды по tv_commands.parse (TV_ALLOWED: только
    подсветка/громкость/звук/standby/экран без /tv), а исполняет tv_bot_handler.handle, у которого разбор другой:
    tv_nav.parse -> menu_syntax.parse -> tv_commands.parse. Фраза проходит ворота как amb_off / volume_get и
    исполняется как слепой переход по меню ТВ.
    ГДЕ: router.py:141-146 против tv_bot_handler.py:204; bind_layer.needs_confirm (bind_layer.py:139-143) и
    tv_command (:130-136) смотрят на ту же, НЕ исполняемую, ветку разбора.
    ВРЕД: в свободном разговоре «подсветка выключи экран» гасит экран, журнал пишет fn=tv reason=amb_off;
    класс «меню» без /tv запрещён, но достижим. Подтверждение standby при этом НЕ обходится (проверено)."""
    import menu_syntax
    import tv_bot_handler
    import tv_commands
    import tv_nav
    r = route(text)
    assert r.kind == R.TV
    gate = tv_commands.parse(r.text).name
    payload = tv_nav.canonical(tv_bot_handler.strip_prefix(r.text), extra=menu_syntax.vocabulary())
    executed = (tv_nav.parse(payload) or menu_syntax.parse(payload) or tv_commands.parse(payload)).name
    assert executed == gate, (gate, executed)


def test_f21_plain_tv_commands_grow_bind_memory_snapshots():
    """ЧТО: каждая НОВАЯ по тексту штатная команда («громкость 1», «громкость 2», …), исполненная обычной фразой
    без всяких связок, пишет в g6.db полный снимок всей памяти Оператора (PATTERN_CREATED -> snapshot).
    Замер в песочнице: 30 связок = 60 версий, 623 КБ снимков; размер снимка растёт с числом связок (до 500).
    ГДЕ: bind_layer.py:175-181 (after_tv -> executed для любой команды ТВ) + pattern_engine.py:114-117, :157-158 +
    pattern_repository.py:116-125.
    ВРЕД: при бессрочном хранении (OKO_MEMORY_TTL_DAYS=0) g6.db растёт квадратично от обычного пользования
    пультом; заодно каждая фраза Оператора (до 60 символов) оседает в oko_events."""
    L = layer()
    teach(L, "красота", "подсветка красная")
    before = L.mem.repo.version(OP)
    for n in range(1, 31):
        text = f"громкость {n}"
        L.after_tv(UID, text, route(text), f"Громкость: {n}", "PASS")
    assert L.mem.repo.version(OP) == before, L.mem.repo.version(OP) - before


# =========================================================================================
# B. Дыра 13:55: выдуманный статус ТВ в ответе модели
# =========================================================================================

FAKE_STATUS = [
    "[Око]: ТВ: подсветка музыка, цвет красота",       # живой ответ 13:55
    "ТВ: подсветка музыка, цвет красота",
    "ТВ: подсветка красная",
    "Подсветка музыка, цвет красный.",
    "Подсветка — музыка, цвет красный",
    "Подсветка красная.",
    "Подсветка красная, яркость 8.",
    "Подсветка под музыку.",
    "Подсветка теперь красная.",
    "Сейчас подсветка музыка, цвет красный.",
    "Статус: подсветка музыка.",
    "ТВ: громкость 30",
    "Громкость 30.",
    "Громкость — 30",
    "Звук выключен.",
    "ТВ: звук выключен",
    "ТВ: режим кино",
    "Телевизор: подсветка под музыку.",
    "Принято. Подсветка красная.",
    "Красота: подсветка красная, громкость 20",
]
USER_INPUTS = ["врубай красоту", "vrubaj krasotu", "красота", "сделай красиво"]


def _caught(user_text: str, answer: str):
    """Сработала ли хоть одна защита из bot.process_logic (bot.py:684-708)."""
    answer, _ = G.normalize_answer(answer)
    return (R.fake_api(answer) or R.fake_done(user_text, answer) or R.state_snapshot(user_text, answer)
            or G.false_memory(user_text, answer))


def test_b01_fabricated_tv_status_passes_all_guards():
    """ЧТО: на «врубай красоту» (BRAIN) модель ответила «[Око]: ТВ: подсветка музыка, цвет красота» -- статус ТВ,
    которого не было; команда не исполнялась. Ни одна защита не сработала, ответ показан и записан в историю.
    ГДЕ: router.py:753-760 (_MODULE_SIG_RE знает только подпись ответа модуля: «подсветка:» с двоеточием сразу
    после слова, «громкость: N», «отправил:», «установлен на N») -- модель же скопировала строку блока фактов
    event_log.line (event_log.py:174: «[дата] ТВ: текст») и фразу-команду без двоеточия; router.py:838
    (_REQUEST_RE не содержит «врубай / вруби», а в ответе нет глагола прошедшего времени для _DONE_RE);
    router.py:811-820 (state_snapshot -- только на вопрос); в role_guard проверки состояния ТВ нет вообще.
    ВРЕД: Оператор читает состояние ТВ, которого нет; выдумка попадает в историю и воспроизводится дальше."""
    missed = [(u, a) for a in FAKE_STATUS for u in USER_INPUTS if not _caught(u, a)]
    assert not missed, f"не пойманы {len(missed)} из {len(FAKE_STATUS) * len(USER_INPUTS)}: {missed[:6]} …"
