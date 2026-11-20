"""CAM_MARKERS: reference target on a monitor and its detector in a camera frame (no change of camera_watcher).

The target (tools/cam_target.py) shows a solid background, four large corner markers and a centre marker: each marker
is a white square inside a black frame, so it stays a high-contrast blob on any background colour. The detector finds
the four corner markers in a camera frame, tells in which lens (a frame of the two-lens camera holds two pictures one
above the other), returns their coordinates, the perspective (homography target -> frame, side ratios) and the mean
colour of the background around the centre. This is the reference for the PTZ step: "turn" -> where are the markers now.

Pure numpy + PIL (both already used by the project), no OpenCV, no network, no files. Coordinates in the result are
pixels of the FULL frame that was given (times `upscale`, if the caller passed an already reduced frame).
"""
from __future__ import annotations

import itertools
import math
from collections import deque
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------------------------------- layout
MARKER = 0.09          # white square side, share of min(screen w, h)
BORDER = 0.30          # black frame width, share of the white side
MARGIN = 0.035         # gap between the screen edge and the black frame, share of min(w, h)
TEXT_Y = 0.80          # centre line of the frame number / time text, share of the screen height
TEXT_H = 0.075         # text height, share of the screen height
TEXT_RGB = (128, 128, 128)
SAMPLE_PTS = ((0.35, 0.35), (0.65, 0.35), (0.65, 0.65), (0.35, 0.65))   # background around the centre (normalised)
CORNERS = ("tl", "tr", "br", "bl")


def layout(w: int, h: int) -> dict:
    """Marker rectangles on a w x h screen: name -> {'white': (x0, y0, x1, y1), 'frame': (...), 'c': (cx, cy)}."""
    if w < 64 or h < 64:
        raise ValueError("screen too small for the target")
    m = min(w, h)
    s = max(8, int(round(MARKER * m)))
    b = max(2, int(round(BORDER * s)))
    g = max(2, int(round(MARGIN * m)))
    half = s / 2.0
    off = g + b + half
    centres = {"tl": (off, off), "tr": (w - off, off), "br": (w - off, h - off), "bl": (off, h - off),
               "c": (w / 2.0, h / 2.0)}
    out = {}
    for k, (cx, cy) in centres.items():
        wx0, wy0 = int(round(cx - half)), int(round(cy - half))
        white = (wx0, wy0, wx0 + s, wy0 + s)
        frame = (white[0] - b, white[1] - b, white[2] + b, white[3] + b)
        out[k] = {"white": white, "frame": frame, "c": (cx, cy)}     # intended centre (the square is rounded to px)
    return out


def text_box(w: int, h: int) -> tuple[int, int, int, int]:
    """Area kept for the frame number / time text (between the bottom markers, below the centre marker)."""
    lay = layout(w, h)
    th = int(round(TEXT_H * h))
    cy = int(round(TEXT_Y * h))
    x0 = lay["bl"]["frame"][2] + int(0.05 * w)
    x1 = lay["br"]["frame"][0] - int(0.05 * w)
    return (x0, cy - th // 2, x1, cy + th // 2)


def normalised(w: int, h: int) -> dict:
    """Marker centres in screen units [0..1]."""
    return {k: (v["c"][0] / w, v["c"][1] / h) for k, v in layout(w, h).items()}


def render(w: int, h: int, rgb=(40, 40, 40), markers: bool = True) -> np.ndarray:
    """The target as an RGB array (what the window shows, without the text) -- for tests and a saved preview."""
    img = np.empty((h, w, 3), np.uint8)
    img[:, :] = np.array(rgb, np.uint8)
    if markers:
        for v in layout(w, h).values():
            x0, y0, x1, y1 = v["frame"]
            img[y0:y1, x0:x1] = 0
            x0, y0, x1, y1 = v["white"]
            img[y0:y1, x0:x1] = 255
    return img


# ---------------------------------------------------------------------------------------------------- geometry
def homography(src, dst) -> np.ndarray:
    """3x3 H with dst ~ H @ src for 4+ point pairs (DLT, normalised by H[2,2])."""
    a = []
    for (x, y), (u, v) in zip(src, dst):
        a.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        a.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, _, vt = np.linalg.svd(np.asarray(a, float))
    hm = vt[-1].reshape(3, 3)
    if abs(hm[2, 2]) < 1e-12:
        raise ValueError("degenerate point set")
    return hm / hm[2, 2]


def apply_h(hm: np.ndarray, pts) -> list[tuple[float, float]]:
    out = []
    for x, y in pts:
        u, v, w = hm @ np.array([x, y, 1.0])
        out.append((float(u / w), float(v / w)))
    return out


def poly_area(pts) -> float:
    s = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def _convex(pts) -> bool:
    sign = 0
    n = len(pts)
    for i in range(n):
        (x0, y0), (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        cr = (x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1)
        if abs(cr) < 1e-9:
            return False
        s = 1 if cr > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return True


def order_corners(pts) -> list[tuple[float, float]]:
    """4 points -> TL, TR, BR, BL (clockwise on the image, starting at the one nearest the top-left)."""
    cx = sum(p[0] for p in pts) / 4.0
    cy = sum(p[1] for p in pts) / 4.0
    ring = sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))     # y down -> clockwise
    k = min(range(4), key=lambda i: ring[i][0] + ring[i][1])
    return ring[k:] + ring[:k]


