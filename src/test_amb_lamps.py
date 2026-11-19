"""test_amb_lamps.py -- Wi-Fi лампы в мосте подсветки (AMB_LAMPS, 09.11.2026). Сеть не трогается: лампы -- заглушки."""
from __future__ import annotations

import json
import threading
import time

import pytest

import amb_music as M


# --- цвет в формате Tuya ----------------------------------------------------------------------------------------

def test_tuya_hsv_red_full_and_floor():
    assert M.tuya_hsv((255, 0, 0), 1.0) == "000003e803e8"          # как в выгрузке ламп: H0 S1000 V1000
    assert M.tuya_hsv((255, 0, 0), 0.0).endswith(f"{M.LAMP_MIN_V:04x}")   # тишина -- не ниже минимума лампы
    assert M.tuya_hsv((0, 0, 255), 1.0)[:4] == f"{240:04x}"


def test_tuya_hsv_quantized():
    assert M.tuya_hsv((0, 255, 0), 0.501) == M.tuya_hsv((0, 255, 0), 0.503)   # дрожание громкости -- без новой команды


# --- раскладка и цвета позиций ------------------------------------------------------------------------------------

def test_lamp_base_static_and_gradient_from_tv():
    s = M.State(mode=M.STATIC, rgb=[1, 2, 3])
    assert set(M.lamp_base(s).values()) == {(1, 2, 3)}
    g = M.lamp_base(M.State(mode=M.GRADIENT, a=[0, 0, 255], b=[255, 255, 0]))
    assert g[4] == (0, 0, 255) and g[1] == (255, 255, 0)            # у ТВ -- начало перехода, дальняя -- конец


def test_layout_order_and_missing(tmp_path):
    (tmp_path / "tuya").mkdir()
    devs = [{"id": f"id{i}", "ip": f"192.0.2.{i}", "key": "k" * 16, "version": "3.5", "name": f"L{i}"} for i in (4, 5, 6, 7)]
    devs.append({"id": "noip", "key": "k", "version": "3.5"})
    (tmp_path / "tuya" / "devices.json").write_text(json.dumps(devs), encoding="utf-8")
    (tmp_path / M.LAMPS_FILE).write_text(json.dumps({"order": ["id7", "id6", "id5", "id4", "noip"]}), encoding="utf-8")
    lay = M.load_lamp_layout(tmp_path)
    assert [(d["pos"], d["id"]) for d in lay] == [(1, "id7"), (2, "id6"), (3, "id5"), (4, "id4")]
    assert M.load_lamp_layout(tmp_path / "нет") == []


# --- волна от ТВ -------------------------------------------------------------------------------------------------

def test_wave_starts_at_tv_and_ramps_to_far_lamp():
    env = M.Envelope(decay=10.0)
    for i in range(50):                       # тишина, потом удар в t=1.0
        env.push(i * 0.02, 0.0)
    env.push(1.0, 1.0)
    for i in range(1, 30):
        env.push(1.0 + i * 0.01, 0.0)
    at = lambda t: M.lamp_levels("ripple", t, env)  # noqa: E731
    first = at(1.0)
    assert first[4] > M.MIN_K + 0.3 and first[1] <= M.MIN_K + 1e-9     # у ТВ уже вспыхнула, дальняя -- ещё нет
    late = at(1.0 + 3 * M.RIPPLE_STEP_S + 0.001)
    assert late[1] > late[2] > late[3] > late[4]                         # дошла до дальней; дальняя ярче (нарастание)


def test_sync_all_equal_and_silence_not_black():
    env = M.Envelope()
    env.push(0.0, 0.6)
    lv = M.lamp_levels("sync", 0.0, env)
    assert len(set(lv.values())) == 1 and abs(lv[1] - (M.MIN_K + (1 - M.MIN_K) * 0.6)) < 1e-9
    assert min(M.lamp_levels("ripple", 5.0, M.Envelope()).values()) >= M.MIN_K


def test_envelope_decays():
    env = M.Envelope(decay=0.45)
    env.push(0.0, 1.0)
    assert env.push(0.45, 0.0) == pytest.approx(0.3679, abs=1e-3)


