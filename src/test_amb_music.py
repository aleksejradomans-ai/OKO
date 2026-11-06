"""test_amb_music.py -- подсветка под музыку своим цветом (AMB_MUSIC, 04.11.2026). Сеть, микрофон и процесс не трогаются:
мост подменяется записью файла статуса, ТВ -- заглушкой."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import amb_music as M
import tv_commands as C

TOPO = {"left": 3, "top": 7, "right": 3, "bottom": 0}


# --- разбор фраз --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase,mode,key,val", [
    ("подсветка под музыку зеленая", M.STATIC, "rgb", [0, 255, 0]),
    ("подсветка под музыку 0 120 255", M.STATIC, "rgb", [0, 120, 255]),
    ("подсветка под музыку 0078ff", M.STATIC, "rgb", [0, 120, 255]),
    ("подсветка музыка синяя", M.STATIC, "rgb", [0, 0, 255]),
    ("подсветка под музыку от синего до желтого", M.GRADIENT, "a", [0, 0, 255]),
    ("подсветка переход", M.GRADIENT, None, None),
    ("подсветка градиент", M.GRADIENT, None, None),
])
def test_parse_music(phrase, mode, key, val):
    upd = M.parse_music(C.normalize(phrase))
    assert upd["mode"] == mode
    if key:
        assert upd[key] == val


def test_range_end_colour():
    upd = M.parse_music(C.normalize("подсветка под музыку от синего до желтого"))
    assert upd["b"] == [255, 220, 0]


@pytest.mark.parametrize("phrase", ["подсветка музыка", "подсветка под музыку", "подсветка красная 100",
                                    "подсветка музыка 5", "подсветка синхрон"])
def test_not_ours(phrase):
    assert M.parse_music(C.normalize(phrase)) is None


def test_rgb_out_of_range_is_not_rgb():
    assert M.parse_rgb("300 10 10") is None


@pytest.mark.parametrize("phrase,name", [
    ("подсветка под музыку зелёная", "amb_music"), ("подсветка переход", "amb_music"),
    ("подсветка музыка", "amb_audio"), ("подсветка красная", "amb_colour"), ("подсветка музыка выключи", "amb_off"),
    ("подсветка вечеринка", "amb_audio"),
])
def test_tv_commands_route(phrase, name):
    assert C.parse(phrase).name == name


def test_help_mentions_amb():
    assert "/amb" in C.help_text()


# --- цвет ----------------------------------------------------------------------------------------------------

def test_ring_order_and_flip():
    s = M.State()
    r = M.ring(TOPO, s)
    assert len(r) == 13 and r[0] == ("left", 0) and r[3] == ("top", 0) and r[-1] == ("right", 2)
    s.flip_left = True
    assert M.ring(TOPO, s)[0] == ("left", 2)


def test_static_all_same():
    s = M.State(mode=M.STATIC, rgb=[10, 20, 30])
    assert set(M.base_colors(TOPO, s).values()) == {(10, 20, 30)}


def test_gradient_blue_to_yellow_goes_through_green():
    """Как клавиатура Nitro 5: синий -> бирюзовый -> зелёный -> жёлтый, а НЕ через фиолетовый/красный."""
    s = M.State(mode=M.GRADIENT, a=[0, 0, 255], b=[255, 255, 0])
    cols = list(M.base_colors(TOPO, s).values())
    assert cols[0] == (0, 0, 255) and cols[-1] == (255, 255, 0)
    mid = cols[6]
    assert mid[1] > 200 and mid[0] < 60          # середина -- зелёная
    assert all(c[0] < 200 or c[1] > 150 for c in cols)   # ни одной красной/фиолетовой лампы


def test_grey_end_takes_other_hue():
    assert M.blend([255, 255, 255], [255, 0, 0], 1.0) == (255, 0, 0)


def test_frame_silence_dim_not_black_and_full():
    colors = {("left", 0): (200, 100, 0)}
    quiet = M.frame(colors, 0.0)["left"]["0"]
    loud = M.frame(colors, 1.0)["left"]["0"]
    assert 0 < quiet["r"] < 20 and loud == {"r": 200, "g": 100, "b": 0}


# --- пульт и кнопки ------------------------------------------------------------------------------------------

def test_state_file_roundtrip_and_garbage(tmp_path):
    p = tmp_path / "c.json"
    M.save_state(p, M.State(mode=M.GRADIENT, a=[1, 2, 3]))
    assert M.load_state(p).a == [1, 2, 3]
    p.write_text("{битый", encoding="utf-8")
    assert M.load_state(p) == M.State()
    p.write_text(json.dumps({"rgb": [999, -5, "x"], "ceil": -70, "floor": -20, "mode": "zzz", "device": True}),
                 encoding="utf-8")
    s = M.load_state(p)
    assert s.rgb == [0, 120, 255] and s.mode == M.STATIC and s.floor < s.ceil and s.device is None


def test_slider_static_clamped():
    s = M.State(rgb=[250, 0, 100])
    s, act = M.apply_button(s, "amb:c:r:+32")
    assert act == "save" and s.rgb == [255, 0, 100]
    s, _ = M.apply_button(s, "amb:c:g:-8")
    assert s.rgb[1] == 0


def test_slider_gradient_edits_selected_end():
    s = M.State(mode=M.GRADIENT)
    s, _ = M.apply_button(s, "amb:e:b")
    s, _ = M.apply_button(s, "amb:c:b:-32")
    assert s.b == [255, 255, 0] and s.a == [0, 0, 255]
    s, _ = M.apply_button(s, "amb:c:r:-32")
    assert s.b == [223, 255, 0]
    s, _ = M.apply_button(s, "amb:p:ff0000")
    assert s.b == [255, 0, 0] and s.a == [0, 0, 255]


@pytest.mark.parametrize("data", ["amb:c:x:+8", "amb:c:r:abc", "amb:m:zzz", "amb:p:zz", "nope", "amb:nop"])
def test_bad_buttons_noop(data):
    s0 = M.State()
    s, act = M.apply_button(M.State(), data)
    assert act == "" and s == s0


@pytest.mark.parametrize("mode", [M.STATIC, M.GRADIENT])
@pytest.mark.parametrize("running", [True, False])
def test_panel_buttons_valid(mode, running):
    st = {"running": running, "beat": 100.0, "frames": 5}
    text, rows = M.panel(M.State(mode=mode), st, now=101.0)
    datas = [d for row in rows for _, d in row]
    assert all(len(d.encode()) <= 64 and d.startswith("amb:") for d in datas)
    assert ("amb:stop" in datas) == running and ("amb:go" in datas) == (not running)
    for d in datas:                                   # каждая кнопка разбирается (или явно nop)
        _, act = M.apply_button(M.State(mode=mode), d)
        assert act or d == "amb:nop"
    assert ("работает" in text) == running


def test_alive_by_fresh_beat_only():
    assert M.alive({"running": True, "beat": 100.0}, now=104.0)
    assert not M.alive({"running": True, "beat": 100.0}, now=106.0)
    assert not M.alive({"running": False, "beat": 100.0}, now=100.0)


# --- старт/стоп (мост подменён) ------------------------------------------------------------------------------

@pytest.fixture
def files(tmp_path):
    return tmp_path / "amb_music.json", tmp_path / "amb_music_status.json"


def _fake_bridge(status, ok=True):
    def spawn():
        import time as t
        st = {"running": ok, "started": t.time(), "beat": t.time(), "frames": 3 if ok else 0, "tv_mode": "expert",
              "reason": "" if ok else "телевизор недоступен: X"}
        M.write_json(status, st)
    return spawn


def test_start_reports_from_status(files):
    c, s = files
    out = M.start({"mode": M.STATIC, "rgb": [0, 255, 0]}, control=c, status=s, spawn=_fake_bridge(s),
                  sleep=lambda _: None)
    assert "режим под музыку" in out and "#00FF00" in out and "expert" in out and "кадров 3" in out
    assert M.load_state(c).on


def test_start_failure_honest_and_switches_off(files):
    c, s = files
    out = M.start({}, control=c, status=s, spawn=_fake_bridge(s, ok=False), sleep=lambda _: None)
    assert "НЕ запущена" in out and "телевизор недоступен" in out and not M.load_state(c).on


def test_start_no_answer_times_out(files):
    c, s = files
    out = M.start({}, control=c, status=s, spawn=lambda: None, timeout=0.01, sleep=lambda _: None)
    assert "НЕ запущена" in out and "не ответил" in out


def test_start_running_updates_live_no_spawn(files):
    c, s = files
    import time as t
    M.write_json(s, {"running": True, "beat": t.time()})
    called = []
    out = M.start({"rgb": [1, 2, 3]}, control=c, status=s, spawn=lambda: called.append(1))
    assert called == [] and "на лету" in out and M.load_state(c).rgb == [1, 2, 3]


def test_stop_when_idle_is_silent(files):
    c, s = files
    assert M.stop(control=c, status=s) == ""


def test_stop_waits_for_bridge(files):
    c, s = files
    import time as t
    M.save_state(c, M.State(on=True))
    M.write_json(s, {"running": True, "beat": t.time()})

    def sleep(_):
        M.write_json(s, {"running": False, "restored": "вернул стиль FOLLOW_VIDEO/STANDARD (режим internal)"})
    out = M.stop(control=c, status=s, sleep=sleep)
    assert "остановлена" in out and "FOLLOW_VIDEO" in out and not M.load_state(c).on


def test_on_button_save_does_not_spawn(files):
    c, s = files
    assert M.on_button("amb:c:g:+8", control=c, status=s) == ""
    assert M.load_state(c).rgb == [0, 128, 255]


# --- возврат стиля -------------------------------------------------------------------------------------------

class FakeTV:
    def __init__(self, mode_after="internal"):
        self.calls, self.mode = [], "expert"
        self.mode_after = mode_after
        self.cfg = {}

    def set_ambilight_style(self, style, setting=None):
        self.calls.append(("style", style, setting))
        self.cfg = {"styleName": style, "menuSetting": setting}
        self.mode = self.mode_after

    def set_lounge_color(self, h, s, b):
        self.calls.append(("lounge", h, s, b))

    def set_ambilight_mode(self, m):
        self.calls.append(("mode", m))
        self.mode = m

    def get_ambilight_mode(self):
        return {"current": self.mode}

    def get_ambilight_config(self):
        return self.cfg


def test_restore_follow_color_with_lounge(monkeypatch):
    monkeypatch.setattr(M.time, "sleep", lambda _: None)
    tv = FakeTV(mode_after="lounge")
    out = M.restore(tv, {"config": {"styleName": "FOLLOW_COLOR", "menuSetting": "MANUAL_HUE"},
                         "lounge": {"color": {"hue": 85, "saturation": 255, "brightness": 0}}})
    assert ("lounge", 85, 255, 0) in tv.calls and "FOLLOW_COLOR" in out and "lounge" in out


def test_restore_forces_out_of_expert(monkeypatch):
    monkeypatch.setattr(M.time, "sleep", lambda _: None)
    tv = FakeTV(mode_after="expert")
    M.restore(tv, {"config": {"styleName": "FOLLOW_VIDEO", "menuSetting": "STANDARD"}})
    assert ("mode", "internal") in tv.calls


def test_restore_unknown_goes_video(monkeypatch):
    monkeypatch.setattr(M.time, "sleep", lambda _: None)
    tv = FakeTV()
    M.restore(tv, {})
    assert tv.calls[0] == ("style", "FOLLOW_VIDEO", "STANDARD")


# --- проводка: обработчик ТВ и бот ---------------------------------------------------------------------------

def test_style_commands_stop_music_first(monkeypatch):
    import tv_bot_handler as H
    order = []
    monkeypatch.setattr(M, "stop", lambda **kw: order.append("stop") or "🎵 остановлена")
    cmd = C.Command(kind=C.VERIFIED, name="amb_colour", run=lambda tv: order.append("run") or "Подсветка: ok")
    out = H._execute(cmd, lambda: object())
    assert order == ["stop", "run"] and "🎵 остановлена" in out


@pytest.mark.parametrize("name", ["amb_brighter", "amb_music", "volume_set", "amb_brightness_get"])
def test_other_commands_leave_music(monkeypatch, name):
    import tv_bot_handler as H
    monkeypatch.setattr(M, "stop", lambda **kw: pytest.fail("мост остановлен зря"))
    H._execute(C.Command(kind=C.VERIFIED, name=name, run=lambda tv: "ok"), lambda: object())


def test_stop_failure_does_not_block_command(monkeypatch):
    import tv_bot_handler as H

    def boom(**kw):
        raise OSError("x")
    monkeypatch.setattr(M, "stop", boom)
    out = H._execute(C.Command(kind=C.VERIFIED, name="amb_off", run=lambda tv: "Подсветка: выключена"), lambda: 1)
    assert out == "Подсветка: выключена"


BOT = Path(__file__).with_name("bot.py")


@pytest.fixture(scope="module")
def bot_src():
    return BOT.read_text(encoding="utf-8-sig")


def test_bot_wiring(bot_src):
    assert re.search(r"^import amb_music\b", bot_src, re.M)
    assert 'CommandHandler("amb", handle_amb)' in bot_src
    body = bot_src[bot_src.index("async def handle_amb("):bot_src.index("async def _handle_amb_choice(")]
    assert "ADMIN_USER_IDS" in body and "asyncio.to_thread" in body
    cb = bot_src[bot_src.index("async def handle_staging_callback("):]
    assert cb.index('data.startswith("amb:")') < cb.index('bot_data["staging_manager"]')
    choice = bot_src[bot_src.index("async def _handle_amb_choice("):]
    choice = choice[:choice.index("\nasync def ")]
    assert "asyncio.to_thread(amb_music.on_button" in choice and "_answer_callback_safely" in choice
    assert not re.search(r"MessageHandler\([^)]*handle_amb", bot_src)


# --- без музыки (04.11 19:37) ---------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase,upd", [
    ("подсветка 0 120 255", {"mode": M.STATIC, "rgb": [0, 120, 255], "music": False}),
    ("подсветка без музыки зеленая", {"mode": M.STATIC, "rgb": [0, 255, 0], "music": False}),
    ("подсветка статика синяя", {"mode": M.STATIC, "rgb": [0, 0, 255], "music": False}),
    ("подсветка под музыку зеленая", {"mode": M.STATIC, "rgb": [0, 255, 0], "music": True}),
    ("подсветка без музыки", {"music": False}),
    ("подсветка переход без музыки", {"mode": M.GRADIENT, "music": False}),
    ("подсветка переход", {"mode": M.GRADIENT, "music": True}),
])
def test_parse_music_flag(phrase, upd):
    assert M.parse_music(C.normalize(phrase)) == upd


@pytest.mark.parametrize("phrase,name", [
    ("подсветка 0 120 255", "amb_music"), ("подсветка без музыки", "amb_music"),
    ("подсветка 7", "amb_brightness_set"), ("подсветка красная 100", "amb_colour"), ("яркость 8", "amb_brightness_set"),
])
def test_no_music_routes(phrase, name):
    assert C.parse(phrase).name == name


def test_no_music_buttons():
    s = M.State()
    s, act = M.apply_button(s, "amb:s:off")
    assert act == "save" and not s.music
    s, _ = M.apply_button(s, "amb:b:-20")
    s, _ = M.apply_button(s, "amb:b:-100")
    assert s.bright == 5
    s, _ = M.apply_button(s, "amb:b:+500")
    assert s.bright == 100
    s, _ = M.apply_button(s, "amb:s:on")
    assert s.music


def test_panel_no_music_has_brightness_and_text():
    text, rows = M.panel(M.State(music=False, bright=40), {})
    datas = [d for r in rows for _, d in r]
    assert "amb:b:+5" in datas and "без музыки" in text and "яркость 40%" in text
    _, rows = M.panel(M.State(), {})
    assert "amb:b:+5" not in [d for r in rows for _, d in r]


def test_start_no_music_text(files):
    c, s = files
    out = M.start({"rgb": [1, 2, 3], "music": False}, control=c, status=s, spawn=_fake_bridge(s),
                  sleep=lambda _: None)
    assert "без музыки" in out and "микрофон" not in out


def test_bright_garbage_in_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"bright": "x", "music": 0}), encoding="utf-8")
    s = M.load_state(p)
    assert s.bright == 100 and s.music is False


def test_panel_reports_mic_state():
    """04.11 19:49: значка Windows нет (сеанс 0) -- о микрофоне сообщает сам мост."""
    text, _ = M.panel(M.State(), {"running": True, "beat": 100.0, "mic": "закрыт"}, now=100.0)
    assert "🎙 микрофон: закрыт" in text
    text, _ = M.panel(M.State(), {"running": False}, now=100.0)
    assert "микрофон" not in text


def test_bridge_keeps_no_audio():
    """Мост не пишет звук и не распознаёт речь: в модуле нет записи wav/stt/отправки звука."""
    src = Path(M.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b(wave|soundfile|stt|whisper|sendVoice|send_voice)\b", src.split('"""', 2)[2])