MIN_ANGLE, MAX_ANGLE = 35.0, 145.0   # interior angles of the corner quad (a screen seen at an angle, not a sliver)


def _angles_ok(q) -> bool:
    for i in range(4):
        p0, p1, p2 = q[i - 1], q[i], q[(i + 1) % 4]
        a = (p0[0] - p1[0], p0[1] - p1[1])
        b = (p2[0] - p1[0], p2[1] - p1[1])
        na, nb = math.hypot(*a), math.hypot(*b)
        if na < 1e-9 or nb < 1e-9:
            return False
        ang = math.degrees(math.acos(max(-1.0, min(1.0, (a[0] * b[0] + a[1] * b[1]) / (na * nb)))))
        if not MIN_ANGLE <= ang <= MAX_ANGLE:
            return False
    return True


def _diag_cross(q) -> Optional[tuple[float, float]]:
    (x1, y1), (x2, y2), (x3, y3), (x4, y4) = q[0], q[2], q[1], q[3]
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) < 1e-9:
        return None
    a, b = x1 * y2 - y1 * x2, x3 * y4 - y3 * x4
    return ((a * (x3 - x4) - (x1 - x2) * b) / d, (a * (y3 - y4) - (y1 - y2) * b) / d)


# ---------------------------------------------------------------------------------------------------- detector
MIN_CONTRAST = 40      # grey levels between the lens median and its brightest pixels
THR_SHARE = 0.60       # bright threshold = median + share * (max - median)
RING_SHARE = 0.45      # the ring around a marker must be darker than median + share * (max - median)
MIN_FILL = 0.50
MAX_ASPECT = 2.2
MAX_SIZE_RATIO = 6.0   # largest / smallest corner marker area
MIN_AREA_PX = 6        # pixels of a marker blob at the detection scale
MAX_CANDIDATES = 12
CENTER_TOL = 0.12      # centre marker within this share of the diagonal from the crossing of the diagonals
MIN_QUAD_SHARE = 0.002 # corner quad area, share of the lens area


