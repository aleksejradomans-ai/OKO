"""CAM_TARGET: reference picture for the camera on the main monitor (full screen; Esc closes it).

What is shown: a solid background of the given colour; four large corner markers and a centre marker (white square in
a black frame, geometry -- src/cam_markers.layout, the same module detects them in a camera frame); a frame counter
and the wall-clock time with milliseconds, large, to measure the delay "shown -> seen by the camera".

Modes:
  --color R,G,B        background colour (default 40,40,40: dark, the markers are the brightest objects)
  --markers            draw the five markers
  --sequence LIST      change the background along "R,G,B;R,G,B;..." every --step-ms ms (default 1000), --cycles times;
                       each change is written with its show time into the journal (JSON)
  --exit-after S       close by itself after S seconds
  --tag NAME           journal name target_<NAME>.json (letters, digits, _ and -), default: the start time
Journal: data/cam/target/target_<tag>.json -- screen size, marker layout, every background change with its show time
(time.time() right after the window was updated; the real display is up to one monitor refresh later). The file is
rewritten after every change, so a crash leaves the part shown so far. Nothing is written outside data/cam/target/;
no network. Older files in that folder are trimmed to the last KEEP_FILES.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import cam_markers as M  # noqa: E402

OUT = ROOT / "data" / "cam" / "target"
TICK_MS = 40
KEEP_FILES = 40
DEFAULT_RGB = (40, 40, 40)
TAG_RX = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_STEPS = 200
MIN_STEP_MS = 100


def parse_rgb(s: str) -> tuple[int, int, int]:
    parts = [p.strip() for p in str(s).split(",")]
    if len(parts) != 3:
        raise ValueError(f"colour {s!r}: need R,G,B")
    vals = tuple(int(p) for p in parts)
    if any(not 0 <= v <= 255 for v in vals):
        raise ValueError(f"colour {s!r}: each part 0..255")
    return vals  # type: ignore[return-value]


def parse_sequence(s: str) -> list[tuple[int, int, int]]:
    items = [x for x in str(s).split(";") if x.strip()]
    if not items:
        raise ValueError("empty sequence")
    if len(items) > MAX_STEPS:
        raise ValueError(f"sequence longer than {MAX_STEPS}")
    return [parse_rgb(x) for x in items]


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="cam_target", description="reference picture for the camera (Esc closes)")
    ap.add_argument("--color", type=parse_rgb, default=DEFAULT_RGB)
    ap.add_argument("--markers", action="store_true")
    ap.add_argument("--sequence", type=parse_sequence, default=None)
    ap.add_argument("--step-ms", type=int, default=1000)
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--exit-after", type=float, default=None)
    ap.add_argument("--tag", default="")
    a = ap.parse_args(argv)
    if a.step_ms < MIN_STEP_MS:
        ap.error(f"--step-ms must be >= {MIN_STEP_MS}")
    if a.cycles < 1 or a.cycles > 100:
        ap.error("--cycles must be 1..100")
    if a.exit_after is not None and not 0 < a.exit_after <= 3600:
        ap.error("--exit-after must be in (0, 3600]")
    if a.tag and not TAG_RX.match(a.tag):
        ap.error("--tag: letters, digits, _ and - only (max 40)")
    return a


def target_dir(root: Path = ROOT, out: Optional[Path] = None) -> Path:
    """The only folder this tool writes to: <root>/data/cam/target (a given `out` must resolve inside it)."""
    base = (root / "data" / "cam" / "target").resolve()
    p = (out or base).resolve()
    if p != base and base not in p.parents:
        raise ValueError("cam_target writes only into data/cam/target")
    return p


def trim(d: Path, keep: int = KEEP_FILES) -> int:
    files = sorted((p for p in d.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
    gone = 0
    for p in files[:max(0, len(files) - keep)]:
        try:
            p.unlink()
            gone += 1
        except OSError:
            pass
    return gone


class Journal:
    def __init__(self, path: Path, head: dict):
        self.path = path
        self.data = dict(head, events=[], ended=None, exit=None)

    def event(self, **kw):
        self.data["events"].append(kw)
        self.save()

    def end(self, why: str, t: float):
        self.data["ended"], self.data["exit"] = t, why
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def clock_text(t: float) -> str:
    d = _dt.datetime.fromtimestamp(t)
    return d.strftime("%H:%M:%S.") + f"{d.microsecond // 1000:03d}"


class TargetRun:
    """Logic of the window (no tkinter here): background / sequence / text / exit / journal."""

    def __init__(self, ui, args: argparse.Namespace, out: Path, wall: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic):
        self.ui, self.a, self.out, self.wall, self.mono = ui, args, out, wall, mono
        self.seq = list(args.sequence or [])
        self.frame = 0
        self.step = -1
        self.done = False
        self.t0 = 0.0
        self.journal: Optional[Journal] = None

    def colour_at(self, k: int):
        return self.seq[k % len(self.seq)] if self.seq else tuple(self.a.color)

    def text(self) -> str:
        s = f"{self.frame:06d}  {clock_text(self.wall())}"
        if self.seq:
            s += f"  S{self.step % len(self.seq) + 1}/{len(self.seq)}"
        return s

    def start(self):
        w, h = self.ui.size()
        lay = M.layout(w, h)
        tag = self.a.tag or _dt.datetime.fromtimestamp(self.wall()).strftime("%Y%m%d_%H%M%S")
        self.out.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.out / f"target_{tag}.json", {
            "schema": "cam_target/1", "screen": [w, h], "markers": bool(self.a.markers),
            "layout": {k: {"white": list(v["white"]), "frame": list(v["frame"])} for k, v in lay.items()},
            "normalised": {k: list(v) for k, v in M.normalised(w, h).items()},
            "sequence": [list(c) for c in self.seq], "step_ms": self.a.step_ms if self.seq else None,
            "cycles": self.a.cycles if self.seq else None, "exit_after": self.a.exit_after,
            "started": self.wall(),
        })
        trim(self.out)
        self.ui.draw(self.colour_at(0), lay if self.a.markers else None, M.text_box(w, h), self.text())
        self.ui.bind_key(self.key)
        self.t0 = self.mono()
        self._show(0)
        self.ui.after(TICK_MS, self.tick)

    def _show(self, k: int):
        self.step = k
        rgb = self.colour_at(k)
        self.ui.set_bg(rgb)
        self.ui.set_text(self.text())
        self.ui.flush()
        self.journal.event(step=k, rgb=list(rgb), t_show=self.wall(), frame=self.frame)

    def tick(self):
        if self.done:
            return
        self.frame += 1
        el = self.mono() - self.t0
        if self.a.exit_after is not None and el >= self.a.exit_after:
            return self.stop("timeout")
        if self.seq:
            k = int(el * 1000 // self.a.step_ms)
            if k >= len(self.seq) * self.a.cycles:
                return self.stop("sequence_end")
            if k != self.step:
                self._show(k)
        self.ui.set_text(self.text())
        self.ui.after(TICK_MS, self.tick)

    def key(self, name: str):
        if name == "Escape":
            self.stop("esc")

    def stop(self, why: str):
        if self.done:
            return
        self.done = True
        if self.journal is not None:
            self.journal.end(why, self.wall())
        self.ui.close()


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


class TkUI:
    """Full-screen tkinter window on the main monitor."""

    def __init__(self):
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)      # real pixels, not scaled ones
            except Exception:
                pass
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.title("cam_target")
        self.root.geometry("+0+0")
        self.root.attributes("-fullscreen", True)
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            self.root.configure(cursor="none")
        except tk.TclError:
            pass
        self.c = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.c.pack(fill="both", expand=True)
        self.root.update()
        self.bg = self.txt = None

    def size(self):
        w, h = self.root.winfo_width(), self.root.winfo_height()
        if w < 64 or h < 64:
            w, h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        return w, h

    def draw(self, rgb, lay, tbox, text):
        w, h = self.size()
        self.c.delete("all")
        self.bg = self.c.create_rectangle(0, 0, w, h, fill=_hex(rgb), width=0)
        for v in (lay or {}).values():
            self.c.create_rectangle(*v["frame"], fill="#000000", width=0)
            self.c.create_rectangle(*v["white"], fill="#ffffff", width=0)
        x0, y0, x1, y1 = tbox
        self.txt = self.c.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=text, fill=_hex(M.TEXT_RGB),
                                      font=("Consolas", -(y1 - y0)))

    def set_bg(self, rgb):
        self.c.itemconfigure(self.bg, fill=_hex(rgb))

    def set_text(self, s):
        self.c.itemconfigure(self.txt, text=s)

    def flush(self):
        self.root.update_idletasks()

    def after(self, ms, fn):
        self.root.after(ms, fn)

    def bind_key(self, fn):
        self.root.bind("<Key>", lambda e: fn(e.keysym))

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def main(argv: Optional[list] = None) -> int:
    a = parse_args(argv)
    out = target_dir()
    ui = TkUI()
    run = TargetRun(ui, a, out)
    run.start()
    print(f"TARGET_JOURNAL {run.journal.path.name}", flush=True)
    ui.run()
    run.stop("window_closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
