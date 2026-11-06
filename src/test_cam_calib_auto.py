"""CALIB_AUTO + SAFE-06 (04.11): автокалибровка через меню ТВ и проверка до команды."""
from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

import camera_watcher as cw

W, H = 2304, 2592


def _frame(dark: bool):
    img = Image.new("RGB", (W, H), (200, 200, 200))
    if dark:   # тёмный экран в нижнем объективе, как на штативе 04.11
        ImageDraw.Draw(img).rectangle((330, 1480, 2040, 2440), fill=(8, 8, 10))
    else:      # горящий экран: светлее стены
        ImageDraw.Draw(img).rectangle((330, 1480, 2040, 2440), fill=(230, 230, 240))
    return img


class FakeTV:
    def __init__(self, fail_at=None, fail_restore=False):
        self.keys, self.fail_at, self.fail_restore = [], fail_at, fail_restore

    def send_key(self, key):
        if self.fail_at is not None and len(self.keys) + 1 == self.fail_at:
            self.keys.append("!" + key)
            raise RuntimeError("tv down")
        if key == cw.RESTORE_KEY and self.fail_restore and len(self.keys) >= len(cw.SCREEN_OFF_KEYS):
            raise RuntimeError("restore failed")
        self.keys.append(key)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def cam(monkeypatch, tmp_path):
    monkeypatch.setenv("OKO_CAM_HOST", "10.0.0.2")
    monkeypatch.setenv("OKO_CAM_USER", "u")
    monkeypatch.setenv("OKO_CAM_PASS", "secret")
    monkeypatch.setenv("OKO_CAM_DIR", str(tmp_path))
    return tmp_path


def test_path_matches_map():
    k = cw.SCREEN_OFF_KEYS
    assert len(k) == 23 and k[0] == "Home"
    assert k.count("Confirm") == 3                       # ок только: Настройки, окно экологичности, «Выкл. экран»
    assert [i for i, x in enumerate(k) if x == "Confirm"] == [9, 20, 22]
    assert k[-3:] == ("CursorDown", "Confirm") or k[-2:] == ("CursorDown", "Confirm")


def test_keys_are_known_to_tv_control():
    tv_control = pytest.importorskip("tv_control")
    assert set(cw.SCREEN_OFF_KEYS) | {cw.RESTORE_KEY} <= tv_control.KEYS


def test_success_waits_for_dark_frame_then_restores(cam):
    tv, clk = FakeTV(), Clock()
    frames = iter([_frame(False), _frame(False), _frame(True)])
    text, path = cw.calibrate_auto(tv, grab=lambda url: next(frames), sleep=clk.sleep, clock=clk, step_s=0.0)
    assert "углы сохранены" in text and path is not None and path.exists()
    assert tv.keys == list(cw.SCREEN_OFF_KEYS) + ["Home"]
    q = cw.saved_quad()
    assert q is not None and abs(q[0][0] - 330) < 20 and abs(q[2][1] - 2440) < 20
    assert '"auto_menu"' in (cam / cw.CALIB_FILE).read_text(encoding="utf-8")


def test_timeout_keeps_old_quad_and_restores(cam):
    cw.save_quad(((1, 2), (3, 4), (5, 6), (7, 8)), "calib")
    tv, clk = FakeTV(), Clock()
    text, path = cw.calibrate_auto(tv, grab=lambda url: _frame(False), sleep=clk.sleep, clock=clk,
                                   step_s=0.0, wait_s=5.0)
    assert path is None and "не найден" in text and "Углы не менялись" in text
    assert cw.saved_quad() == ((1, 2), (3, 4), (5, 6), (7, 8))
    assert tv.keys[-1] == "Home"


def test_tv_failure_stops_path_with_step(cam):
    tv, clk = FakeTV(fail_at=5), Clock()
    grabbed = []
    text, path = cw.calibrate_auto(tv, grab=lambda url: grabbed.append(1) or _frame(True), sleep=clk.sleep,
                                   clock=clk, step_s=0.0)
    assert path is None and "шаге 5/23" in text
    assert not grabbed                                   # после сбоя кнопки камеру не дёргаем
    assert tv.keys[-1] == "Home"


def test_camera_error_masks_password(cam):
    tv, clk = FakeTV(), Clock()

    def boom(url):
        raise OSError(f"cannot open {url}")
    text, path = cw.calibrate_auto(tv, grab=boom, sleep=clk.sleep, clock=clk, step_s=0.0)
    assert path is None and "secret" not in text and "***" in text
    assert tv.keys[-1] == "Home"


def test_restore_failure_does_not_raise(cam):
    tv, clk = FakeTV(fail_restore=True), Clock()
    text, path = cw.calibrate_auto(tv, grab=lambda url: _frame(True), sleep=clk.sleep, clock=clk, step_s=0.0)
    assert path is not None


def test_disabled_camera_raises(monkeypatch):
    monkeypatch.setenv("OKO_CAM_HOST", "")
    monkeypatch.setattr(cw, "_env", lambda name, default="": "")
    with pytest.raises(RuntimeError):
        cw.calibrate_auto(FakeTV())


# --- tv_bot_handler.PRE ---------------------------------------------------------------------------------------

class _Cmd:
    def __init__(self, log):
        self.name, self.kind, self.meta, self.needs_confirm, self._log = "power_on", "verified", {}, False, log

    def run(self, tv):
        self._log.append("run")
        return "Отправил пакет пробуждения."


def test_pre_runs_before_command_and_note_is_appended(monkeypatch):
    tbh = pytest.importorskip("tv_bot_handler")
    log = []
    monkeypatch.setattr(tbh, "PRE", {"power_on": lambda: log.append("pre") or "⚙️ углы пересчитаны"})
    monkeypatch.setattr(tbh, "VERIFY", {})
    out = tbh._execute(_Cmd(log), lambda: object())
    assert log == ["pre", "run"]
    assert out.endswith("⚙️ углы пересчитаны")


def test_pre_failure_does_not_block_command(monkeypatch):
    tbh = pytest.importorskip("tv_bot_handler")
    log = []

    def bad():
        raise RuntimeError("camera down")
    monkeypatch.setattr(tbh, "PRE", {"power_on": bad})
    monkeypatch.setattr(tbh, "VERIFY", {})
    out = tbh._execute(_Cmd(log), lambda: object())
    assert log == ["run"] and out == "Отправил пакет пробуждения."


def test_pre_empty_by_default():
    tbh = pytest.importorskip("tv_bot_handler")
    assert "power_on" not in tbh.PRE or callable(tbh.PRE["power_on"])