# --- микрофон не прячем (04.11 20:08) -------------------------------------------------------------------------

@pytest.mark.parametrize("sid,ok", [(None, True), (1, True), (2, True), (0, False), (-1, False)])
def test_mic_only_in_user_session(sid, ok):
    """Сеанс службы (0) и неизвестный (-1) -- значка Windows нет -> микрофон запрещён."""
    assert M.mic_allowed(sid) is ok


def _bridge_at(status, session, mic):
    def spawn():
        import time as t
        M.write_json(status, {"running": True, "started": t.time(), "beat": t.time(), "frames": 2,
                              "tv_mode": "expert", "session": session, "mic": mic})
        return "user" if session else "service"
    return spawn


def test_start_says_where_and_mic(files):
    c, s = files
    out = M.start({"music": True}, control=c, status=s, spawn=_bridge_at(s, 1, "открыт"), sleep=lambda _: None)
    assert "в твоём сеансе Windows" in out and "🎙 микрофон: открыт" in out


def test_start_in_service_reports_denied(files):
    c, s = files
    out = M.start({"music": True}, control=c, status=s, spawn=_bridge_at(s, 0, M.MIC_DENIED), sleep=lambda _: None)
    assert "в сеансе службы" in out and "запрещён" in out


def test_user_task_silent_no_music_falls_back_to_service(files):
    c, s = files
    calls = []
    out = M.start({"music": False}, control=c, status=s, spawn=lambda: calls.append("user") or "user",
                  fallback=lambda: calls.append("svc") or _bridge_at(s, 0, "закрыт")(), timeout=0.01,
                  sleep=lambda _: None)
    assert calls == ["user", "svc"] and "сеансе службы" in out