# --- пульт ------------------------------------------------------------------------------------------------------

def test_state_lamps_garbage_is_off():
    assert M.state_from({"lamps": "disco"}).lamps == "off"
    assert M.state_from({"lamps": "ripple"}).lamps == "sync"          # волна в сейфе: старый пульт = синхрон
    assert M.apply_button(M.State(), "amb:l:ripple")[1] == ""
    assert M.State().lamps == "off"


def test_panel_has_lamp_row_and_buttons_work():
    _, rows = M.panel(M.State(), {})
    datas = [d for row in rows for _, d in row]
    assert "amb:l:ripple" not in datas                       # волна в сейфе (SAFE-13) -- кнопки нет
    for m in M.LAMP_BUTTONS:
        assert f"amb:l:{m}" in datas
        s, act = M.apply_button(M.State(), f"amb:l:{m}")
        assert act == "save" and s.lamps == m
    text, _ = M.panel(M.State(lamps="sync"), {"running": True, "beat": 1.0, "lamps": "на связи 4/4"}, now=1.0)
    assert "💡 Лампы: синхрон — на связи 4/4" in text


# --- отправка в лампы -------------------------------------------------------------------------------------------

class _Bulb:
    def __init__(self, d, log, fail=False, no28=False):
        self.d, self.log, self.fail, self.no28 = d, log, fail, no28

    def status(self):
        return {"dps": {"20": True, "21": "white", "22": 1000, "23": 1000, "24": "000003e803e8"}}

    def set_value(self, k, v, nowait=False):
        self.log.append((self.d["pos"], k, v))
        if self.fail:
            return {"Error": "Network Error: Device Unreachable", "Err": "905"}
        if nowait:
            return None
        if self.no28 and k == "28":
            return {"Error": "Network Error: Device Unreachable", "Err": "905"}
        return {"dps": {k: v}}

    def set_multiple_values(self, vals):
        self.log.append((self.d["pos"], "multi", tuple(sorted(vals))))
        return {"dps": vals}


LAY = [{"pos": p, "id": f"id{p}", "ip": "x", "key": "k", "version": "3.5", "name": f"L{p}"} for p in (1, 2)]


def _wait(cond, t=2.0):
    end = time.monotonic() + t
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    return cond()


def test_lampout_latest_wins_and_restore_mode_last():
    log: list = []
    out = M.LampOut(LAY, factory=lambda d: _Bulb(d, log), gap=0.05).start()
    for h in ("aaaa", "bbbb", "cccc"):
        out.push(1, h)
    out.push(2, "dddd")
    assert _wait(lambda: (1, "28", "1cccc00000000") in log and (2, "28", "1dddd00000000") in log)   # канал музыки, плавно
    assert ("21" in [k for p, k, _ in log if p == 1][:1])                    # первым -- режим colour
    assert "на связи 2/2" in out.stats()
    msg = out.close(restore=True)
    tail = [e for e in log if e[0] == 1][-2:]
    assert tail[0][1] == "multi" and tail[1] == (1, "21", "white")          # возврат: режим ПОСЛЕДНИМ
    assert "2/2" in msg


def test_lampout_failing_lamp_does_not_block_others():
    log: list = []
    out = M.LampOut(LAY, factory=lambda d: _Bulb(d, log, fail=d["pos"] == 1), gap=0.01, retry=0.05).start()
    out.push(1, "aaaa")
    out.push(2, "bbbb")
    assert _wait(lambda: (2, "28", "1bbbb00000000") in log)
    assert _wait(lambda: out.fails >= 1)
    assert out.ok[2] and not out.ok[1]
    out.close(restore=False)


# --- мост: лампы выключены -- ничего не создаётся (прежние тесты моста не меняются) ---------------------------------

def test_bridge_no_layout_reports_honestly(tmp_path):
    assert M.load_lamp_layout(tmp_path) == []


# --- выравнивание по самой медленной лампе (09.11 12:47: №4 «подвисает») -----------------------------------------

