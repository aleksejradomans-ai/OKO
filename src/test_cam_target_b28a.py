"""B28a: tools/cam_target.py -- arguments, write guard, sequence timing and the show-time journal (no window)."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("cam_target_b28a", ROOT / "tools" / "cam_target.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


T = load()


class FakeUI:
    def __init__(self, w=1920, h=1080):
        self.w, self.h = w, h
        self.calls, self.bg, self.text, self.closed, self.keyfn, self.pending = [], None, "", 0, None, []

    def size(self):
        return self.w, self.h

    def draw(self, rgb, lay, tbox, text):
        self.calls.append(("draw", tuple(rgb), lay is not None, tbox))
        self.bg, self.text = tuple(rgb), text

    def set_bg(self, rgb):
        self.bg = tuple(rgb)
        self.calls.append(("bg", tuple(rgb)))

    def set_text(self, s):
        self.text = s

    def flush(self):
        pass

    def after(self, ms, fn):
        self.pending.append((ms, fn))

    def bind_key(self, fn):
        self.keyfn = fn

    def close(self):
        self.closed += 1


class Clock:
    def __init__(self):
        self.t = 0.0

    def mono(self):
        return self.t

    def wall(self):
        return 1795219200.0 + self.t


def drive(run, ui, clock, until_s, step_s=0.04):
    run.start()
    while not run.done and clock.t < until_s:
        clock.t = round(clock.t + step_s, 6)
        ms, fn = ui.pending.pop(0)
        assert ms == T.TICK_MS
        fn()


def make(tmp_path, argv, **ui):
    a = T.parse_args(argv)
    u, c = FakeUI(**ui), Clock()
    out = tmp_path / "data" / "cam" / "target"
    return T.TargetRun(u, a, out, wall=c.wall, mono=c.mono), u, c


# ------------------------------------------------------------------ arguments
def test_parse_colour_and_sequence():
    assert T.parse_rgb("255, 0,10") == (255, 0, 10)
    for bad in ("1,2", "1,2,3,4", "1,2,256", "a,b,c", "-1,0,0"):
        with pytest.raises(ValueError):
            T.parse_rgb(bad)
    assert T.parse_sequence("200,0,0;0,160,0;") == [(200, 0, 0), (0, 160, 0)]
    with pytest.raises(ValueError):
        T.parse_sequence(" ; ")
    with pytest.raises(ValueError):
        T.parse_sequence(";".join(["1,1,1"] * (T.MAX_STEPS + 1)))


def test_parse_args_defaults_and_limits():
    a = T.parse_args([])
    assert a.color == T.DEFAULT_RGB and not a.markers and a.sequence is None and a.exit_after is None
    for bad in (["--step-ms", "50"], ["--cycles", "0"], ["--exit-after", "0"], ["--tag", "../x"], ["--tag", "a b"]):
        with pytest.raises(SystemExit):
            T.parse_args(bad)


def test_writes_only_into_data_cam_target(tmp_path):
    base = T.target_dir(tmp_path)
    assert base == (tmp_path / "data" / "cam" / "target").resolve()
    assert T.target_dir(tmp_path, base / "sub") == (base / "sub")
    for bad in (tmp_path / "data" / "cam", tmp_path / "elsewhere", base / ".." / "x"):
        with pytest.raises(ValueError):
            T.target_dir(tmp_path, bad)


def test_no_window_library_imported_at_module_level():
    src = (ROOT / "tools" / "cam_target.py").read_text(encoding="utf-8")
    top = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert not any("tkinter" in ln for ln in top)
    assert "socket" not in src and "httpx" not in src and "urllib" not in src


# ------------------------------------------------------------------ run logic
def test_static_colour_with_markers_and_exit_after(tmp_path):
    run, ui, c = make(tmp_path, ["--color", "10,20,30", "--markers", "--exit-after", "1", "--tag", "t1"])
    drive(run, ui, c, 5)
    assert run.done and ui.closed == 1
    assert ui.calls[0][0] == "draw" and ui.calls[0][1] == (10, 20, 30) and ui.calls[0][2] is True
    j = json.loads((tmp_path / "data" / "cam" / "target" / "target_t1.json").read_text(encoding="utf-8"))
    assert j["schema"] == "cam_target/1" and j["screen"] == [1920, 1080] and j["markers"] is True
    assert j["exit"] == "timeout" and len(j["events"]) == 1 and j["events"][0]["rgb"] == [10, 20, 30]
    assert set(j["layout"]) == {"tl", "tr", "br", "bl", "c"} and j["normalised"]["c"] == [0.5, 0.5]
    assert 0.99 <= c.t <= 1.05


def test_sequence_show_times_and_end(tmp_path):
    run, ui, c = make(tmp_path, ["--sequence", "200,0,0;0,160,0;0,0,200", "--step-ms", "500", "--cycles", "2",
                                 "--tag", "seq"])
    drive(run, ui, c, 10)
    j = json.loads((tmp_path / "data" / "cam" / "target" / "target_seq.json").read_text(encoding="utf-8"))
    ev = j["events"]
    assert [e["step"] for e in ev] == [0, 1, 2, 3, 4, 5]
    assert [e["rgb"] for e in ev] == [[200, 0, 0], [0, 160, 0], [0, 0, 200]] * 2
    t = [e["t_show"] - 1795219200.0 for e in ev]
    for k, x in enumerate(t):                               # each change within one tick after its slot
        assert k * 0.5 <= x + 1e-9 <= k * 0.5 + T.TICK_MS / 1000 + 1e-9
    assert all(b > a for a, b in zip(t, t[1:]))
    assert j["exit"] == "sequence_end" and j["step_ms"] == 500 and j["cycles"] == 2
    assert 3.0 <= c.t <= 3.05 and ui.closed == 1
    assert "S3/3" in ui.text or "S1/3" in ui.text


def test_escape_stops_once_and_text_has_frame_and_time(tmp_path):
    run, ui, c = make(tmp_path, ["--tag", "esc"])
    run.start()
    for _ in range(3):
        c.t += 0.04
        ui.pending.pop(0)[1]()
    assert ui.text.startswith("000003  ") and ":" in ui.text and "." in ui.text
    ui.keyfn("a")
    assert not run.done
    ui.keyfn("Escape")
    ui.keyfn("Escape")
    run.stop("window_closed")
    assert run.done and ui.closed == 1
    j = json.loads((tmp_path / "data" / "cam" / "target" / "target_esc.json").read_text(encoding="utf-8"))
    assert j["exit"] == "esc" and j["ended"] is not None
    assert not (tmp_path / "data" / "cam" / "target" / "target_esc.json.tmp").exists()


def test_trim_keeps_last_files(tmp_path):
    d = tmp_path / "data" / "cam" / "target"
    d.mkdir(parents=True)
    for i in range(45):
        p = d / f"f{i:02d}.json"
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))
    assert T.trim(d, keep=40) == 5
    names = sorted(p.name for p in d.iterdir())
    assert len(names) == 40 and names[0] == "f05.json"


def test_clock_text_format():
    s = T.clock_text(1795219200.123)
    assert len(s) == 12 and s[2] == ":" and s[5] == ":" and s.endswith(".123")
