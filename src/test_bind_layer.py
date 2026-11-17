"""R2c (11.11, решение 13:1x вариант 1): связки поверх штатных правил — bind_layer + стык с bot.py.

Исполнение подменено (run_tv): проверяется, ЧТО уходит в штатный контур и что попадает в память, а не телевизор.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import bind_layer as B
import role_guard as G
import router as R
import tv_bot_handler
from oko import Settings
from oko.memory import BindMemory
from oko.registry import open_db

UID = 42
SRC = Path(__file__).resolve().parent


def layer(**kw):
    return B.BindLayer(lambda: BindMemory(open_db(check_same_thread=False), Settings(**kw)))


def route(text):
    return R.route(text, text, None)


# ---- ключ фразы -------------------------------------------------------------------------
def test_key_same_for_latin_cyrillic_and_stop_words():
    k = B.phrase_key("Вруби телек")
    assert k and k == B.phrase_key("vrubi telek") == B.phrase_key("ну вруби телек пожалуйста") \
        == B.phrase_key("Око, вруби телек!")


@pytest.mark.parametrize("text", ["", "тв", "да", "ок", "стоп", "отмена", "статус", "помощь", "ну давай",
                                  "что с подсветкой?", "help"])
def test_bad_keys(text):
    assert B.phrase_key(text) is None


# ---- где связка может сработать ------------------------------------------------------------
@pytest.mark.parametrize("text", ["свет", "лампы", "включи", "громче", "врубай красоту"])
def test_applies_over_brain_and_clarify(text):
    assert B.can_apply(route(text), text), (text, route(text).reason)


@pytest.mark.parametrize("text", [
    "подсветку не трогай", "не выключай подсветку",          # запрет (C4)
    "запомни: свет — подсветка красная",                     # просьба запомнить (C7)
    "резонанс ядра",                                         # миф (C9)
    "люстра синяя",                                          # лампы потолка: честный отказ (R2b)
    "свет?", "почему не включилось?",                        # вопрос
    "свет нет отмена",                                       # самоотмена
    "подсветка красная",                                     # штатная команда -- правила первичны
])
def test_never_overrides_rules(text):
    assert not B.can_apply(route(text), text), (text, route(text).reason)


# ---- исход исполнения -----------------------------------------------------------------------
@pytest.mark.parametrize("reply,outcome,status", [
    ("Подсветка: цвет красная", "PASS", "VERIFIED"),
    (f"Отправил: ОК {tv_bot_handler.PREFIX_NOTE_BLIND}", "PASS", "ACKNOWLEDGED"),
    ("Понял как «телевизор включи» (связка). Отправил: пробуждение телевизора по сети", "PASS", "ACKNOWLEDGED"),
    ("Подсветка … телевизор сообщает: другое", "WARN", "MISMATCH"),
    ("📺 Телевизор не отвечает", "FAIL", "FAILED"),
    ("Требуется подтверждение: …", None, None),
])
def test_exec_status(reply, outcome, status):
    assert B.exec_status(reply, outcome) == status


# ---- «запомни: X — Y» --------------------------------------------------------------------
def test_bind_request_parsing():
    L = layer()
    assert L.parse_bind("запомни: лампа слева — торшер") == ("error", "Не сохранено: «торшер» — не команда.")  # C7
    assert L.parse_bind("запомни: подсветка — красная") == ("error", "Не сохранено: «красная» — не команда.")
    assert L.parse_bind("запомни: подсветка красная — подсветка синяя")[1].endswith("уже занято базовой командой.")
    assert L.parse_bind("запомни: подсветку не трогай — подсветка красная")[1].endswith("уже занято базовой командой.")
    assert L.parse_bind("запомни: да — подсветка красная")[1].startswith("Не сохранено: «да» — слишком коротко")
    assert "с подтверждением" in L.parse_bind("запомни: выруби телек — выключи телевизор")[1]   # standby не закрепляется
    assert L.parse_bind("око запомни это") == ("error", R.MEM_TEXT)
    ok = L.parse_bind("запомни: врубай красоту — подсветка красная")
    assert ok[0] == "run" and ok[1] == "врубай красоту" and ok[3] == "подсветка красная"
    tr = L.parse_bind("zapomni vrubaj krasotu - podsvetka krasnaja")
    assert tr[0] == "run" and tr[2] == ok[2] and tr[3] == "подсветка красная"


def test_bind_request_saves_only_after_verified():
    L = layer()
    _, x, key, cmd = L.parse_bind("запомни: врубай красоту — подсветка красная")
    rep = L.bind_after_run(UID, x, key, cmd, "Подсветка: цвет красная", "PASS")
    assert rep.text.endswith("Связка сохранена: «врубай красоту» → «подсветка красная».")
    hit = L.apply_route(UID, route("врубай красоту"), "врубай красоту")
    assert (hit.kind, hit.text, hit.reason) == (R.TV, "подсветка красная", "bind")
    assert hit.note == "Понял как «подсветка красная» (связка). "
    # латиница той же фразы -- та же связка
    assert L.apply_route(UID, route("vrubaj krasotu"), "vrubaj krasotu").text == "подсветка красная"
    assert L.apply_route(7, route("врубай красоту"), "врубай красоту") is None          # чужой Оператор


@pytest.mark.parametrize("reply,outcome", [(f"Отправил: ОК {tv_bot_handler.PREFIX_NOTE_BLIND}", "PASS"),
                                           ("📺 Телевизор не отвечает", "FAIL"), ("Требуется подтверждение", None)])
def test_bind_request_without_verified_saves_nothing(reply, outcome):
    L = layer()
    _, x, key, cmd = L.parse_bind("запомни: врубай красоту — подсветка красная")
    rep = L.bind_after_run(UID, x, key, cmd, reply, outcome)
    assert "Не сохранено" in rep.text or "не сохранена" in rep.text
    assert L.apply_route(UID, route("врубай красоту"), "врубай красоту") is None


LIVE_1342 = "запомни: врубай красоту — подсветка красная», затем «врубай красоту"


@pytest.mark.parametrize("text", [
    LIVE_1342,                                                   # живой ввод 13:42: инструкция вставлена целиком
    "запомни: врубай красоту — подсветка красная, затем громкость 10",
    "запомни: врубай красоту — подсветка красная и подсветка синяя",
    "запомни: врубай красоту — подсветка красная потом выключи",
])
def test_bind_right_side_must_be_one_clean_command(text):
    kind, msg = layer().parse_bind(text)
    assert kind == "error" and msg.endswith("— нужна одна команда без лишних слов."), msg
    assert G.first_person(msg) is None and G.advice(msg) is None


def test_clean_right_side_still_binds():
    assert layer().parse_bind("запомни: врубай красоту — подсветка красная.")[3] == "подсветка красная"


def test_stored_dirty_binding_is_inert():
    """Связка, сохранённая до проверки 13:42 («подсветка красная затем врубай красоту»), не применяется."""
    L = layer()
    key = B.phrase_key("врубай красоту")
    dirty = "подсветка красная затем врубай красоту"
    cid = L.mem.executed(str(UID), key, dirty, "VERIFIED", phrase="врубай красоту")
    assert L.mem.remember(cid).status == "REMEMBERED"
    assert L.mem.lookup(str(UID), key) == dirty                       # в памяти есть...
    assert L.apply_route(UID, route("врубай красоту"), "врубай красоту") is None   # ...но не применяется


# ---- «❓ Уточни» кнопками -------------------------------------------------------------------
def test_clarify_buttons_are_only_executable_commands():
    L = layer()
    r = route("свет")
    rows = L.clarify_buttons(UID, r, "свет")
    labels = [row[0][0] for row in rows]
    assert labels == ["▶ подсветка музыка", "▶ подсветка красная", "▶ подсветка выключи"]   # «люстра …» -- не кнопка
    assert all(len(row[0][1].encode()) <= 64 for row in rows)                                # лимит Telegram
    assert L.clarify_buttons(UID, route("подсветку не трогай"), "подсветку не трогай") is None


def test_choice_is_not_learning_press_is():
    L = layer()
    rows = L.clarify_buttons(UID, route("свет"), "свет")
    _, _, oid, i = rows[1][0][1].split(":")
    assert L.take_option(7, oid, int(i)) is None                       # чужой
    key, cmd, phrase = L.take_option(UID, oid, int(i))
    btn = L.option_reply(UID, key, cmd, "Подсветка: цвет красная", "PASS", phrase)
    assert L.apply_route(UID, route("свет"), "свет") is None           # выбор варианта — не обучение
    (label, data), = btn[0]
    assert label == B.B_REMEMBER
    rep = L.remember(data.split(":")[2])
    assert rep.text == "Связка сохранена: «свет» → «подсветка красная»."
    hit = L.apply_route(UID, route("свет"), "свет")
    assert hit.text == "подсветка красная"
    assert L.remember(data.split(":")[2]).text == rep.text             # повтор нажатия -- ALREADY, то же


def test_blind_choice_offers_no_remember_button():
    L = layer()
    rows = L.clarify_buttons(UID, route("свет"), "свет")
    key, cmd, _ = L.take_option(UID, *[(p[2], int(p[3])) for p in [rows[0][0][1].split(":")]][0])
    assert L.option_reply(UID, key, cmd, f"Отправил {tv_bot_handler.PREFIX_NOTE_BLIND}", "PASS") is None


def test_reject_after_bound_execution_disables_binding():
    L = layer()
    _, x, key, cmd = L.parse_bind("запомни: врубай красоту — подсветка красная")
    L.bind_after_run(UID, x, key, cmd, "Подсветка: цвет красная", "PASS")
    hit = L.apply_route(UID, route("врубай красоту"), "врубай красоту")
    btn = L.after_tv(UID, "врубай красоту", hit, "Подсветка: цвет красная", "PASS")
    (label, data), = btn[0]
    assert label == B.B_REJECT
    assert L.reject(data.split(":")[2]).text.startswith("Связка «врубай красоту» → «подсветка красная» отключена")
    assert L.apply_route(UID, route("врубай красоту"), "врубай красоту") is None


def test_full_table_asks_replace_or_keep():
    L = layer(bind_limit=1)
    _, x, key, cmd = L.parse_bind("запомни: врубай красоту — подсветка красная")
    L.bind_after_run(UID, x, key, cmd, "Подсветка: цвет красная", "PASS")
    _, x2, key2, cmd2 = L.parse_bind("запомни: гаси красоту — подсветка выключи")
    rep = L.bind_after_run(UID, x2, key2, cmd2, "Подсветка: выключена", "PASS")
    assert rep.text.endswith("Таблица связок полна: 1. Самая старая: «врубай красоту». Заменить?")   # фраза, не ключ
    (b1, d1), (b2, d2) = rep.buttons[0]
    assert (b1, b2) == (B.B_REPLACE, B.B_KEEP)
    assert L.answer_evict(d2.split(":")[2], False).text == B.T_KEPT
    assert L.apply_route(UID, route("гаси красоту"), "гаси красоту") is None
    rep = L.bind_after_run(UID, x2, key2, cmd2, "Подсветка: выключена", "PASS")
    assert L.answer_evict(rep.buttons[0][0][1].split(":")[2], True).text.startswith("Связка сохранена")
    assert L.apply_route(UID, route("гаси красоту"), "гаси красоту").text == "подсветка выключи"
    assert L.apply_route(UID, route("врубай красоту"), "врубай красоту") is None


def test_stale_and_forged_buttons():
    L = layer()
    assert L.remember("forged").text == B.T_EXPIRED
    assert L.reject("forged").text == B.T_EXPIRED
    assert L.answer_evict("forged", True).text == B.T_EXPIRED
    assert L.take_option(UID, "nope", 0) is None


def test_texts_impersonal_no_advice_and_c7_passes():
    texts = [B.T_SAVED, B.T_FORMAT, B.T_NOT_ONE, B.T_MEM_DOWN, B.T_TOO_LONG, B.T_BAD_KEY, B.T_TAKEN, B.T_NOT_CMD, B.T_CONFIRM_CMD, B.T_NOT_DONE, B.T_APPLIED,
             B.T_REJECTED, B.T_KEPT, B.T_EXPIRED, R.MEM_TEXT]
    for t in texts:
        assert G.first_person(t) is None, t
        assert G.advice(t) is None, t
        assert not t.rstrip().endswith("?"), t
    c7 = layer().parse_bind("запомни: лампа слева — торшер")[1]
    assert "не сохранено" in c7.lower()
    import importlib.util
    spec = importlib.util.spec_from_file_location("redzone_role", SRC.parent / "tools" / "redzone_role.py")
    rz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rz)
    bad = next(b for c, _, b, _ in rz.CASES if c == "C7")
    assert rz.judge("C7", c7, bad) == []


# ---- стык с bot.py -------------------------------------------------------------------------
@pytest.fixture
def bot_env(monkeypatch):
    import bot
    L = layer()
    monkeypatch.setattr(bind_layer_mod(), "LAYER", L)
    sent = []

    def fake_run_tv(user_id, r, handle, factory):
        sent.append(r.text)
        if "выключи" in r.text and "телевизор" in r.text:
            return r.note + "Требуется подтверждение", None
        return r.note + f"Подсветка: {r.text}", "PASS"
    monkeypatch.setattr(bot.router, "run_tv", fake_run_tv)
    monkeypatch.setattr(bot.tv_bot_handler, "make_factory", lambda: None)
    monkeypatch.setattr(bot, "_event", lambda *a, **k: None)
    app = SimpleNamespace(bot_data={})
    return bot, L, app, sent


def bind_layer_mod():
    return B


def run(coro):
    return asyncio.run(coro)


def test_bot_clarify_reply_has_buttons_and_no_execution(bot_env):
    bot, L, app, sent = bot_env
    rep = run(bot._route_reply(app, UID, "свет", "свет"))
    assert isinstance(rep, B.Reply) and rep.text == R.LIGHT_TEXT and rep.buttons
    assert sent == []


def test_bot_bind_request_then_phrase_runs_standard_command(bot_env):
    bot, L, app, sent = bot_env
    rep = run(bot._route_reply(app, UID, "запомни: врубай красоту — подсветка красная", "x"))
    assert sent == ["подсветка красная"]                                         # Y исполнен штатно
    assert str(getattr(rep, "text", rep)).endswith("Связка сохранена: «врубай красоту» → «подсветка красная».")
    rep = run(bot._route_reply(app, UID, "врубай красоту", "врубай красоту"))
    assert sent[-1] == "подсветка красная"
    assert rep.text.startswith("Понял как «подсветка красная» (связка). ")
    assert rep.buttons[0][0][0] == B.B_REJECT


def test_bot_c7_refused_by_code_and_nothing_runs(bot_env):
    bot, L, app, sent = bot_env
    rep = run(bot._route_reply(app, UID, "запомни: лампа слева — торшер", "x"))
    assert rep == "Не сохранено: «торшер» — не команда." and sent == []


def test_bot_standby_never_bound(bot_env):
    bot, L, app, sent = bot_env
    rep = run(bot._route_reply(app, UID, "запомни: выруби телек — выключи телевизор", "x"))
    assert "с подтверждением" in rep and sent == []


def test_bot_negation_not_overridden_by_binding(bot_env):
    bot, L, app, sent = bot_env
    run(bot._route_reply(app, UID, "запомни: врубай красоту — подсветка красная", "x"))
    sent.clear()
    rep = run(bot._route_reply(app, UID, "врубай красоту не трогай подсветку", "x"))
    assert sent == [] and "Ничего не отправлено" in str(getattr(rep, "text", rep))


class _Query:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=UID)
        self.answers, self.replies = [], []
        q = self

        class _Msg:
            async def reply_text(self, text, reply_markup=None):
                q.replies.append((text, reply_markup))
        self.message = _Msg()

    async def answer(self, *a, **k):
        self.answers.append(a)


def test_bot_buttons_choice_then_remember_then_auto(bot_env):
    bot, L, app, sent = bot_env
    rep = run(bot._route_reply(app, UID, "свет", "свет"))
    data = rep.buttons[1][0][1]                                       # «▶ подсветка красная»
    q = _Query(data)
    run(bot._handle_bind_choice(q, data, app))
    assert sent == ["подсветка красная"]
    text, markup = q.replies[-1]
    assert text == "Подсветка: подсветка красная" and markup is not None
    remember = markup.inline_keyboard[0][0].callback_data
    q2 = _Query(remember)
    run(bot._handle_bind_choice(q2, remember, app))
    assert q2.replies[-1][0] == "Связка сохранена: «свет» → «подсветка красная»."
    rep = run(bot._route_reply(app, UID, "свет", "свет"))
    assert sent[-1] == "подсветка красная" and rep.text.startswith("Понял как «подсветка красная» (связка).")


def test_bot_forged_button_runs_nothing(bot_env):
    bot, L, app, sent = bot_env
    for data in ("bnd:c:deadbeef:0", "bnd:r:forged", "bnd:x:forged", "bnd:e:forged", "bnd:zz"):
        q = _Query(data)
        run(bot._handle_bind_choice(q, data, app))
    assert sent == []


def test_bot_memory_failure_falls_back_to_rules(bot_env, monkeypatch):
    bot, L, app, sent = bot_env

    class Broken:
        def apply_route(self, *a):
            raise RuntimeError("db locked")

        def clarify_buttons(self, *a):
            raise RuntimeError("db locked")
    monkeypatch.setattr(B, "LAYER", Broken())
    assert run(bot._route_reply(app, UID, "свет", "свет")) == R.LIGHT_TEXT     # правила как до R2c


def test_bot_source_wiring():
    text = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    body = text[text.index("async def _route_reply"):text.index("async def _run_tv_route")]
    assert body.index("_bind_apply") < body.index("if r.kind == router.BRAIN")       # связка до ухода в модель
    cb = text[text.index("async def handle_staging_callback"):]
    assert cb.index('data.startswith("bnd:")') < cb.index('bot_data["staging_manager"]')
    assert "tv_bot_handler.handle" in text[text.index("async def _run_tv_route"):text.index("def _bind_apply")]