def test_align_fast_lamp_waits_for_slow_capped():
    out = M.LampOut(LAY)
    out.lat, out.ok = {1: 0.03, 2: 0.13}, {1: True, 2: True}
    assert out.align(1) == pytest.approx(0.10) and out.align(2) == 0.0
    out.lat[2] = 2.0
    assert out.align(1) == M.LAMP_ALIGN_MAX_S
    out.ok[2] = False                                                     # отвалившаяся лампа не тормозит остальных
    assert out.align(1) == 0.0


def test_stats_show_latency():
    out = M.LampOut(LAY)
    out.lat, out.ok = {1: 0.031, 2: 0.12}, {1: True, 2: True}
    assert "ответ ламп мс 1:31 2:120" in out.stats()


# --- резкость и затухание ламп (09.11 13:11) ------------------------------------------------------------------------

def test_gain_and_floor():
    env = M.Envelope()
    env.push(0.0, 0.5)
    hi = M.lamp_levels("sync", 0.0, env, gain=1.3, floor=M.LAMP_FLOOR)[1]
    lo = M.lamp_levels("sync", 0.0, env)[1]
    assert hi == pytest.approx(M.LAMP_FLOOR + (1 - M.LAMP_FLOOR) * 0.65) and hi > lo
    assert M.lamp_levels("sync", 0.0, env, gain=3.0)[1] == 1.0                     # не выше максимума
    assert min(M.lamp_levels("sync", 9.0, M.Envelope(), floor=M.LAMP_FLOOR).values()) == M.LAMP_FLOOR


def test_lamp_tuning_buttons_and_limits():
    s = M.State(lamps="sync", music=True)
    assert (s.lamp_gain, s.lamp_decay) == (1.3, 0.6)
    _, rows = M.panel(s, {})
    datas = [d for row in rows for _, d in row]
    assert {"amb:lg:-0.1", "amb:lg:+0.1", "amb:ld:-0.1", "amb:ld:+0.1"} <= set(datas)
    s, act = M.apply_button(s, "amb:lg:+0.1")
    assert act == "save" and s.lamp_gain == 1.4
    s, _ = M.apply_button(s, "amb:ld:-0.1")
    assert s.lamp_decay == 0.5
    for _ in range(40):
        s, _ = M.apply_button(s, "amb:ld:-0.1")
    assert s.lamp_decay == 0.15
    assert M.state_from({"lamp_gain": "x", "lamp_decay": 99}).lamp_decay == 2.0
    assert M.state_from({"lamp_gain": "x"}).lamp_gain == 1.3
    _, rows = M.panel(M.State(lamps="sync", music=False), {})               # без музыки -- ползунков нет
    assert "amb:lg:+0.1" not in [d for row in rows for _, d in row]


# --- канал музыки DP 28 (09.11 14:17: вариант C «плавно» выбран Оператором) -----------------------------------------

def test_sharp_fx_and_fallback_to_dp24():
    log: list = []
    out = M.LampOut(LAY, factory=lambda d: _Bulb(d, log, no28=d["pos"] == 2), gap=0.01)
    out.fx = "sharp"
    out.start()
    out.push(1, "aaaa")
    out.push(2, "bbbb")
    assert _wait(lambda: (1, "28", "0aaaa00000000") in log)              # резко -- переход 0
    out.push(2, "cccc")
    assert _wait(lambda: (2, "24", "cccc") in log)                        # DP 28 не принят дважды -> обычный цвет
    assert "без канала музыки: 2" in out.stats()
    out.close(restore=False)


def test_fx_buttons_and_state():
    assert M.State().lamp_fx == "smooth" and M.state_from({"lamp_fx": "x"}).lamp_fx == "smooth"
    _, rows = M.panel(M.State(lamps="sync"), {})
    datas = [d for row in rows for _, d in row]
    assert {"amb:lf:smooth", "amb:lf:sharp"} <= set(datas)
    s, act = M.apply_button(M.State(lamps="sync"), "amb:lf:sharp")
    assert act == "save" and s.lamp_fx == "sharp"


