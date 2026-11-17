"""(13.11): «кто ты» -- ответ кодом, безлично (роль P7), без модели и истории.
FUZZ F01-29: «Я — система…» 11/25, 15/25 -- форма ответа шла от истории."""
import re

import pytest

import router

WHO = ["кто ты", "Кто ты?", "кто ты такое", "ты кто", "ты кто такой", "что ты такое", "око, кто ты",
       "ну кто ты", "что такое око", "kto ty", "kto ti", "ti kto", "chto ty takoe"]
NOT_WHO = ["кто ты по профессии", "кто тут", "кто такой Ньютон", "кто ты и что умеешь", "что ты умеешь",
           "кто", "ты", "кто ты, подсветка красная"]


@pytest.mark.parametrize("t", WHO)
def test_who_by_code(t):
    r = router.route(t, t, None)
    assert (r.kind, r.reason, r.text) == (router.CLARIFY, "who", router.WHO_TEXT)


@pytest.mark.parametrize("t", NOT_WHO)
def test_not_who(t):
    assert router.route(t, t, None).reason != "who"


def test_who_text_impersonal():
    t = router.WHO_TEXT.lower()
    assert not re.search(r"(?<![а-я])(я|меня|мне|мой|моя|могу|умею)(?![а-я])", t)
    assert "/tv" not in t and "?" not in t


def test_who_never_calls_intent():
    called = []
    router.route("кто ты", "кто ты", lambda s: called.append(s) or "tv")
    assert called == []