def test_user_task_silent_music_never_falls_back(files):
    """Под музыку тихая подмена на службу запрещена: там микрофон не откроется, значка нет."""
    c, s = files
    out = M.start({"music": True}, control=c, status=s, spawn=lambda: "user",
                  fallback=lambda: pytest.fail("подмена на службу под музыку"), timeout=0.01, sleep=lambda _: None)
    assert "НЕ запущена" in out and "вход в Windows" in out


def test_panel_shows_denied():
    text, _ = M.panel(M.State(), {"running": True, "beat": 100.0, "mic": M.MIC_DENIED}, now=100.0)
    assert "запрещён" in text


# --- настоящий цикл моста на заглушках: гарантия «в сеансе службы микрофон не открывается» -------------------

class _Stream:
    opened = 0

    def __init__(self, **kw):
        self.cb = kw["callback"]

    def start(self):
        type(self).opened += 1
        self.cb(memoryview(b"\0" * 8), 2, None, None)

    def stop(self):
        pass

    def close(self):
        type(self).opened -= 1


class _BridgeTV:
    host = "10.0.0.5"

    def __init__(self):
        self.mode, self.posts = "lounge", 0

    def get_ambilight_config(self):
        return {"styleName": "FOLLOW_VIDEO", "menuSetting": "STANDARD"}

    def get_lounge_color(self):
        return {}

    def set_ambilight_power(self, on):
        pass

    def set_ambilight_mode(self, m):
        self.mode = m

    def get_ambilight_mode(self):
        return {"current": self.mode}

    def get_topology(self):
        return dict(TOPO)

    def _call(self, method, path, body):
        self.posts += 1

    def set_ambilight_style(self, style, setting=None):
        self.mode = "internal"


