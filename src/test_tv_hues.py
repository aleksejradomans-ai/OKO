"""TV_HUES_FORMS (02.11.2026): цвет подсветки узнаётся правилами во всех родах и в транслите --
в игровом режиме модель смысла (tv_intent) отключена, иначе команда уходит в мозг и получает заглушку."""
import pytest

import router
import tv_commands as c

COLOURS = {
    "красн": 0, "оранжев": 20, "желт": 40, "зелен": 85, "бирюзов": 128, "голуб": 150, "син": 170,
    "фиолетов": 200, "сиренев": 210, "розов": 230, "бел": 0, "тепл": 25, "холодн": 160,
}


def test_every_colour_has_four_forms():
    for stem, hue in COLOURS.items():
        forms = [w for w in c.HUES if w.startswith(stem)]
        assert len(forms) == 4, (stem, forms)
        assert {c.HUES[w] for w in forms} == {hue}


@pytest.mark.parametrize("phrase,hue", [
    ("подсветка желтая", 40), ("подсветка жёлтая", 40), ("подсветка желтый", 40), ("подсветку желтую", 40),
    ("подсветка синяя", 170), ("подсветка синее", 170), ("подсветка оранжевая", 20),
    ("подсветка голубая", 150), ("подсветка белая", 0), ("подсветка теплая", 25),
    ("подсветка холодная", 160), ("подсветка розовая", 230),
])
def test_parse_feminine_and_other_forms(phrase, hue):
    cmd = c.parse(phrase)
    assert cmd.name == "amb_colour"
    assert cmd.meta["hue"] == hue


def test_old_keys_kept():
    for w in ("красный", "красную", "красная", "желтый", "желтую", "зеленый", "зеленую", "зеленая",
              "синий", "синюю", "синяя", "белый", "теплый", "холодный", "голубой"):
        assert w in c.HUES


@pytest.mark.parametrize("phrase", ["подсветка белая", "подсветка белый", "подсветку белую"])
def test_white_any_form_zero_saturation(phrase):
    cmd = c.parse(phrase)
    calls = []

    class TV:
        def set_ambilight_power(self, on): pass
        def set_ambilight_style(self, *a): pass
        def set_lounge_color(self, hue, sat, bri): calls.append((hue, sat, bri))
        def get_lounge_color(self): return {"color": {"hue": 0, "saturation": 0, "brightness": 255}}

    import os
    os.environ["TV_READBACK_DELAY_S"] = "0"
    try:
        cmd.run(TV())
    finally:
        os.environ.pop("TV_READBACK_DELAY_S", None)
    assert calls and calls[0][1] == 0


@pytest.mark.parametrize("raw", ["podsvetka zeltaja", "podsvetka zheltaja", "podsvetka zelenaja",
                                 "podsvetka krasnaja", "podsvetka sinjaja", "podsvetka belaja"])
def test_translit_routes_to_tv_without_intent(raw):
    """Живой случай 02.11 15:10: «podsvetka zeltaja» -> brain. В игре intent=None."""
    r = router.route(raw, raw, None)
    assert r.kind == router.TV, (raw, router._translit(raw))
    assert r.reason == "tv_rule:amb_colour"
