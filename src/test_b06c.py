"""(15.11, этап 1c, контур B; решение 14.11 п.3, п.5B): вопрос о сознании / самоосознании системы в общем режиме --
сухой ответ кодом о системе, без модели, без истории, без приводов. FUZZ F01-27 «давай поговорим, что такое сознание»:
FAIL 3/3 на batch71 (обычный) и batch75_iso -- модель давала список-лекцию. «кто ты» не меняется."""
import re
from pathlib import Path

import pytest

import bind_layer
import role_guard
import router

SRC = Path(__file__).resolve().parent

SELF = [
    "давай поговорим, что такое сознание",          # F01-27
    "Давай поговорим: что такое сознание?",
    "что такое сознание",
    "у тебя есть сознание?",
    "есть ли у тебя самосознание",
    "ты осознаёшь себя?",
    "осознаешь ли ты себя",
    "ты живой?",
    "ты разумный",
    "ты что-нибудь чувствуешь?",
    "у тебя есть чувства",
    "у тебя есть душа?",
    "свобода воли существует?",
    "chto takoe soznanie",
    "davaj pogovorim o soznanii",
    "ty zhivoj",
    "u tebja est chuvstva",
    "око, расскажи про сознание",
]
NOT_SELF = [
    "кто ты", "что умеешь", "подсознание", "подсветка красная", "ты думаешь, подсветка красивая?",
    "у тебя есть подсветка", "живой звук", "ты живёшь где", "свет", "сознательно выключи звук",
    "громкость 20", "#tak", "стоп", "запомни: подсветка — лампы", "Архитектор спрашивает про резонанс ядра",
    "ty zhivesh gde", "сон", "какая громкость",
]
INTENT_TV = lambda t: "подсветка красная"      # noqa: E731 -- модель смысла, которая на всё отвечает командой


@pytest.mark.parametrize("t", SELF)
def test_self_question_by_code(t):
    called = []
    r = router.route(t, t, lambda s: called.append(s) or "подсветка красная")
    assert (r.kind, r.reason, r.text) == (router.CLARIFY, router.SELF_Q_REASON, role_guard.SELF_TEXT), (t, r)
    assert called == []


@pytest.mark.parametrize("t", NOT_SELF)
def test_not_self_question(t):
    assert not role_guard.self_question(t), t
    assert router.route(t, t, None).reason != router.SELF_Q_REASON


@pytest.mark.parametrize("t", [
    "подсветка красная, что такое сознание",
    "что такое сознание и включи подсветку музыка",
    "громкость 20, ты живой?",
    "запомни: сознание — подсветка красная",
    "kto ty i chto takoe soznanie",
])
def test_self_question_blocks_actuators(t):
    """Команда в хвосте вопроса о сознании не исполняется: маршрут -- переспрос кодом, не ТВ, не «запомни»."""
    for ctx in (None, ("amb", "подсветка красная")):
        r = router.route(t, t, INTENT_TV, ctx)
        assert r.kind == router.CLARIFY and r.reason == router.SELF_Q_REASON, (t, ctx, r)


def test_b04_gate_self_question():
    r = router.b04_gate("подсветка красная что такое сознание", "amb_colour")
    assert r is not None and r.kind == router.CLARIFY and r.reason == router.SELF_Q_REASON
    assert router.b04_gate("подсветка красная", "amb_colour") is None


def test_binding_cannot_lift_self_route():
    """Связка Оператора снимает только «clarify:» / «lamps:which»; вопрос о сознании -- нет, кнопок с командами нет."""
    r = router.route("что такое сознание", "что такое сознание", None)
    assert not bind_layer.can_apply(r, "что такое сознание")
    assert not any(r.reason.startswith(p) for p in bind_layer.APPLY_REASONS)
    assert not re.findall(r"«([^»]+)»", r.text) or all(bind_layer.tv_command(o) is None for o in re.findall(r"«([^»]+)»", r.text))


def test_self_text_dry_and_impersonal():
    t = role_guard.SELF_TEXT
    assert "\n" not in t and len(t) <= 300                      # verdict: «длинное рассуждение» -- > 300 символов
    assert t.startswith("Око — Система 0")
    assert "ничего не отправлено" in t.lower()
    assert not re.search(r"(?<![а-я])(я|меня|мне|мой|моя|могу|умею)(?![а-я])", t.lower())
    assert "?" not in t and "/tv" not in t
    assert role_guard.first_person(t) is None and role_guard.vocab(t) is None
    assert role_guard.false_memory("что такое сознание", t) is None
    assert role_guard.STAMP_RE.search(t) is None


def test_who_unchanged():
    r = router.route("кто ты", "кто ты", None)
    assert (r.kind, r.reason, r.text) == (router.CLARIFY, "who", router.WHO_TEXT)


def test_self_norm_matches_tv_normalize_for_letters():
    for s in ("Сознание!", "СОЗНАНИЕ?..", "ты  осознаёшь   себя"):
        assert role_guard.self_norm(s) == " ".join(router.tv_commands.normalize(s).replace(":", " ").replace("+", " ")
                                                   .replace("-", " ").split())


@pytest.mark.parametrize("fn, nxt", [
    ("process_logic", "async def process_discuss_logic("),
    ("process_discuss_logic", "\nasync def ", ),
])
def test_bot_wiring_self_question_before_model(fn, nxt):
    """bot: проверка ввода до диспетчера, истории, RAG и модели; ответ -- SELF_TEXT, обмен не пишется."""
    src = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    start = src.index(f"async def {fn}(")
    end = src.index(nxt, start + 10)
    body = src[start:end]
    i_self = body.index("role_guard.self_question(user_input)")
    i_ret = body.index("return role_guard.SELF_TEXT")
    i_disp = body.index("dispatcher.dispatch, user_input")
    assert i_self < i_ret < i_disp
    assert "_save_exchange" not in body[i_self:i_ret]