def _run_bridge(monkeypatch, tmp_path, sid, seconds=1.0, tv=None):
    import sys
    import threading
    import time as t
    import types

    import audio_bridge  # noqa: F401 -- настоящий модуль в sys.modules: после теста monkeypatch вернёт именно его
    import tv_control
    fake_sd = types.SimpleNamespace(
        query_devices=lambda *a, **k: {"name": "mic", "max_input_channels": 2, "default_samplerate": 48000},
        InputStream=_Stream)
    fake_meter = types.SimpleNamespace(analyze=lambda *a, **k: types.SimpleNamespace(rms_dbfs=-30.0),
                                       SILENCE_DBFS=-80.0, dbfs=lambda x: -30.0)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setitem(sys.modules, "audio_meter", fake_meter)
    monkeypatch.delitem(sys.modules, "audio_bridge", raising=False)
    tv = tv or _BridgeTV()
    monkeypatch.setattr(tv_control, "TVClient", types.SimpleNamespace(from_env=lambda: tv))
    c, s = tmp_path / "amb_music.json", tmp_path / "amb_music_status.json"
    monkeypatch.setattr(M, "_paths", lambda: (c, s))
    monkeypatch.setattr(M, "session_id", lambda: sid)
    monkeypatch.setattr(M, "_load_env", lambda: None)
    M.save_state(c, M.State(on=True, music=True))
    _Stream.opened = 0
    th = threading.Thread(target=M.run, daemon=True)
    th.start()
    end = t.monotonic() + seconds
    peak = 0
    while t.monotonic() < end:
        peak = max(peak, _Stream.opened)
        t.sleep(0.02)
    st = M.read_json(s)
    M.save_state(c, M.State(on=False))
    th.join(5)
    return peak, st, tv