def lenses_of(w: int, h: int) -> dict:
    """Two lenses one above the other when the frame is taller than wide (2304 x 2592 -> 2 x 2304 x 1296)."""
    if h >= w:
        return {"top": (0, 0, w, h // 2), "bottom": (0, h // 2, w, h)}
    return {"full": (0, 0, w, h)}


def _as_array(img) -> np.ndarray:
    if isinstance(img, np.ndarray):
        a = img
    else:
        a = np.asarray(img.convert("RGB"))
    if a.ndim == 2:
        a = np.repeat(a[:, :, None], 3, axis=2)
    return a[:, :, :3]


def _reduce(a: np.ndarray, scale: int) -> np.ndarray:
    if scale <= 1:
        return a.astype(np.float32)
    h, w = (a.shape[0] // scale) * scale, (a.shape[1] // scale) * scale
    b = a[:h, :w].astype(np.float32).reshape(h // scale, scale, w // scale, scale, 3)
    return b.mean(axis=(1, 3))


def _components(mask: np.ndarray, limit: int = 4000):
    """Connected bright blobs (4-neighbour). Blobs larger than `limit` pixels are skipped (windows, lamps)."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    ys, xs = np.nonzero(mask)
    comps = []
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if seen[sy, sx]:
            continue
        q = deque([(sy, sx)])
        seen[sy, sx] = True
        pts = []
        big = False
        while q:
            y, x = q.popleft()
            if not big:
                pts.append((y, x))
                if len(pts) > limit:
                    big = True
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    q.append((ny, nx))
        if not big:
            comps.append(pts)
    return comps


def _candidates(gray: np.ndarray) -> tuple[list, dict]:
    med = float(np.median(gray))
    hi = float(np.percentile(gray, 99.9))
    info = {"median": round(med, 1), "max": round(hi, 1)}
    if hi - med < MIN_CONTRAST:
        info["why"] = "LOW_CONTRAST"
        return [], info
    thr = med + THR_SHARE * (hi - med)
    ring_max = med + RING_SHARE * (hi - med)
    h, w = gray.shape
    out = []
    for pts in _components(gray >= thr):
        n = len(pts)
        if n < MIN_AREA_PX:
            continue
        yy = np.fromiter((p[0] for p in pts), int, n)
        xx = np.fromiter((p[1] for p in pts), int, n)
        x0, x1, y0, y1 = int(xx.min()), int(xx.max()) + 1, int(yy.min()), int(yy.max()) + 1
        bw, bh = x1 - x0, y1 - y0
        if max(bw, bh) / max(1, min(bw, bh)) > MAX_ASPECT or n / float(bw * bh) < MIN_FILL:
            continue
        r = max(2, int(round(0.35 * max(bw, bh))))
        ex0, ey0, ex1, ey1 = max(0, x0 - r), max(0, y0 - r), min(w, x1 + r), min(h, y1 + r)
        if ex0 == x0 or ey0 == y0 or ex1 == x1 or ey1 == y1:
            continue                                   # touches the lens edge: no full ring to check
        outer = gray[ey0:ey1, ex0:ex1]
        inner = gray[max(0, y0 - 1):y1 + 1, max(0, x0 - 1):x1 + 1]
        ring_n = outer.size - inner.size
        if ring_n <= 0:
            continue
        ring_mean = (float(outer.sum()) - float(inner.sum())) / ring_n
        if ring_mean > ring_max:
            continue                                   # glow around it: a lamp / window, not a framed marker
        out.append({"x": float(xx.mean()) + 0.5, "y": float(yy.mean()) + 0.5, "area": n, "ring": round(ring_mean, 1)})
    out.sort(key=lambda c: -c["area"])
    info["candidates"] = len(out)
    return out[:MAX_CANDIDATES], info


def _best_quad(cands: list, lens_area: float):
    best = None
    for combo in itertools.combinations(range(len(cands)), 4):
        pts = [(cands[i]["x"], cands[i]["y"]) for i in combo]
        q = order_corners(pts)
        if not _convex(q) or not _angles_ok(q):
            continue
        areas = [cands[i]["area"] for i in combo]
        if max(areas) / max(1, min(areas)) > MAX_SIZE_RATIO:
            continue
        a = poly_area(q)
        if a < MIN_QUAD_SHARE * lens_area:
            continue
        cross = _diag_cross(q)
        centre = None
        if cross is not None:
            diag = max(math.dist(q[0], q[2]), math.dist(q[1], q[3]))
            rest = [cands[i] for i in range(len(cands)) if i not in combo]
            near = sorted(rest, key=lambda c: math.dist((c["x"], c["y"]), cross))
            if near and math.dist((near[0]["x"], near[0]["y"]), cross) <= CENTER_TOL * diag:
                centre = (near[0]["x"], near[0]["y"])
        key = (centre is not None, a)
        if best is None or key > best[0]:
            best = (key, q, centre)
    return best


def _mean_rgb(rgb: np.ndarray, x: float, y: float, r: int) -> Optional[list]:
    h, w = rgb.shape[:2]
    xi, yi = int(round(x)), int(round(y))
    if not (0 <= xi < w and 0 <= yi < h):
        return None
    patch = rgb[max(0, yi - r):yi + r + 1, max(0, xi - r):xi + r + 1]
    return [float(v) for v in patch.reshape(-1, 3).mean(axis=0)]


def detect_lens(rgb: np.ndarray, screen=(1920, 1080)) -> dict:
    """One lens picture (already reduced) -> markers result in its own pixel coordinates."""
    gray = rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.114
    cands, info = _candidates(gray)
    res = {"found": False, "info": info}
    if len(cands) < 4:
        info.setdefault("why", "FEWER_THAN_4_MARKERS")
        return res
    best = _best_quad(cands, float(gray.size))
    if best is None:
        info["why"] = "NO_CONVEX_QUAD"
        return res
    _, q, centre = best
    norm = normalised(*screen)
    hm = homography([norm[k] for k in CORNERS], q)
    pred_c = apply_h(hm, [norm["c"]])[0]
    scr = apply_h(hm, [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
    left, right = math.dist(q[0], q[3]), math.dist(q[1], q[2])
    top, bottom = math.dist(q[0], q[1]), math.dist(q[3], q[2])
    side = max(1.0, math.dist(q[0], q[1]) / 40.0)
    samples = [_mean_rgb(rgb, *p, r=max(1, int(side))) for p in apply_h(hm, SAMPLE_PTS)]
    samples = [s for s in samples if s is not None]
    res.update({
        "found": True,
        "corners": [list(p) for p in q],
        "centre_found": centre is not None,
        "centre": list(centre) if centre else None,
        "centre_residual": round(math.dist(centre, pred_c), 2) if centre else None,
        "screen_quad": [list(p) for p in scr],
        "ratio_lr": round(left / right, 4) if right else None,
        "ratio_tb": round(top / bottom, 4) if bottom else None,
        "area_share": round(poly_area(q) / float(gray.size), 5),
        "centre_rgb": [int(round(v)) for v in np.mean(samples, axis=0)] if samples else None,
        "h": hm,
    })
    return res


def _scale_xy(p, k: float, dx: float, dy: float):
    return [p[0] * k + dx, p[1] * k + dy]


def detect(img, screen=(1920, 1080), scale: int = 2, upscale: float = 1.0) -> dict:
    """Frame -> {'found', 'lens', 'lenses': {name: result}}; coordinates in full-frame pixels (x upscale).
    If both lenses show the target, the one with the larger marker quad wins."""
    a = _as_array(img)
    h, w = a.shape[:2]
    out = {"found": False, "lens": None, "frame": [int(w * upscale), int(h * upscale)], "lenses": {}}
    for name, (x0, y0, x1, y1) in lenses_of(w, h).items():
        r = detect_lens(_reduce(a[y0:y1, x0:x1], scale), screen)
        k = scale * upscale
        if r["found"]:
            dx, dy = x0 * upscale, y0 * upscale
            r["corners"] = [_scale_xy(p, k, dx, dy) for p in r["corners"]]
            r["screen_quad"] = [_scale_xy(p, k, dx, dy) for p in r["screen_quad"]]
            if r["centre"]:
                r["centre"] = _scale_xy(r["centre"], k, dx, dy)
                r["centre_residual"] = round(r["centre_residual"] * k, 2)
            sc = np.diag([k, k, 1.0])
            sc[0, 2], sc[1, 2] = dx, dy
            hm = sc @ r["h"]
            r["h"] = [[round(float(v), 6) for v in row] for row in (hm / hm[2, 2])]
        out["lenses"][name] = r
    found = [(n, r) for n, r in out["lenses"].items() if r["found"]]
    if found:
        n, _ = max(found, key=lambda t: t[1]["area_share"])
        out["found"], out["lens"] = True, n
    return out


def colour_class(rgb, palette) -> Optional[int]:
    """Index of the nearest palette colour by chromaticity (r, g, b shares) and brightness; None if no rgb."""
    if rgb is None:
        return None
    def feat(c):
        s = float(sum(c)) + 1e-6
        return np.array([c[0] / s, c[1] / s, c[2] / s, s / 765.0])
    f = feat(rgb)
    d = [float(np.sum((feat(p) - f) ** 2)) for p in palette]
    return int(np.argmin(d))


def sample_colour(rgb_frame: np.ndarray, hm, scale: int = 1) -> Optional[list]:
    """Mean background colour around the centre of a known target (H from detect) in a reduced frame."""
    hmat = np.asarray(hm, float)
    if scale != 1:
        hmat = np.diag([1.0 / scale, 1.0 / scale, 1.0]) @ hmat
    vals = [_mean_rgb(rgb_frame.astype(np.float32), *p, r=2) for p in apply_h(hmat, SAMPLE_PTS)]
    vals = [v for v in vals if v is not None]
    return [int(round(v)) for v in np.mean(vals, axis=0)] if vals else None
