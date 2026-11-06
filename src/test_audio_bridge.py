"""test_audio_bridge.py — арифметика моста «уровень на ПК -> подсветка».

Устройства и сеть здесь не участвуют: проверяется то, из-за чего мост
либо завалит телевизор запросами, либо замрёт, либо будет дёргаться на
каждом слоге.
"""
from __future__ import annotations

import math

import pytest


@pytest.fixture
def ab():
    import audio_bridge as mod
    return mod


# --------------------------------------------------------------------------
# dBFS -> уровень
# --------------------------------------------------------------------------

def test_silence_is_zero(ab):
    assert ab.level_from_dbfs(-math.inf) == 0.0


@pytest.mark.parametrize(("dbfs", "expected"), [
    (-48.0, 0.0), (-30.0, 0.5), (-12.0, 1.0),
])
def test_linear_in_decibels(ab, dbfs, expected):
    """Шкала линейна по ДЕЦИБЕЛАМ: -30 dBFS должно давать ровно середину.

    По амплитуде вся музыка сжалась бы у нуля — разница между -40 и -20
    dBFS это 0.01 и 0.1, глазом не различить.
    """
    assert ab.level_from_dbfs(dbfs) == pytest.approx(expected, abs=0.001)


@pytest.mark.parametrize("dbfs", [-120.0, -60.0, -48.1])
def test_below_floor_clamps_to_zero(ab, dbfs):
    assert ab.level_from_dbfs(dbfs) == 0.0


@pytest.mark.parametrize("dbfs", [-11.9, -3.0, 0.0, 6.0])
def test_above_ceil_clamps_to_one(ab, dbfs):
    assert ab.level_from_dbfs(dbfs) == 1.0


def test_monotonic(ab):
    prev = -1.0
    for d in range(-60, 1):
        cur = ab.level_from_dbfs(float(d))
        assert cur >= prev, f"падение на {d} dBFS"
        prev = cur


def test_custom_range(ab):
    assert ab.level_from_dbfs(-20.0, floor=-40.0, ceil=0.0) == pytest.approx(0.5)


@pytest.mark.parametrize(("floor", "ceil"), [(-12.0, -48.0), (-20.0, -20.0)])
def test_inverted_range_rejected(ab, floor, ceil):
    with pytest.raises(ValueError):
        ab.level_from_dbfs(-30.0, floor=floor, ceil=ceil)


# --------------------------------------------------------------------------
# Сглаживание
# --------------------------------------------------------------------------

def test_attack_faster_than_release(ab):
    """Атака быстрая, чтобы удар был виден; спад медленный, чтобы между
    словами не проваливалось в темноту."""
    assert ab.ATTACK > ab.RELEASE


def test_rise_uses_attack(ab):
    s = ab.Smoother(attack=0.5, release=0.1, value=0.0)
    assert s.push(1.0) == pytest.approx(0.5)
    assert s.push(1.0) == pytest.approx(0.75)


def test_fall_uses_release(ab):
    s = ab.Smoother(attack=0.5, release=0.1, value=1.0)
    assert s.push(0.0) == pytest.approx(0.9)
    assert s.push(0.0) == pytest.approx(0.81)


def test_converges_to_target(ab):
    s = ab.Smoother(attack=0.5, release=0.5, value=0.0)
    for _ in range(60):
        s.push(0.7)
    assert s.value == pytest.approx(0.7, abs=0.001)


def test_smoother_never_overshoots(ab):
    s = ab.Smoother(attack=1.0, release=1.0, value=0.0)
    assert s.push(0.4) == pytest.approx(0.4)
    s2 = ab.Smoother(attack=0.9, release=0.9, value=0.0)
    for _ in range(10):
        v = s2.push(1.0)
        assert v <= 1.0


@pytest.mark.parametrize(("attack", "release"), [
    (0.0, 0.1), (1.1, 0.1), (0.5, 0.0), (0.5, -0.2),
])
def test_bad_coefficients_rejected(ab, attack, release):
    with pytest.raises(ValueError):
        ab.Smoother(attack=attack, release=release)


# --------------------------------------------------------------------------
# Уровень -> цвет
# --------------------------------------------------------------------------

def test_zero_level_still_glows(ab):
    """Яркость 0 даёт мгновенное погасание, и на паузах между фразами
    подсветка мигала бы. Тихо — это слабое свечение, а не выключено."""
    _, _, b = ab.level_to_color(0.0)
    assert b > 0


def test_full_level_is_max_brightness(ab):
    _, _, b = ab.level_to_color(1.0)
    assert b == 255


def test_brightness_monotonic(ab):
    prev = -1
    for i in range(0, 101):
        _, _, b = ab.level_to_color(i / 100)
        assert b >= prev
        prev = b