# --- лампы за картинкой (AMB_VIDEO 09.11 16:3x) -------------------------------------------------------------------

PROCESSED = {"layer1": {"left": {"0": {"r": 200, "g": 0, "b": 0}, "1": {"r": 100, "g": 0, "b": 0}},
                        "top": {"0": {"r": 0, "g": 90, "b": 0}},
                        "right": {"0": {"r": 0, "g": 0, "b": 60}, "x": {"r": "bad"}}}}


def test_video_rgb_mean_of_all_leds_and_empty():
    assert M.video_rgb(PROCESSED) == (111, 18, 8)                   # v8: вес по яркости; битая пропущена
    black = {"layer1": {"top": {"0": {"r": 0, "g": 0, "b": 0}}}}
    assert M.video_rgb(black) == (0, 0, 0)
    assert M.video_rgb({}) is None and M.video_rgb(None) is None and M.video_rgb({"layer1": {}}) is None


def test_video_levels_far_dimmer_tv_full():
    lv = M.video_levels(4)
    assert lv[4] == 1.0 and lv[1] < lv[2] < lv[3] < lv[4]
    assert M.video_levels(2) == {1: 0.9, 2: 1.0} and len(M.video_levels(6)) == 6


def test_source_state_buttons_and_panel():
    assert M.state_from({"source": "video"}).source == "video" and M.state_from({"source": "x"}).source == "own"
    s, act = M.apply_button(M.State(), "amb:src:video")
    assert s.source == "video" and act == "save"
    _, rows = M.panel(M.State(source="video", lamps="sync", music=True), {})
    data = [d for r in rows for _, d in r]
    assert "amb:src:own" in data and "amb:src:video" in data
    assert not any(d.startswith(("amb:c:", "amb:s:", "amb:p:", "amb:lg:", "amb:ld:")) for d in data)  # цвет считает ТВ
    assert "amb:lf:smooth" in data and "amb:l:off" in data
    _, rows = M.panel(M.State(source="own"), {})
    assert any(d.startswith("amb:c:") for r in rows for _, d in r)


class _SrcTV:
    def __init__(self, mode="internal"):
        self.calls, self.mode = [], mode

    def set_ambilight_mode(self, m):
        self.calls.append(("mode", m))
        self.mode = m

    def set_ambilight_style(self, style, setting=None):
        self.calls.append(("style", style, setting))
        self.mode = "internal"

    def get_ambilight_mode(self):
        return {"current": self.mode}


def test_enter_source_video_keeps_prev_setting_else_natural(monkeypatch):
    monkeypatch.setattr(M.time, "sleep", lambda _: None)
    tv = _SrcTV("expert")
    assert M.enter_source(tv, "video", {"config": {"styleName": "FOLLOW_VIDEO", "menuSetting": "VIVID"}}) == "internal"
    assert ("style", "FOLLOW_VIDEO", "VIVID") in tv.calls
    tv = _SrcTV()
    M.enter_source(tv, "video", {"config": {"styleName": "FOLLOW_COLOR", "menuSetting": "HOT_LAVA"}})
    assert ("style", "FOLLOW_VIDEO", M.VIDEO_SETTING) in tv.calls
    tv = _SrcTV()
    assert M.enter_source(tv, "own", {}) == "expert" and tv.calls == [("mode", "expert")]


class _VideoTV:
    """ТВ сам ведёт подсветку: мост только читает processed, ничего не пишет."""

    def __init__(self):
        self.mode, self.reads, self.posts, self.n, self.host = "internal", 0, 0, 0, "fake"

    def get_ambilight_config(self):
        return {"styleName": "FOLLOW_VIDEO", "menuSetting": "NATURAL"}

    def get_lounge_color(self):
        return {}

    def set_ambilight_power(self, on):
        pass

    def set_ambilight_mode(self, m):
        self.mode = m

    def get_ambilight_mode(self):
        return {"current": self.mode}

    def get_topology(self):
        return {"layers": 1, "left": 4, "top": 5, "right": 4, "bottom": 0}

    def set_ambilight_style(self, style, setting=None):
        self.mode = "internal"

    def _call(self, method, path, body=None):
        if method == "GET" and path == "ambilight/processed":
            self.reads += 1
            self.n += 1
            return {"layer1": {"left": {"0": {"r": 250 if self.n % 2 else 0, "g": 0, "b": 0}}}}
        self.posts += 1


