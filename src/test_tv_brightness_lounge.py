"""B20 (20.11, Оператор 00:59 [1]): яркость подсветки по ТЕКУЩЕМУ режиму (tv_commands._amb_bri / _amb_bri_get).

Живой случай 19.11 23:43: после «подсветка зелёная» (FOLLOW_COLOR, цвет через ambilight/lounge) «яркость N» писала
ползунок меню 710 и читала его же (PASS), а лампы не менялись. Теперь:
  режим цвета (FOLLOW_COLOR)            -> set_lounge_color(тот же оттенок, та же насыщенность, новая яркость), сверка
                                           get_lounge_color.brightness; шкала речи 0..10 <-> lounge 0..255;
  «за картинкой» / «под музыку»         -> как раньше, ползунок 710 (тесты test_tv_commands_ambi без изменений);
  режим не прочитан / неизвестен        -> отказ «нет данных о режиме», ничего не отправлено;
  вне 0..10 в режиме цвета              -> отказ, ничего не отправлено; шаг у края -- «уже предел».
Сеть не затрагивается: двойник ТВ в памяти, модель смысла заглушена.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sigma_gate as G
import tv_bot_handler as H
import tv_commands as C
import tv_intent

ADMIN = 4545
_UNSET = object()


class AmbTV:
    """Двойник подсветки: стиль, цвет lounge, ползунок 710. accept=False -- ТВ не принимает яркость цвета;
    drift -- ТВ меняет оттенок при записи; config -- подменённый ответ currentconfiguration."""

    def __init__(self, style="FOLLOW_COLOR", hue=85, sat=255, bri=255, b710=4, accept=True, drift=0, config=_UNSET):
        self.style, self.setting_ = style, "MANUAL_HUE"
        self.lounge = {"hue": hue, "saturation": sat, "brightness": bri}
        self.setting = {710: b710, 720: 0}
        self.accept, self.drift, self.config = accept, drift, config
        self.calls: list = []

    def get_ambilight_config(self):
        self.calls.append(("get_ambilight_config",))
        if self.config is not _UNSET:
            return self.config
        return {"styleName": self.style, "menuSetting": self.setting_}

    def set_ambilight_power(self, on):
        self.calls.append(("set_ambilight_power", on))

    def set_ambilight_style(self, style, setting=None):
        self.calls.append(("set_ambilight_style", style, setting))
        self.style, self.setting_ = style, setting

    def get_lounge_color(self):
        self.calls.append(("get_lounge_color",))
        return {"color": dict(self.lounge)}

    def set_lounge_color(self, hue, sat, bri):
        self.calls.append(("set_lounge_color", hue, sat, bri))
        if self.accept:
            self.lounge = {"hue": (int(hue) + self.drift) % 256, "saturation": int(sat), "brightness": int(bri)}

    def get_setting_value(self, node):
        self.calls.append(("get_setting_value", node))
        return self.setting[node]

    def set_setting_value(self, node, value, lo, hi):
        v = max(lo, min(hi, int(value)))
        self.calls.append(("set_setting_value", node, v))
        self.setting[node] = v
        return v

    def writes(self):
        return [c for c in self.calls if c[0].startswith("set_")]


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "SHADOW_DEFAULT_PATH", tmp_path / "default" / "sigma_shadow.jsonl")
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(tmp_path / "j" / "sigma_shadow.jsonl"))
    monkeypatch.setenv(H.SHADOW_FLAG_ENV, "0")
    monkeypatch.delenv(G.LIVE_ENV, raising=False)
    monkeypatch.delenv(G.STATE_ENV, raising=False)
    monkeypatch.delenv(G.PARAMS_ENV, raising=False)
    monkeypatch.delenv("OKO_COLLECTOR", raising=False)
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")
    monkeypatch.setenv("TV_SETTING_SETTLE_S", "0")
    monkeypatch.setattr(tv_intent, "understand", lambda *a, **k: None)
    monkeypatch.setattr(H, "VERIFY", {})
    monkeypatch.setattr(H, "PRE", {})
    monkeypatch.setattr(H, "_stop_music", lambda name: "")
    monkeypatch.setattr(C, "_settle", lambda: None)
    H._SHADOW_WARNED.clear()
    G.reset()
    yield
    G.reset()


def run(phrase, tv):
    return C.parse(phrase).run(tv)


def say(tv, text, t=0.0, signs=None):
    return H.handle(ADMIN, text, lambda: tv, pending=H.PendingStore(), admin_ids=[ADMIN], now=t,
                    signs=signs if signs is not None else H.SignSessions())


# --- шкала ------------------------------------------------------------------------------------------------------
def test_scale_ends_and_roundtrip():
    assert C.level_to_lounge(0) == 0 and C.level_to_lounge(10) == 255 and C.lounge_to_level(255) == 10
    assert [C.lounge_to_level(C.level_to_lounge(n)) for n in range(11)] == list(range(11))
    vals = [C.level_to_lounge(n) for n in range(11)]
    assert vals == sorted(vals) and len(set(vals)) == 11
    assert C.lounge_to_level(100) == 4                         # «подсветка красная 100» -> «яркость 4 из 10»


# --- режим цвета ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("phrase,level", [("яркость 3", 3), ("подсветка 7", 7), ("яркость подсветки 0", 0),
                                          ("яркость на максимум", 10), ("яркость на минимум", 0)])
def test_colour_mode_sets_lounge_brightness_keeps_hue(phrase, level):
    tv = AmbTV(hue=85, sat=200, bri=C.level_to_lounge(5))
    out = run(phrase, tv)
    raw = C.level_to_lounge(level)
    assert ("set_lounge_color", 85, 200, raw) in tv.calls and tv.lounge == {"hue": 85, "saturation": 200, "brightness": raw}
    assert not [c for c in tv.calls if c[0] == "set_setting_value"]           # меню-яркость 710 не трогается
    assert not [c for c in tv.calls if c[0] == "set_ambilight_style"]         # режим не меняется
    assert out == f"Подсветка: режим один цвет (статичный), яркость {level} из 10"


@pytest.mark.parametrize("phrase,start,level", [("подсветка ярче", 4, 5), ("подсветка ярче на 3", 4, 7),
                                                ("подсветка тусклее", 4, 3), ("яркость плюс 2", 4, 6),
                                                ("яркость минус 3", 4, 1), ("яркость повыше", 4, 5),
                                                ("яркость пониже", 4, 3)])
def test_colour_mode_steps(phrase, start, level):
    tv = AmbTV(bri=C.level_to_lounge(start))
    out = run(phrase, tv)
    assert tv.lounge["brightness"] == C.level_to_lounge(level) and tv.lounge["hue"] == 85
    assert out == f"Подсветка: режим один цвет (статичный), яркость {level} из 10"


def test_colour_mode_at_limit_no_write():
    tv = AmbTV(bri=255)
    out = run("подсветка ярче", tv)
    assert tv.writes() == [] and out == "Подсветка: режим один цвет (статичный), яркость 10 из 10 — уже предел"


def test_colour_mode_step_over_edge_clamped_and_said():
    tv = AmbTV(bri=C.level_to_lounge(9))
    out = run("яркость плюс 3", tv)
    assert tv.lounge["brightness"] == 255 and "просили 12, предел 0…10" in out


def test_colour_mode_same_value_no_write():
    tv = AmbTV(bri=C.level_to_lounge(4))
    assert run("яркость 4", tv) == "Подсветка: режим один цвет (статичный), яркость 4 из 10" and tv.writes() == []


@pytest.mark.parametrize("phrase", ["яркость 15", "подсветка 11"])
def test_colour_mode_out_of_range_refused_nothing_sent(phrase):
    tv = AmbTV()
    with pytest.raises(ValueError, match="вне диапазона 0…10"):
        run(phrase, tv)
    assert tv.writes() == []


def test_colour_mode_readback_fail_when_tv_does_not_take_it():
    tv = AmbTV(bri=255, accept=False)
    out = run("яркость 3", tv)
    assert out.startswith("Подсветка: запрошено яркость 3 из 10") and "телевизор сообщает яркость цвета 255" in out


def test_colour_mode_readback_fail_when_hue_drifts():
    tv = AmbTV(hue=85, drift=7)
    out = run("яркость 3", tv)
    assert "телевизор сообщает hue 92" in out and "запрошено" in out


def test_colour_mode_reads_brightness():
    tv = AmbTV(bri=C.level_to_lounge(6))
    assert run("какая яркость", tv) == "Подсветка: режим один цвет (статичный), яркость 6 из 10" and tv.writes() == []


# --- режим по видео / по музыке: прежний путь (ползунок 710) -------------------------------------------------------
@pytest.mark.parametrize("style", ["FOLLOW_VIDEO", "FOLLOW_AUDIO"])
def test_setting_modes_keep_menu_slider(style):
    tv = AmbTV(style=style, b710=4)
    assert run("яркость 7", tv) == "Подсветка: яркость 7 из 10" and tv.setting[710] == 7
    assert not [c for c in tv.calls if c[0] == "set_lounge_color"]
    assert run("какая яркость", tv) == "Подсветка: яркость 7 из 10"


# --- режим неизвестен -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("config", [{}, None, {"styleName": None}, {"styleName": "EXPERT"}, "мусор"])
def test_unknown_mode_no_data_nothing_sent(config):
    tv = AmbTV(config=config)
    for phrase in ("яркость 3", "подсветка ярче", "яркость плюс 1"):
        with pytest.raises(ValueError, match="нет данных о режиме"):
            run(phrase, tv)
    with pytest.raises(ValueError, match="нет данных о режиме"):
        run("какая яркость", tv)
    assert tv.writes() == []


def test_colour_mode_without_lounge_data_refused():
    tv = AmbTV()
    tv.get_lounge_color = lambda: {}
    with pytest.raises(ValueError, match="нет данных о цвете"):
        run("яркость 3", tv)
    assert tv.writes() == []


# --- через обработчик: смена режима посреди сессии, ответ отказа, журнал Σ --------------------------------------------
def test_session_colour_brightness_video_brightness():
    tv = AmbTV(style="FOLLOW_VIDEO", b710=4)
    signs = H.SignSessions()
    assert say(tv, "подсветка зелёная", 0.0, signs).startswith("Подсветка: режим один цвет (статичный)")
    assert tv.style == "FOLLOW_COLOR"
    hue = tv.lounge["hue"]
    assert say(tv, "яркость 3", 1.0, signs) == "Подсветка: режим один цвет (статичный), яркость 3 из 10"
    assert tv.lounge == {"hue": hue, "saturation": 255, "brightness": C.level_to_lounge(3)} and tv.setting[710] == 4
    assert say(tv, "+2", 2.0, signs).endswith("яркость 5 из 10") and tv.lounge["brightness"] == C.level_to_lounge(5)
    assert say(tv, "подсветка кино", 3.0, signs).startswith("Подсветка: режим за картинкой")
    assert say(tv, "яркость 8", 4.0, signs) == "Подсветка: яркость 8 из 10"
    assert tv.setting[710] == 8 and tv.lounge["brightness"] == C.level_to_lounge(5)   # цвет больше не трогается


def test_handler_unknown_mode_answer_is_not_executed():
    tv = AmbTV(config={})
    a = say(tv, "яркость 3")
    assert a.startswith("📺 Команда не выполнена: нет данных о режиме подсветки") and tv.writes() == []


def test_shadow_outcome_feature_lounge_brightness(monkeypatch, tmp_path):
    monkeypatch.setenv(H.SHADOW_FLAG_ENV, "1")
    tv = AmbTV(bri=255)
    say(tv, "яркость 3")
    rows = [json.loads(x) for x in Path(tmp_path / "j" / "sigma_shadow.jsonl").read_text(encoding="utf-8").splitlines()]
    (o,) = [r for r in rows if r["step"] == "outcome"]
    raw = C.level_to_lounge(3)
    assert o["feature"] == "lounge_brightness" and o["s_exp"] == raw and o["s_obs"] == raw and o["outcome"] == "PASS"
    tv2 = AmbTV(bri=255, accept=False)
    Path(tmp_path / "j" / "sigma_shadow.jsonl").unlink()
    say(tv2, "яркость 3")
    rows = [json.loads(x) for x in Path(tmp_path / "j" / "sigma_shadow.jsonl").read_text(encoding="utf-8").splitlines()]
    (o,) = [r for r in rows if r["step"] == "outcome"]
    assert o["feature"] == "lounge_brightness" and o["s_obs"] == 255 and o["outcome"] == "FAIL"