@pytest.mark.parametrize("level", [-1.0, -0.001, 1.001, 5.0])
def test_level_clamped(ab, level):
    _, _, b = ab.level_to_color(level)
    assert 0 < b <= 255


@pytest.mark.parametrize(("hue", "expected"), [
    (0, 0), (170, 170), (255, 255), (256, 0), (300, 44), (-1, 255),
])
def test_hue_wraps_into_byte(ab, hue, expected):
    """hue вне 0..255 телевизор отверг бы, а set_lounge_color проверяет
    диапазон и бросит ValueError — мост обязан свернуть значение сам."""
    h, _, _ = ab.level_to_color(0.5, hue=hue)
    assert h == expected
    assert 0 <= h <= 255


def test_saturation_constant(ab):
    for level in (0.0, 0.3, 1.0):
        _, s, _ = ab.level_to_color(level)
        assert s == ab.SAT


# --------------------------------------------------------------------------
# Темп отправки
# --------------------------------------------------------------------------

def test_first_send_always_allowed(ab):
    p = ab.Pacer()
    assert p.should_send(0.0, now=0.0) is True


def test_too_soon_is_skipped(ab):
    p = ab.Pacer(min_interval_s=0.2, min_delta=0.01)
    assert p.should_send(0.5, 10.0) is True
    p.mark(0.5, 10.0)
    assert p.should_send(0.9, 10.05) is False
    assert p.should_send(0.9, 10.25) is True


def test_small_change_is_skipped(ab):
    """Телевизор не отличит, а запрос стоит столько же, сколько
    осмысленный, и копит задержку."""
    p = ab.Pacer(min_interval_s=0.1, min_delta=0.05)
    p.mark(0.50, 0.0)
    assert p.should_send(0.52, 1.0) is False
    assert p.should_send(0.56, 1.0) is True


def test_change_downward_also_counts(ab):
    p = ab.Pacer(min_interval_s=0.1, min_delta=0.05)
    p.mark(0.50, 0.0)
    assert p.should_send(0.40, 1.0) is True


def test_rate_ceiling_is_honest(ab):
    """Потолок частоты не должен обещать больше, чем выдержит HTTPS с
    digest: один POST — это десятки миллисекунд."""
    assert ab.MIN_INTERVAL_S >= 0.1
    assert 1.0 / ab.MIN_INTERVAL_S <= 10.0


def test_pacer_simulation_does_not_flood(ab):
    """Прогон ровного сигнала: при неподвижном уровне отправок почти не
    должно быть — иначе мост льёт запросы в телевизор без причины."""
    p = ab.Pacer(min_interval_s=0.14, min_delta=0.03)
    sends = 0
    for i in range(1000):          # 10 секунд по 10 мс
        now = i * 0.01
        if p.should_send(0.5, now):
            sends += 1
            p.mark(0.5, now)
    assert sends == 1, f"отправок {sends}, ожидалась одна"


def test_pacer_follows_changing_signal(ab):
    """На меняющемся сигнале отправки идут, но не чаще потолка."""
    p = ab.Pacer(min_interval_s=0.14, min_delta=0.03)
    sends = 0
    for i in range(1000):
        now = i * 0.01
        level = (i % 100) / 100.0
        if p.should_send(level, now):
            sends += 1
            p.mark(level, now)
    assert 0 < sends <= 10 / 0.14 + 1, f"отправок {sends}"


# --------------------------------------------------------------------------
# Связность с клиентом телевизора
# --------------------------------------------------------------------------

def test_color_is_acceptable_for_tv_client(ab, monkeypatch):
    """Любой выход level_to_color должен проходить проверку диапазонов в
    set_lounge_color, иначе мост будет падать на ValueError в рантайме."""
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake_token_for_tests")
    import tv_control

    class Rec:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kw):
            self.calls.append(kw.get("json"))
            class R:
                status_code = 200

                def json(self_inner):
                    return {}
            return R()

    rec = Rec()
    c = tv_control.TVClient("h", "u", "p", session=rec)
    for level in (0.0, 0.01, 0.5, 0.99, 1.0, -5.0, 9.0):
        for hue in (0, 128, 255, 300, -7):
            h, s, b = ab.level_to_color(level, hue=hue)
            c.set_lounge_color(h, s, b)
    assert len(rec.calls) == 35


# --------------------------------------------------------------------------
# 8. Раскладка по светодиодам (режим expert)
# --------------------------------------------------------------------------

TOPOLOGY = {"layers": 1, "left": 3, "top": 7, "right": 3, "bottom": 0}


