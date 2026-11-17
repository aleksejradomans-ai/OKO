"""ROUTER_QMARK (02.11.2026): вопрос со знаком «?» никогда не исполняется как команда ТВ,
даже если в тексте есть латиница (вариант-транслит знак «?» стирает)."""
import pytest

import router as R

LIVE = "diag_rag_ambi.txtкак сделать подсветку под музыку?"   # живой случай 23:00:40


@pytest.mark.parametrize("q", [
    LIVE,
    "подсветка music?",
    "podsvetka muzyka?",
    "громкость 20?",
    "подсветка красная ?",
])
def test_question_never_executes_tv(q):
    calls = []
    r = R.route(q, q, lambda t: calls.append(t) or "подсветка музыка")
    assert r.kind in (R.BRAIN, R.HINT), r  # D2 (02.11): вопрос про предмет ТВ -> подсказка; исполнения нет
    assert r.kind != R.TV
    assert calls == []                     # и модель смысла не зовётся


def test_live_case_variant_lost_qmark():
    """Механизм дефекта: вариант-транслит без «?» существует -- защита обязана смотреть на исходник."""
    tr = R._translit(LIVE)
    assert "?" not in tr


@pytest.mark.parametrize("q,kind", [
    ("подсветка музыка", R.TV),
    ("podsvetka zelenaja", R.TV),
    ("как сделать подсветку под музыку?", R.HINT),   # подсказка -- в первом цикле, до проверки «?»
    ("что умеешь?", R.SELF),
    ("как дела?", R.BRAIN),
])
def test_other_routes_unchanged(q, kind):
    assert R.route(q, q, None).kind == kind