def test_bridge_video_source_reads_tv_drives_lamps_no_mic_no_writes(monkeypatch, tmp_path):
    import sys
    import types

    import audio_bridge  # noqa: F401
    import tv_control
    opened = []

    class _St:
        def __init__(self, **kw):
            opened.append(1)

        def start(self): pass

        def stop(self): pass

        def close(self): pass
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(
        query_devices=lambda *a, **k: {"name": "mic", "max_input_channels": 2, "default_samplerate": 48000},
        InputStream=_St))
    monkeypatch.setitem(sys.modules, "audio_meter", types.SimpleNamespace(
        analyze=lambda *a, **k: types.SimpleNamespace(rms_dbfs=-30.0), SILENCE_DBFS=-80.0, dbfs=lambda x: -30.0))
    tv = _VideoTV()
    monkeypatch.setattr(tv_control, "TVClient", types.SimpleNamespace(from_env=lambda: tv))
    c, s = tmp_path / "amb_music.json", tmp_path / "amb_music_status.json"
    monkeypatch.setattr(M, "_paths", lambda: (c, s))
    monkeypatch.setattr(M, "session_id", lambda: 1)
    monkeypatch.setattr(M, "_load_env", lambda: None)
    pushed = []

    class _Lamps:
        def __init__(self, layout):
            self.lamps, self.fx = layout, "smooth"

        def start(self):
            return self

        def push(self, p, hsv):
            pushed.append((p, hsv))

        def stats(self):
            return "ok"

        def close(self, restore=True):
            return "restored"
    monkeypatch.setattr(M, "LampOut", _Lamps)
    monkeypatch.setattr(M, "load_lamp_layout", lambda base: [{"pos": i} for i in (1, 2, 3, 4)])
    M.save_state(c, M.State(on=True, music=True, source="video", lamps="sync"))
    th = threading.Thread(target=M.run, daemon=True)
    th.start()
    # ожидание условия, не фиксированная пауза (CI 20.11: за 2 с медленный раннер успел 1 кадр); потолок 15 с
    assert _wait(lambda: M.read_json(s).get("frames", 0) >= 3 and tv.reads >= 3, 15.0)
    st = M.read_json(s)
    M.save_state(c, M.State(on=False))
    th.join(5)
    assert not th.is_alive()
    assert opened == [] and tv.posts == 0 and tv.reads >= 3          # микрофон закрыт, в ТВ не пишется
    assert st["tv_mode"] == "internal" and st["source"] == "video" and st["frames"] >= 3
    assert {p for p, _ in pushed} == {1, 2, 3, 4}
    near = [h for p, h in pushed if p == 4 and h[:4] == "0000" and h[4:8] == "03e8"]
    far = [h for p, h in pushed if p == 1 and h[:4] == "0000" and h[4:8] == "03e8"]
    assert near and far and int(max(far)[8:], 16) < int(max(near)[8:], 16)   # дальняя тусклее


def test_lamp_wb_keeps_brightness_lifts_green():
    assert M.lamp_wb((200, 200, 200)) == (160, 200, 148)                    # белый к зелёному, яркость та же
    r, g, b = M.lamp_wb((0, 0, 255))
    assert b == 255 and r == g == 0                                     # чистый цвет не меняется
    assert M.lamp_wb((0, 0, 0)) == (0, 0, 0)



# --- v8 (09.11 19:16, lamp_diag EXEC): поток nowait + контрольный кадр раз в LAMP_CHECK_S ---------------------------