def test_hue_to_rgb_primaries(ab):
    """Оттенок 0 — красный, 85 — зелёный, 170 — синий (шкала 0..255).

    Небольшая примесь соседнего канала допустима: 256 оттенков не делятся
    на шесть секторов нацело, поэтому на границах остаётся несколько
    единиц. Требовать ровный ноль значило бы проверять округление, а не
    цвет.
    """
    assert ab.hue_to_rgb(0) == (255, 0, 0)
    r, g, b = ab.hue_to_rgb(85)
    assert g > 240 and r < 10 and b < 10, (r, g, b)
    r, g, b = ab.hue_to_rgb(170)
    assert b > 240 and r < 10 and g < 10, (r, g, b)
    r, g, b = ab.hue_to_rgb(128)
    assert g > 200 and b > 200 and r < 10, (r, g, b)


def test_hue_wraps_and_value_scales(ab):
    assert ab.hue_to_rgb(256) == ab.hue_to_rgb(0)
    assert ab.hue_to_rgb(0, 0.5) == (128, 0, 0)
    assert ab.hue_to_rgb(0, 0.0) == (0, 0, 0)


def test_hue_rgb_always_in_byte_range(ab):
    for hue in range(0, 256, 7):
        for value in (0.0, 0.3, 1.0, 5.0, -1.0):
            for c in ab.hue_to_rgb(hue, value):
                assert 0 <= c <= 255


def test_bar_partial_pixel_is_dimmed(ab):
    """На трёх точках ступенчатая шкала дала бы три положения. Частично
    попавший светодиод должен гореть пропорционально."""
    bar = ab.bar_pixels(0.5, 3, hue=0)
    assert bar["0"] == {"r": 255, "g": 0, "b": 0}
    assert 0 < bar["1"]["r"] < 255
    assert bar["2"] == {"r": 0, "g": 0, "b": 0}


def test_bar_full_and_empty(ab):
    full = ab.bar_pixels(1.0, 7, hue=0)
    assert all(px["r"] == 255 for px in full.values())
    empty = ab.bar_pixels(0.0, 7, hue=0)
    assert all(px == {"r": 0, "g": 0, "b": 0} for px in empty.values())


def test_bar_keys_are_strings_and_complete(ab):
    bar = ab.bar_pixels(0.4, 7, hue=100)
    assert set(bar) == {str(i) for i in range(7)}


def test_render_channels_go_to_their_sides(ab):
    """Левый канал — в левый столбец, правый — в правый. Иначе картинка
    врёт о панораме."""
    out = ab.render_pixels(1.0, 0.0, TOPOLOGY, hue=0)
    assert all(out["left"][str(i)]["r"] == 255 for i in range(3))
    assert all(out["right"][str(i)] == {"r": 0, "g": 0, "b": 0} for i in range(3))


def test_render_top_shows_overall(ab):
    out = ab.render_pixels(0.0, 1.0, TOPOLOGY, hue=0)
    lit = sum(1 for px in out["top"].values() if px["r"] > 0)
    assert lit == 7, "верх показывает максимум по каналам"


def test_render_skips_side_without_leds(ab):
    """`bottom: 0` — отправка такой стороны бессмысленна, и клиент на неё
    отвечает ошибкой."""
    out = ab.render_pixels(0.5, 0.5, TOPOLOGY, hue=0)
    assert "bottom" not in out
    assert set(out) == {"left", "right", "top"}


def test_render_clamps_levels(ab):
    out = ab.render_pixels(5.0, -3.0, TOPOLOGY, hue=0)
    assert all(out["left"][str(i)]["r"] == 255 for i in range(3))
    assert all(out["right"][str(i)]["r"] == 0 for i in range(3))


def test_render_output_accepted_by_tv_client(ab, monkeypatch):
    """Всё, что рисует render_pixels, должно проходить проверки
    set_pixels: иначе мост падал бы в рантайме на каждом кадре."""
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake_token_for_tests")
    import tv_control

    class Rec:
        def __init__(self):
            self.bodies = []

        def request(self, method, url, **kw):
            body = kw.get("json")
            self.bodies.append((method, url, body))

            class R:
                status_code = 200

                def json(self_inner):
                    return TOPOLOGY
            return R()

    rec = Rec()
    c = tv_control.TVClient("h", "u", "p", session=rec)
    for ll in (0.0, 0.33, 1.0):
        for rr in (0.0, 0.7, 1.0):
            c.set_pixels(ab.render_pixels(ll, rr, TOPOLOGY, hue=170))
    posts = [b for m, _, b in rec.bodies if m == "POST"]
    assert len(posts) == 9
    for body in posts:
        layer = body["layer1"]
        assert set(layer) == {"left", "right", "top"}
        assert set(layer["top"]) == {str(i) for i in range(7)}
