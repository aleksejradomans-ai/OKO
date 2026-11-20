"""(16.11): знаковые команды «+1», «−2», «+», «++» (tv_commands.parse_sign / sign_command, tv_bot_handler: сессия
последнего параметра, handle, sign_route; bot._route_reply -- вызов до маршрутизатора).

Блок (УТВЕРЖДЁН Оператором 16.11 21:54). Каждый пункт -- свой тест:
  P1  «+N» / «−N» -> STEP ±N для параметра, изменённого последним в сессии (пауза ≤ 60 с): «яркость 5» -> «+1» -> 6;
  P2  «+» / «−» -> ±1; «++» / «−−» -> ±2; прочая знаковая запись -> переспрос;
  P3  нет последнего параметра (новая сессия, пауза > 60 с) -> CLARIFY «чего +1?», ничего не исполняется;
  P4  громкость: шаг знаком, итог ≤ 15; выше -- отказ + переспрос, запись в ТВ не уходит;
  P5  «−», «-», «–» эквивалентны; пробелы («+ 1») допустимы;
  P6  Σ видит обычный STEP с d_cmd (те же строки журнала, что у «яркость плюс N»);
  P7  регрессия: «плюс 1», «ярче», абсолютные команды -- без изменений; router.py не меняется (вызов -- из bot.py).
Сеть не затрагивается: двойник ТВ в памяти, модель смысла заглушена.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

import router
import sigma_gate as G
import tv_bot_handler as H
import tv_commands as C
import tv_intent
from tv_control import TVTransportError

ADMIN = 4343
GUEST = 77


class StepTV:
    """Двойник с состоянием: ползунки 710 (яркость 0..10) / 720 (насыщенность −2..2), громкость 0..60 (потолок контура 30).
    Пишет вызовы. volume_unreadable -- get_volume без числа; fail -- любой вызов -> сеть."""

    def __init__(self, bri: int = 4, sat: int = 0, vol: int = 10) -> None:
        self.calls: list = []
        self.setting = {710: bri, 720: sat}
        self.vol = vol
        self.volume_unreadable = False
        self.fail = False

    def _rec(self, name, args, res):
        if self.fail:
            raise TVTransportError("сеть (тест)")
        self.calls.append((name, args))
        return res

    def get_setting_value(self, node):
        return self._rec("get_setting_value", (node,), self.setting[node])

    def set_setting_value(self, node, value, lo, hi):
        v = max(lo, min(hi, int(value)))
        self.setting[node] = v
        return self._rec("set_setting_value", (node, value, lo, hi), v)

    def get_volume(self):
        cur = None if self.volume_unreadable else self.vol
        return self._rec("get_volume", (), {"current": cur, "min": 0, "max": 60, "muted": False})

    def set_volume(self, level, muted=False):
        self.vol = min(int(level), 30)
        return self._rec("set_volume", (level,), self.vol)

    def volume_step(self, delta):
        now = self.vol
        tgt = max(0, min(60, now + int(delta)))
        self.vol = tgt if delta < 0 else max(now, min(tgt, 30))
        return self._rec("volume_step", (delta,), self.vol)

    def set_ambilight_power(self, on):
        return self._rec("set_ambilight_power", (on,), None)

    def set_ambilight_style(self, style, setting=None):
        return self._rec("set_ambilight_style", (style, setting), None)

    def get_ambilight_config(self):          # B20: яркость идёт по режиму; двойник -- «за картинкой» (ползунок 710)
        return self._rec("get_ambilight_config", (), {"styleName": "FOLLOW_VIDEO", "menuSetting": "STANDARD"})

    def set_lounge_color(self, hue, sat, bri):
        return self._rec("set_lounge_color", (hue, sat, bri), None)

    def get_lounge_color(self):
        return self._rec("get_lounge_color", (), {"color": {"hue": 0, "saturation": 255, "brightness": 255}})

    def writes(self) -> list:
        return [c for c in self.calls if not c[0].startswith("get_")]


class Factory:
    def __init__(self, tv: StepTV) -> None:
        self.tv = tv
        self.count = 0

    def __call__(self):
        self.count += 1
        return self.tv


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
    H._SHADOW_WARNED.clear()
    G.reset()
    yield
    G.reset()


class Session:
    """Один пользователь: свои ожидания и своя сессия знаков; часы задаёт тест."""

    def __init__(self, tv: StepTV | None = None) -> None:
        self.tv = tv or StepTV()
        self.factory = Factory(self.tv)
        self.pending = H.PendingStore()
        self.signs = H.SignSessions()

    def say(self, text: str, t: float, user: int = ADMIN) -> str:
        return H.handle(user, text, self.factory, pending=self.pending, admin_ids=[ADMIN], now=t, signs=self.signs)

    def route(self, text: str, t: float, user: int = ADMIN):
        return H.sign_route(user, text, admin_ids=[ADMIN], pending=self.pending, signs=self.signs, now=t)


# ===================================================================== P1
def test_b08e_P1_plus_one_after_brightness_set():
    s = Session()
    assert s.say("яркость 5", 0.0).startswith("Подсветка: яркость 5 из 10")
    a = s.say("+1", 5.0)
    assert s.tv.setting[710] == 6
    assert a.startswith("Понял как «яркость плюс 1». Подсветка: яркость 6 из 10")


def test_b08e_P1_minus_n_after_brightness_set():
    s = Session()
    s.say("яркость 5", 0.0)
    s.say("−2", 3.0)
    assert s.tv.setting[710] == 3


@pytest.mark.parametrize("n", [1, 2, 3, 7])
def test_b08e_P1_plus_n_steps_by_n(n):
    s = Session(StepTV(bri=0))
    s.say("яркость 1", 0.0)
    s.say(f"+{n}", 1.0)
    assert s.tv.setting[710] == min(10, 1 + n)


def test_b08e_P1_last_changed_parameter_wins():
    s = Session()
    s.say("яркость 5", 0.0)
    s.say("громкость 10", 2.0)
    s.say("+1", 4.0)
    assert s.tv.vol == 11 and s.tv.setting[710] == 5
    s.say("подсветка ярче", 6.0)
    s.say("+1", 8.0)
    assert s.tv.setting[710] == 7 and s.tv.vol == 11


def test_b08e_P1_saturation_step():
    s = Session()
    s.say("насыщенность 1", 0.0)
    a = s.say("-1", 1.0)
    assert s.tv.setting[720] == 0
    assert a.startswith("Понял как «насыщенность минус 1».")


def test_b08e_P1_sign_steps_refresh_the_session():
    s = Session()
    s.say("яркость 5", 0.0)
    s.say("+1", 50.0)
    s.say("+1", 100.0)          # 50 с после предыдущего шага -- сессия жива
    assert s.tv.setting[710] == 7


def test_b08e_P1_step_command_is_the_rule_command():
    for param, d, phrase in [("brightness", 1, "яркость плюс 1"), ("brightness", -3, "яркость минус 3"),
                             ("saturation", 2, "насыщенность плюс 2")]:
        a, b = C.sign_command(param, d), C.parse(phrase)
        assert (a.name, a.kind, a.meta) == (b.name, b.kind, b.meta)
    v = C.sign_command("volume", 4)
    assert (v.name, v.meta) == ("volume_up", {"step": 4})
    assert (C.sign_command("volume", -2).name, C.sign_command("volume", -2).meta) == ("volume_down", {"step": 2})


# ===================================================================== P2
@pytest.mark.parametrize("text,delta", [("+", 1), ("−", -1), ("-", -1), ("–", -1), ("++", 2), ("−−", -2),
                                        ("--", -2), ("––", -2), ("+ +", 2)])
def test_b08e_P2_bare_signs(text, delta):
    assert C.parse_sign(text).delta == delta
    s = Session()
    s.say("яркость 5", 0.0)
    s.say(text, 1.0)
    assert s.tv.setting[710] == 5 + delta


@pytest.mark.parametrize("text", ["+++", "---", "+-", "-+1", "++1", "−−2", "+0", "-0", "+1000", "+0001"])
def test_b08e_P2_other_sign_forms_clarify(text):
    sg = C.parse_sign(text)
    assert sg is not None and sg.delta is None
    s = Session()
    s.say("яркость 5", 0.0)
    n0 = s.factory.count
    a = s.say(text, 1.0)
    assert a.startswith("❓ Не понял шаг") and "ничего не отправлено" in a
    assert s.factory.count == n0 and s.tv.setting[710] == 5


# ===================================================================== P3
def test_b08e_P3_no_session_clarify_nothing_executed():
    s = Session()
    a = s.say("+1", 0.0)
    assert a.startswith("❓ Чего +1?") and "Ничего не отправлено" in a
    assert s.factory.count == 0 and s.tv.calls == []


@pytest.mark.parametrize("text,label", [("+1", "+1"), ("−2", "−2"), ("+", "+"), ("++", "++"), ("- 3", "−3")])
def test_b08e_P3_clarify_names_the_sign(text, label):
    assert Session().say(text, 0.0).startswith(f"❓ Чего {label}?")


def test_b08e_P3_pause_over_60s_is_new_session():
    s = Session()
    s.say("яркость 5", 0.0)
    a = s.say("+1", 60.5)
    assert a.startswith("❓ Чего +1?") and s.tv.setting[710] == 5
    n = s.factory.count
    s.say("яркость 5", 100.0)
    assert s.factory.count == n + 1
    s.say("+1", 160.0)          # ровно 60 с -- ещё сессия (≤ 60 с)
    assert s.tv.setting[710] == 6


def test_b08e_P3_after_non_step_command_clarify():
    s = Session()
    s.say("яркость 5", 0.0)
    assert s.say("подсветка красная", 2.0).startswith("Подсветка:")
    w = len(s.tv.writes())
    a = s.say("+1", 4.0)
    assert a.startswith("❓ Чего +1?") and len(s.tv.writes()) == w


def test_b08e_P3_reads_keep_the_session():
    s = Session()
    s.say("яркость 5", 0.0)
    s.say("громкость", 2.0)                     # чтение громкости
    s.say("яркость", 3.0)                       # чтение яркости
    s.say("+1", 4.0)
    assert s.tv.setting[710] == 6 and s.tv.vol == 10


def test_b08e_P3_failed_command_does_not_refresh_session():
    s = Session()
    s.say("яркость 5", 0.0)
    s.tv.fail = True
    a = s.say("+1", 50.0)
    assert a.startswith("📺") and router.tv_outcome(a) == "FAIL"
    s.tv.fail = False
    a = s.say("+1", 70.0)                       # 70 с от последнего исполненного
    assert a.startswith("❓ Чего +1?") and s.tv.setting[710] == 5


def test_b08e_P3_clock_backwards_no_session():
    s = Session()
    s.say("яркость 5", 100.0)
    assert s.say("+1", 50.0).startswith("❓ Чего +1?")


def test_b08e_P3_guest_refused_and_not_routed():
    s = Session()
    s.say("яркость 5", 0.0)
    assert s.say("+1", 1.0, user=GUEST) == "Управление телевизором доступно только администратору."
    assert s.route("+1", 1.0, user=GUEST) is None
    assert s.tv.setting[710] == 5


def test_b08e_P3_users_do_not_share_sessions():
    s = Session()
    other = 4444
    H.handle(ADMIN, "яркость 5", s.factory, pending=s.pending, admin_ids=[ADMIN, other], now=0.0, signs=s.signs)
    a = H.handle(other, "+1", s.factory, pending=s.pending, admin_ids=[ADMIN, other], now=1.0, signs=s.signs)
    assert a.startswith("❓ Чего +1?") and s.tv.setting[710] == 5


def test_b08e_P3_sign_during_pending_confirm_cancels_it():
    s = Session()
    s.say("яркость 5", 0.0)
    assert s.say("выключи телевизор", 1.0).startswith("Требуется подтверждение")
    assert s.route("+1", 2.0) == ("tv", "sign:pending", "+1", "")
    a = s.say("+1", 2.0)
    assert a.startswith("Отменено") and s.tv.setting[710] == 5


# ===================================================================== P4
def test_b08e_P4_volume_step_up_to_15_allowed():
    s = Session()
    s.say("громкость 10", 0.0)
    a = s.say("+5", 1.0)
    assert s.tv.vol == 15 and a.startswith("Понял как «громкость плюс 5». Громкость: 15/60")


def test_b08e_P4_volume_above_15_refused_before_send():
    s = Session()
    s.say("громкость 10", 0.0)
    n = s.factory.count
    a = s.say("+6", 1.0)
    assert a.startswith("❓ Громкость: сейчас 10, +6 даст 16") and "ничего не отправлено" in a and "громкость 15" in a
    assert s.factory.count == n and s.tv.vol == 10
    assert s.route("+6", 1.0)[:2] == ("clarify", "sign:volume_cap")


def test_b08e_P4_volume_above_15_refused_by_fresh_read():
    s = Session()
    s.say("громкость 10", 0.0)
    s.tv.vol = 14                               # изменили пультом: по ответу бота 10, по чтению 14
    a = s.say("+2", 1.0)
    assert a.startswith("Понял как «громкость плюс 2». ❓ Громкость: сейчас 14, +2 даст 16")
    assert s.tv.vol == 14 and not [c for c in s.tv.calls if c[0] == "volume_step"]


def test_b08e_P4_volume_unreadable_step_up_refused():
    s = Session()
    s.say("громкость 10", 0.0)
    s.tv.volume_unreadable = True
    a = s.say("+1", 1.0)
    assert "не смог прочитать" in a and not [c for c in s.tv.calls if c[0] == "volume_step"]


def test_b08e_P4_volume_at_15_and_step_down():
    s = Session()
    s.say("громкость 15", 0.0)
    assert s.say("+1", 1.0).startswith("❓ Громкость уже 15")
    s.say("−3", 2.0)
    assert s.tv.vol == 12
    s.say("+3", 3.0)
    assert s.tv.vol == 15


def test_b08e_P4_known_volume_follows_steps():
    s = Session()
    s.say("громкость 10", 0.0)
    s.say("+3", 1.0)
    s.say("+2", 2.0)
    assert s.tv.vol == 15
    n = s.factory.count
    assert s.say("+1", 3.0).startswith("❓ Громкость уже 15") and s.factory.count == n


def test_b08e_P4_known_volume_from_reply_without_number():
    st = H.SignSessions()
    cmd = C.parse("громкость 10")
    st.note(ADMIN, cmd, "Отправил: [ПОЛИГОН] volume_set", 0.0)      # ответ без числа (полигон) -> цель команды
    assert st.get(ADMIN, 1.0).volume == 10
    st.note(ADMIN, C.sign_command("volume", 3), "Отправил: [ПОЛИГОН] volume_up", 1.0)
    assert st.get(ADMIN, 2.0).volume == 13
    kind, reason, _ = H._sign_resolve(ADMIN, C.parse_sign("+3"), 2.0, st)
    assert (kind, reason) == ("clarify", "sign:volume_cap")


def test_b08e_P4_absolute_volume_unchanged():
    s = Session()
    s.say("громкость 20", 0.0)
    assert s.tv.vol == 20                       # абсолютная команда -- без потолка знака (прежний путь)


# ===================================================================== P5
@pytest.mark.parametrize("text", ["-2", "−2", "–2", "- 2", "− 2", " – 2 "])
def test_b08e_P5_minus_variants_equivalent(text):
    assert C.parse_sign(text).delta == -2
    s = Session()
    s.say("яркость 5", 0.0)
    s.say(text, 1.0)
    assert s.tv.setting[710] == 3


@pytest.mark.parametrize("text", ["+1", "+ 1", " +1 ", "+  1"])
def test_b08e_P5_spaces_allowed(text):
    assert C.parse_sign(text).delta == 1
    s = Session()
    s.say("яркость 5", 0.0)
    s.say(text, 1.0)
    assert s.tv.setting[710] == 6


def test_b08e_P5_tv_prefix_same_as_plain():
    s = Session()
    s.say("/tv яркость 5", 0.0)
    s.say("/tv −1", 1.0)
    assert s.tv.setting[710] == 4


# ===================================================================== P6
_KEYS = ("step", "action", "cmd_kind", "feature", "outcome", "err", "d_cmd", "s_exp", "s_obs", "s_raw",
         "sigma", "sigma_p", "sigma_why", "a_t", "sigma_live")


def _rows(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for x in p.read_text(encoding="utf-8").splitlines():
        if x.strip():
            r = json.loads(x)
            out.append({k: r.get(k) for k in _KEYS})
    return out


def _journal_of(monkeypatch, tmp_path, name, script):
    p = tmp_path / name / "sigma_shadow.jsonl"
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(p))
    monkeypatch.setenv(H.SHADOW_FLAG_ENV, "1")
    G.reset()
    s = Session()
    for i, t in enumerate(script):
        s.say(t, float(i))
    return _rows(p), s


@pytest.mark.parametrize("sign,phrase", [("+1", "яркость плюс 1"), ("−2", "яркость минус 2"), ("++", "яркость плюс 2")])
def test_b08e_P6_sigma_sees_ordinary_step(monkeypatch, tmp_path, sign, phrase):
    a, sa = _journal_of(monkeypatch, tmp_path, "a", ["яркость 5", sign])
    b, sb = _journal_of(monkeypatch, tmp_path, "b", ["яркость 5", phrase])
    assert a == b and a                          # те же строки журнала (шаги, признак, d_cmd, вердикт Σ)
    out = [r for r in a if r["step"] == "outcome"]
    assert out[-1]["action"] == "amb_step" and out[-1]["d_cmd"] == C.parse_sign(sign).delta
    assert out[-1]["sigma"] is not None          # вердикт Σ записан (теневой режим)
    assert sa.tv.calls == sb.tv.calls


def test_b08e_P6_volume_step_has_d_cmd(monkeypatch, tmp_path):
    a, _ = _journal_of(monkeypatch, tmp_path, "v", ["громкость 10", "+3"])
    out = [r for r in a if r["step"] == "outcome"]
    assert (out[-1]["action"], out[-1]["d_cmd"]) == ("volume_up", 3)
    b, _ = _journal_of(monkeypatch, tmp_path, "w", ["громкость 10", "громкость плюс 3"])
    assert a == b


def test_b08e_P6_clarify_writes_nothing_to_journal(monkeypatch, tmp_path):
    a, s = _journal_of(monkeypatch, tmp_path, "c", ["+1", "+++"])
    assert a == [] and s.factory.count == 0


# ===================================================================== P7
def test_b08e_P7_parse_sign_none_for_ordinary_text():
    for t in ["яркость +1", "громкость -5", "плюс 1", "1", "", "   ", "+1 яркость", "c++", "++a", "ярче", "1+1", "+-+-x"]:
        assert C.parse_sign(t) is None, t


def test_b08e_P7_rule_commands_unchanged():
    want = {"яркость 5": ("amb_brightness_set", {"level": 5}), "подсветка ярче": ("amb_brighter", {"step": 1}),
            "яркость плюс 2": ("amb_step", {"delta": 2}), "яркость минус 3": ("amb_step", {"delta": -3}),
            "громкость 10": ("volume_set", {"level": 10}), "громкость плюс 5": ("volume_up", {"step": 5}),
            "громкость -5": ("volume_down", {"step": 5}), "громче": ("volume_up", {"step": 3}),
            "насыщенность 1": ("amb_sat_set", {"level": 1})}
    for text, (name, meta) in want.items():
        c = C.parse(text)
        assert (c.name, c.meta) == (name, meta), text


def test_b08e_P7_router_plus_one_and_brighter_unchanged():
    r = router.route("плюс 1", None, None, ("подсветка", "яркость 5"))
    assert (r.kind, r.text) == (router.TV, "яркость плюс 1")
    r = router.route("плюс 1", None, None, None)
    assert (r.kind, r.reason) == (router.CLARIFY, "clarify:level")
    r = router.route("подсветка ярче", None, None, None)
    assert (r.kind, r.reason) == (router.TV, "tv_rule:amb_brighter")
    r = router.route("яркость 5", None, None, None)
    assert (r.kind, r.reason) == (router.TV, "tv_rule:amb_brightness_set")
    assert router.route("+1", None, None, None).kind == router.BRAIN     # router.py не менялся -> нужен вызов в bot.py


def test_b08e_P7_plus_word_via_handle_records_parameter():
    s = Session()
    s.say("яркость плюс 1", 0.0)                # так приходит «плюс 1» после подсветки (CTX_FOLLOW маршрутизатора)
    s.say("+1", 1.0)
    assert s.tv.setting[710] == 6


def test_b08e_P7_help_and_empty_unchanged():
    s = Session()
    assert s.say("помощь", 0.0) == C.help_text()
    assert s.say("", 0.0) == C.help_text()
    assert s.factory.count == 0


def test_b08e_P7_sign_route_kinds_match_router():
    s = Session()
    assert s.route("яркость 5", 0.0) is None and s.route("плюс 1", 0.0) is None
    k, reason, text, phrase = s.route("+1", 0.0)
    assert (k, reason, phrase) == (router.CLARIFY, "sign:no_param", "") and text.startswith("❓ Чего +1?")
    s.say("яркость 5", 1.0)
    assert s.route("+1", 2.0) == (router.TV, "sign:amb_step", "+1", "яркость плюс 1")
    assert s.route("−2", 2.0) == (router.TV, "sign:amb_step", "−2", "яркость минус 2")
    s.say("громкость 10", 3.0)
    assert s.route("+3", 4.0) == (router.TV, "sign:volume_up", "+3", "громкость плюс 3")
    assert router.tv_subject("sign:amb_step") == router.SUBJ_AMB
    assert router.tv_subject("sign:volume_up") == router.SUBJ_VOL
    assert s.route("+++", 2.0)[:2] == (router.CLARIFY, "sign:bad")


def test_b08e_P7_tv_outcome_of_sign_answers():
    s = Session()
    s.say("яркость 5", 0.0)
    assert router.tv_outcome(s.say("+1", 1.0)) == "PASS"
    assert router.tv_outcome(Session().say("+1", 0.0)) is None
    s2 = Session()
    s2.say("громкость 10", 0.0)
    s2.tv.vol = 14
    assert router.tv_outcome(s2.say("+2", 1.0)) is None          # отказ по свежему чтению -- не исполнено


def test_b08e_P7_bot_calls_sign_route_before_router():
    src = Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_route_reply")
    calls = [(sub.lineno, ast.unparse(sub)) for sub in ast.walk(fn) if isinstance(sub, ast.Call)]
    sign = [ln for ln, c in calls if c.startswith("tv_bot_handler.sign_route(")]
    routes = [ln for ln, c in calls if "router.route" in c]
    runs = [ln for ln, c in calls if c.startswith("_run_tv_route(") and "sign" not in c]
    assert len(sign) == 1 and routes and sign[0] < min(routes)
    assert any(ln > sign[0] for ln, c in calls if c.startswith("_run_tv_route(")) and runs
    seg = ast.get_source_segment(src, fn)
    assert "sign_route(user_id, raw or text)" in seg
    ctx = [ln for ln, c in calls if c.startswith("_tv_ctx_set(") and "sr[3]" in c]
    assert len(ctx) == 1 and ctx[0] > sign[0]


def test_b08e_P7_ctx_phrase_continues_in_words():
    """Контекст после знака -- словами: «ещё 2» маршрутизатор достраивает до шага, а не до значения."""
    r = router.route("ещё 2", None, None, ("подсветка", "яркость плюс 1"))
    assert (r.kind, r.text) == (router.TV, "яркость плюс 2")
    r = router.route("ещё 2", None, None, ("подсветка", "+1"))
    assert (r.kind, r.text) == (router.TV, "яркость 2")        # почему контекст -- не «+1» (значение, не шаг)