class _V8Bulb:
    """Лампа v3.5 как в EXEC 19:16: DP 28 подтверждается ПУСТЫМ ACK (r=None, cmd_retcode=0), без dps.
    plan(n_conn, k-й контрольный кадр на соединении) -> подтвердить?"""

    def __init__(self, n, log, plan=lambda n, k: True, buffered=0):
        self.n, self.log, self.plan, self.k = n, log, plan, 0
        self.cmd_retcode, self.raw_recv, self.buf, self.timeout = None, [], buffered, 2

    def status(self):
        return {"dps": {"21": "white", "24": "000003e803e8"}}

    def set_socketTimeout(self, t):
        self.timeout = t

    def receive(self):
        self.log.append((self.n, "rx", self.buf))
        if self.buf > 0:
            self.buf -= 1
            self.raw_recv = [object()]
        return None

    def set_value(self, k, v, nowait=False):
        self.log.append((self.n, k + ("~" if nowait else ""), v))
        if not nowait and k == "28":
            self.log.append((self.n, "tmo", self.timeout))
        if nowait:
            self.buf += 1
            return None
        if k == "28":
            self.k += 1
            if not self.plan(self.n, self.k):
                self.cmd_retcode = None
                return None
        self.cmd_retcode = 0
        return None


def _v8(plan=lambda n, k: True, check=0.15, retry=0.05, gap=0.01, buffered=0):
    log, opened = [], []

    def fac(d):
        opened.append(1)
        return _V8Bulb(len(opened), log, plan, buffered)
    return M.LampOut(LAY[:1], factory=fac, gap=gap, retry=retry, check=check), log, opened


def _feed(out, n, tag="c", dt=0.02):
    for i in range(n):
        out.push(1, f"{tag}{i:03d}")
        time.sleep(dt)


def test_null_ack_retcode0_is_confirmed():
    assert M._confirmed(type("B", (), {"cmd_retcode": 0})(), None)
    assert not M._confirmed(type("B", (), {"cmd_retcode": None})(), None)
    assert not M._confirmed(None, {"Error": "x", "Err": "905"}) and M._confirmed(None, {"dps": {}})


def test_stream_nowait_first_frame_confirmed_then_periodic_check():
    out, log, opened = _v8(check=0.3)
    out.start()
    out.push(1, "a000")
    assert _wait(lambda: (1, "28", "1a00000000000") in log)           # первый кадр соединения -- с подтверждением
    _feed(out, 5)
    assert _wait(lambda: any(k == "28~" for _, k, _ in log))           # дальше -- без ожидания ответа
    n28 = sum(1 for _, k, _ in log if k == "28")
    assert _wait(lambda: sum(1 for _, k, _ in log if k == "28") > n28, 15.0)   # тишина: контрольный кадр повторяет цвет
    assert out.fails == 0 and out.dp24_count == 0 and out.dp28_seen.get(1) and out.ok[1]
    assert "без ожидания" in out.stats()
    out.close(restore=False)


def test_drain_before_check_frame():
    out, log, opened = _v8(check=0.1)
    out.start()
    out.push(1, "a000")
    _feed(out, 6)
    seen = {}

    def _check_after_rx():                                            # индекс вычитки фиксируется вместе с условием
        snap = list(log)
        rx = [i for i, e in enumerate(snap) if e[1] == "rx"]
        if rx and any(k == "28" for _, k, _ in snap[rx[-1] + 1:]):
            seen["log"], seen["i"] = snap, rx[-1]
            return True
        return False
    assert _wait(_check_after_rx, 15.0)
    log, i_rx = seen["log"], seen["i"]
    assert any(k == "28" for _, k, _ in log[i_rx + 1:])                # после вычитки -- контрольный кадр
    assert log[i_rx][2] == 0                                           # буфер вычитан до пустого
    out.close(restore=False)


def test_seen_lamp_unconfirmed_checks_reconnect_not_dp24():
    out, log, opened = _v8(plan=lambda n, k: n > 1 or k == 1, check=0.05)
    out.start()
    out.push(1, "a000")
    assert _wait(lambda: out.dp28_seen.get(1))
    _feed(out, 30)
    assert _wait(lambda: len(opened) >= 2, 3)
    assert out.dp24_count == 0 and not any(k.startswith("24") for _, k, _ in log)
    out.close(restore=False)