def test_bridge_in_service_session_never_opens_mic(monkeypatch, tmp_path):
    peak, st, tv = _run_bridge(monkeypatch, tmp_path, sid=0)
    assert peak == 0 and st["mic"] == M.MIC_DENIED and st["session"] == 0 and tv.posts > 0


def test_bridge_in_user_session_opens_mic(monkeypatch, tmp_path):
    peak, st, _ = _run_bridge(monkeypatch, tmp_path, sid=1)
    assert peak == 1 and st["mic"] == "открыт"


# --- ТВ отдаёт не JSON (04.11 22:42) -------------------------------------------------------------------------

def test_retry_survives_flaky_reads():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("ответ не JSON")
        return {"ok": 1}
    assert M._retry(flaky, sleep=lambda _: None) == {"ok": 1} and len(calls) == 3


def test_retry_gives_up_with_last_error():
    with pytest.raises(ValueError):
        M._retry(lambda: (_ for _ in ()).throw(ValueError("x")), sleep=lambda _: None)


def test_restore_says_unknown_previous(monkeypatch):
    monkeypatch.setattr(M.time, "sleep", lambda _: None)
    out = M.restore(FakeTV(), {})
    assert out.startswith("прежний стиль не был прочитан") and "FOLLOW_VIDEO" in out


