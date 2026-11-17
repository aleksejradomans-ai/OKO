"""MODE_TEXT (03.11.2026): режим подсветки словами в ответе модуля ТВ.

Живой случай 00:35: после «подсветка зелёная» факт «Подсветка: цвет зеленая, hue 85…» режима не называл;
на «в каком режиме телевизор» мозг (layers=0, kn=31) ответил «режим подсветки музыки» (Оператор: на ТВ статичный цвет).
"""
import pytest

import router
import tv_commands as c


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")


class TV:
    def __init__(self, style="FOLLOW_AUDIO", setting="VU_METER", power="On"):
        self.style, self.setting, self.power = style, setting, power
        self.lounge = {"hue": 85, "saturation": 255, "brightness": 255}

    def set_ambilight_power(self, on): self.power = "On" if on else "Off"
    def set_ambilight_style(self, name, setting): self.style, self.setting = name, setting
    def set_lounge_color(self, hue, sat, bri): self.lounge = {"hue": hue, "saturation": sat, "brightness": bri}
    def get_ambilight_power(self): return {"power": self.power}
    def get_ambilight_config(self): return {"styleName": self.style, "menuSetting": self.setting}
    def get_lounge_color(self): return {"color": dict(self.lounge)}


def test_colour_names_static_mode():
    out = c.parse("подсветка зеленая").run(TV())
    assert out.startswith("Подсветка: режим один цвет (статичный), цвет зелен")
    assert "hue 85" in out and "музык" not in out


def test_audio_names_mode():
    out = c.parse("подсветка музыка").run(TV())
    assert out.startswith("Подсветка: режим под музыку, FOLLOW_AUDIO")


def test_video_names_mode():
    tv = TV()
    out = c.parse("подсветка кино").run(tv)
    assert out.startswith("Подсветка: режим за картинкой, FOLLOW_VIDEO")


def test_off_names_mode():
    out = c.parse("подсветка выключи").run(TV())
    assert out == "Подсветка: выключена (цвет погашен: яркость 0)"   # 03.11 проба: power Off ламп не гасит


def test_off_not_confirmed_is_reported():
    class Stuck(TV):
        def set_lounge_color(self, hue, sat, bri): pass      # телевизор не принял яркость 0
    out = c.parse("подсветка выключи").run(Stuck())
    assert "запрошено погасить" in out and "255" in out
    assert router.tv_outcome(out) is None or "выключена" not in out


def test_on_names_state():
    out = c.parse("подсветка включи").run(TV(power="Off"))
    assert out == "Подсветка: включена (On)"


def test_mismatch_does_not_claim_mode():
    """Сверка не сошлась -- режим не называется установленным."""
    class Stale(TV):
        def set_ambilight_style(self, name, setting): pass
    out = c.parse("подсветка музыка").run(Stale(style="FOLLOW_VIDEO", setting="NATURAL"))
    assert "запрошено" in out and "режим под музыку" not in out


@pytest.mark.parametrize("phrase", ["подсветка зеленая", "подсветка музыка", "подсветка кино", "подсветка выключи"])
def test_router_outcome_and_dguard_signature_still_work(phrase):
    out = c.parse(phrase).run(TV())
    assert router.tv_outcome(out) == "PASS"
    assert router.fake_done("подсветка красная", out)          # подпись модуля в ответе мозга -- по-прежнему подделка