def test_burst_across_reconnect_stays_on_dp28():
    # соед.1: подтвердил 1-й, дальше молчит -> переподключение; соед.2: ещё 1 молчит, потом канал жив
    out, log, opened = _v8(plan=lambda n, k: (n == 1 and k == 1) or (n == 2 and k > 1), check=0.03)
    out.start()
    out.push(1, "a000")
    assert _wait(lambda: out.dp28_seen.get(1))
    _feed(out, 40)
    assert _wait(lambda: len(opened) >= 2, 3)
    _feed(out, 40, "g")                                                # на соед.2 канал ожил -- поток идёт
    assert _wait(lambda: any(n == 2 and k == "28~" for n, k, _ in log), 3)
    assert out.dp24_count == 0 and "без канала музыки" not in out.stats() and len(opened) == 2
    out.close(restore=False)


def test_never_confirmed_goes_dp24_and_retries_dp28(monkeypatch):
    monkeypatch.setattr(M, "LAMP_DP28_RETRY_S", 0.3)
    t_ok = time.monotonic() + 0.25
    out, log, opened = _v8(plan=lambda n, k: time.monotonic() >= t_ok, check=0.03)
    out.start()
    out.push(1, "a000")
    assert _wait(lambda: out.dp24_count >= 1)
    assert "без канала музыки: 1" in out.stats()
    _feed(out, 60, "d")
    assert _wait(lambda: out.dp28_seen.get(1), 3) and out.colour_dp.get(1) is None
    out.close(restore=False)


def test_dp28_dead_after_seen_falls_to_dp24_not_reconnect_loop(monkeypatch):
    monkeypatch.setattr(M, "LAMP_DP28_RETRY_S", 60.0)
    out, log, opened = _v8(plan=lambda n, k: n == 1 and k == 1, check=0.02)
    out.start()
    out.push(1, "a000")
    assert _wait(lambda: out.dp28_seen.get(1))
    for i in range(200):
        out.push(1, f"e{i:03d}")
        time.sleep(0.01)
        if out.dp24_count:
            break
    assert _wait(lambda: out.dp24_count == 1, 3)
    assert len(opened) == 1 + M.LAMP_DP28_DEAD
    out.close(restore=False)


def test_nowait_socket_error_reconnects():
    log = []
    out = M.LampOut(LAY[:1], factory=lambda d: _Bulb(d, log, fail=True), gap=0.01, retry=0.05)
    out.start()
    out.push(1, "b001")
    assert _wait(lambda: out.fails >= 2)
    assert out.colour_dp == {} and out.dp24_count == 0
    out.close(restore=False)


def test_frame_rate_capped_latest_wins():
    out, log, opened = _v8(check=10.0, gap=0.1)
    out.start()
    out.push(1, "a000")
    assert _wait(lambda: out.sent >= 1)
    t0 = time.monotonic()
    _feed(out, 25, "f", dt=0.01)                                       # 25 целей за ~0.25 с
    assert _wait(lambda: [v for _, k, v in log if k == "28~"][-1:] == ["1f02400000000"], 15.0)
    frames = [v for _, k, v in log if k == "28~"]
    assert len(frames) <= int((time.monotonic() - t0) / 0.1) + 2      # не чаще gap
    assert frames[-1] == "1f02400000000"                               # последний кадр побеждает
    out.close(restore=False)


def test_check_frame_short_timeout_then_restored():
    out, log, opened = _v8(check=0.05)
    out.start()
    out.push(1, "a000")
    _feed(out, 5)
    assert _wait(lambda: any(k == "tmo" for _, k, _ in log) and out.dev[1].timeout == 2, 15.0)
    tm = [v for _, k, v in log if k == "tmo"]
    assert tm and all(v == M.LAMP_CHECK_TIMEOUT_S for v in tm)
    assert out.dev[1].timeout == 2                                     # после контрольного -- обратно 2 с
    out.close(restore=False)