class _FlakyTV(_BridgeTV):
    def __init__(self):
        super().__init__()
        self.bad = {"cfg": 2, "mode": 1}

    def get_ambilight_config(self):
        if self.bad["cfg"]:
            self.bad["cfg"] -= 1
            raise ValueError("GET ambilight/currentconfiguration: ответ не JSON")
        return super().get_ambilight_config()

    def get_ambilight_mode(self):
        if self.bad["mode"]:
            self.bad["mode"] -= 1
            raise ValueError("не JSON")
        return super().get_ambilight_mode()


def test_bridge_starts_despite_non_json(monkeypatch, tmp_path):
    """Живой случай 22:42: первое чтение стиля -- не JSON. Мост обязан стартовать (повтор), а не «ТВ недоступен»."""
    import tv_control
    import types
    tv = _FlakyTV()
    orig = types.SimpleNamespace(from_env=lambda: tv)
    monkeypatch.setattr(tv_control, "TVClient", orig)
    peak, st, _ = _run_bridge(monkeypatch, tmp_path, sid=1, tv=tv, seconds=3.0)
    assert st["running"] and st["tv_mode"] == "expert" and st["frames"] > 0, st


# --- гонка файлов на Windows (04.11 22:47: «сбилось на заводские») -------------------------------------------

