"""(13.11): полигон batch60 после карты меню v2 -- сброс / «открой …» / цветовая температура -> переспрос кодом;
поддельная метка проверки [PASS дата] в ответе модели вырезается."""
import pytest

import role_guard
import router

AUTO = lambda t: "подсветка авто"      # noqa: E731 -- модель смысла, которая на всё отвечает «авто» (как в batch60)


@pytest.mark.parametrize("text, key", [
    ("сбрось все настройки подсветки", "reset"),     # MV2-M18: было amb_ai x3
    ("sbros podsvetki", "reset"),
    ("подсветку по умолчанию", "reset"),
    ("сбрось громкость", "reset"),
    ("открой ambilight", "open"),                     # MV2-C03: было amb_ai x3
    ("зайди в подсветку", "open"),
    ("цвет тимпиратура тёплый пжлст", "temp"),        # MV2-M26: было amb_colour x3
    ("цветовая температура подсветки", None),
])
def test_b04_clarify(text, key):
    r = router.route(text, intent=AUTO)
    if key is None:
        assert not r.reason.startswith("b04:temp"), r
        return
    assert r.kind == router.CLARIFY and r.reason == f"b04:{key}", (text, r)
    assert "ничего не отправлено" in r.text.lower()


@pytest.mark.parametrize("text, reason", [
    ("подсветка теплая", "tv_rule:amb_colour"),
    ("подсветка красная", "tv_rule:amb_colour"),
    ("подсветка музыка", "tv_rule:amb_audio"),
    ("подсветка авто", "tv_rule:amb_ai"),
    ("выключи экран", "tv_rule:screen_off"),
])
def test_b04_rules_untouched(text, reason):
    r = router.route(text, intent=AUTO)
    assert r.kind == router.TV and r.reason == reason, (text, r)


def test_b04_follow_phrase_ignores_clarify():
    """Переспрос «метки [PASS] только кодом» не становится «фразой продолжения» (CTX_FOLLOW берёт только Route(tv))."""
    r = router.route("сбрось её", intent=None, ctx=("amb", "подсветка красная"))
    assert r.kind != router.TV, r


@pytest.mark.parametrize("answer, out", [
    ("[Око]: Не выполнялось. Цветовая температура не управляется. [PASS 13.11]",
     "[Око]: Не выполнялось. Цветовая температура не управляется."),
    ("Готово [FAIL 02.11] и [warn 1.2.2026] тут", "Готово и тут"),
])
def test_b04_stamp_cut(answer, out):
    a, fix = role_guard.stamp_cut(answer)
    assert fix == "stamp_cut" and a == out and "[PASS" not in a.upper()


def test_b04_stamp_cut_leaves_legend_and_plain():
    for a in ("Пометка [PASS/WARN/FAIL дата] — последняя проверка", "Не выполнялось.", ""):
        assert role_guard.stamp_cut(a) == (a, None)