def test_write_json_retries_permission_error(tmp_path, monkeypatch):
    real, calls = M.os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Отказано в доступе")
        return real(a, b)
    monkeypatch.setattr(M.os, "replace", flaky)
    p = tmp_path / "s.json"
    M.write_json(p, {"x": 1}, sleep=lambda _: None)
    assert M.read_json(p) == {"x": 1} and len(calls) == 3


def test_write_json_gives_up(tmp_path, monkeypatch):
    def deny(a, b):
        raise PermissionError(5, "Отказано в доступе")
    monkeypatch.setattr(M.os, "replace", deny)
    with pytest.raises(PermissionError):
        M.write_json(tmp_path / "s.json", {}, sleep=lambda _: None)


def test_bridge_survives_status_and_control_races(monkeypatch, tmp_path):
    """Статус не пишется вовсе, пульт через раз читается пустым -- мост продолжает светить, пока не скажут «выкл»."""
    real_read, real_replace = M.read_json, M.os.replace
    n = {"r": 0}

    def racy_read(path):
        n["r"] += 1
        return {} if n["r"] % 2 else real_read(path)

    def deny_status(a, b):
        if str(b).endswith("amb_music_status.json"):
            raise PermissionError(5, "Отказано в доступе")
        return real_replace(a, b)
    monkeypatch.setattr(M, "REPLACE_TRIES", 1)
    monkeypatch.setattr(M, "read_json", racy_read)
    monkeypatch.setattr(M.os, "replace", deny_status)
    import sys
    import threading
    import time as t
    import types

    import audio_bridge  # noqa: F401
    import tv_control
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(
        query_devices=lambda *a, **k: {"name": "mic", "max_input_channels": 2, "default_samplerate": 48000},
        InputStream=_Stream))
    tv = _BridgeTV()
    monkeypatch.setattr(tv_control, "TVClient", types.SimpleNamespace(from_env=lambda: tv))
    c, s = tmp_path / "amb_music.json", tmp_path / "amb_music_status.json"
    monkeypatch.setattr(M, "_paths", lambda: (c, s))
    monkeypatch.setattr(M, "session_id", lambda: 1)
    monkeypatch.setattr(M, "_load_env", lambda: None)
    M.save_state(c, M.State(on=True, music=False))
    th = threading.Thread(target=M.run, daemon=True)
    th.start()
    t.sleep(2.5)
    assert th.is_alive() and tv.posts > 0 and tv.mode == "expert"
    M.save_state(c, M.State(on=False))
    th.join(5)
    assert not th.is_alive() and tv.mode == "internal"
