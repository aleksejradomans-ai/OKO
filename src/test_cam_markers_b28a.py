"""B28a: target layout and the marker detector on synthetic two-lens frames (perspective, noise, distractors)."""
import math

import numpy as np
import pytest

import cam_markers as M

FW, FH = 2304, 2592               # two lenses one above the other, 2304 x 1296 each
SCREEN = (480, 270)               # small synthetic screen (same 16:9 geometry as 1920 x 1080)


def room(seed=1, w=FW, h=FH):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    base = (90 + 60 * xx / w + 30 * np.sin(yy / 300.0)).astype(np.float32)
    return np.stack([base, base * 0.95, base * 0.9], axis=2), rng


def warp_in(frame, target, quad):
    """Paint `target` into `frame` so that its corners land on `quad` (TL, TR, BR, BL)."""
    th, tw = target.shape[:2]
    hm = M.homography(quad, [(0, 0), (tw, 0), (tw, th), (0, th)])
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]
    x0, x1, y0, y1 = int(min(xs)), int(max(xs)) + 1, int(min(ys)), int(max(ys)) + 1
    yy, xx = np.mgrid[y0:y1, x0:x1]
    u, v, w = hm @ np.stack([xx.ravel() + 0.5, yy.ravel() + 0.5, np.ones(xx.size)])
    u, v = u / w, v / w
    ok = (u >= 0) & (u < tw) & (v >= 0) & (v < th)
    frame[yy.ravel()[ok], xx.ravel()[ok]] = target[v[ok].astype(int), u[ok].astype(int)]


def lamp(frame, cx, cy, r=60):
    """Bright blob with a soft glow (no dark frame around it)."""
    yy, xx = np.mgrid[0:frame.shape[0], 0:frame.shape[1]]
    d = np.hypot(xx - cx, yy - cy)
    glow = np.clip(255 * np.exp(-((d / (2.5 * r)) ** 2)), 0, 255)
    frame[:] = np.maximum(frame, glow[:, :, None])


def frame_with(quads, rgb=(40, 40, 40), noise=6.0, seed=1, markers=True, lamps=()):
    f, rng = room(seed)
    t = M.render(*SCREEN, rgb=rgb, markers=markers)
    for q in quads:
        warp_in(f, t, q)
    for cx, cy in lamps:
        lamp(f, cx, cy)
    f += rng.normal(0, noise, f.shape)
    return np.clip(f, 0, 255).astype(np.uint8)


def expected_corners(quad):
    hm = M.homography([(0, 0), (1, 0), (1, 1), (0, 1)], quad)
    return M.apply_h(hm, [M.normalised(*SCREEN)[k] for k in M.CORNERS])


BOTTOM = [(700, 1500), (1700, 1560), (1680, 2200), (720, 2280)]       # left side closer: taller
TOP = [(300, 200), (1250, 160), (1270, 820), (330, 760)]


def close(a, b, tol):
    return all(math.dist(p, q) <= tol for p, q in zip(a, b))


# ------------------------------------------------------------------ layout
def test_layout_geometry_1080p():
    w, h = 1920, 1080
    lay = M.layout(w, h)
    assert set(lay) == {"tl", "tr", "br", "bl", "c"}
    sides = {v["white"][2] - v["white"][0] for v in lay.values()} | {v["white"][3] - v["white"][1] for v in lay.values()}
    assert sides == {round(M.MARKER * h)}                       # all squares, one size
    for v in lay.values():
        x0, y0, x1, y1 = v["frame"]
        assert 0 < x0 < x1 < w and 0 < y0 < y1 < h
    assert lay["tl"]["c"][0] == pytest.approx(w - lay["tr"]["c"][0]) and lay["tl"]["c"][1] == pytest.approx(h - lay["bl"]["c"][1])
    assert lay["c"]["c"] == (w / 2, h / 2)
    tb = M.text_box(w, h)
    for v in lay.values():                                      # text never overlaps a marker
        fx0, fy0, fx1, fy1 = v["frame"]
        assert fx1 <= tb[0] or fx0 >= tb[2] or fy1 <= tb[1] or fy0 >= tb[3]
    for sx, sy in M.SAMPLE_PTS:                                 # colour samples on the background
        px, py = sx * w, sy * h
        assert not (tb[0] <= px <= tb[2] and tb[1] <= py <= tb[3])
        for v in lay.values():
            fx0, fy0, fx1, fy1 = v["frame"]
            assert not (fx0 <= px <= fx1 and fy0 <= py <= fy1)


def test_layout_rejects_tiny_screen_and_render_colours():
    with pytest.raises(ValueError):
        M.layout(32, 32)
    img = M.render(480, 270, rgb=(10, 200, 30))
    lay = M.layout(480, 270)
    x0, y0, x1, y1 = lay["c"]["white"]
    assert tuple(img[(y0 + y1) // 2, (x0 + x1) // 2]) == (255, 255, 255)
    assert tuple(img[lay["c"]["frame"][1] + 1, (x0 + x1) // 2]) == (0, 0, 0)
    assert tuple(img[int(0.35 * 270), int(0.35 * 480)]) == (10, 200, 30)
    assert (M.render(480, 270, markers=False) == 40).all()


def test_homography_round_trip_and_corner_order():
    src = [(0, 0), (1, 0), (1, 1), (0, 1)]
    hm = M.homography(src, BOTTOM)
    assert close(M.apply_h(hm, src), BOTTOM, 1e-6)
    shuffled = [BOTTOM[2], BOTTOM[0], BOTTOM[3], BOTTOM[1]]
    assert M.order_corners(shuffled) == BOTTOM
    assert M.poly_area([(0, 0), (2, 0), (2, 1), (0, 1)]) == 2.0


# ------------------------------------------------------------------ detector
def test_detects_target_in_bottom_lens_with_perspective():
    r = M.detect(frame_with([BOTTOM]), screen=SCREEN)
    assert r["found"] and r["lens"] == "bottom" and r["frame"] == [FW, FH]
    b = r["lenses"]["bottom"]
    assert close(b["corners"], expected_corners(BOTTOM), 6.0)
    assert b["centre_found"] and b["centre_residual"] < 6.0
    assert close(b["screen_quad"], BOTTOM, 8.0)
    assert b["ratio_lr"] > 1.1                                  # left edge longer: the screen is turned
    assert all(abs(c - 40) <= 6 for c in b["centre_rgb"])
    assert not r["lenses"]["top"]["found"]


def test_detects_target_in_top_lens():
    r = M.detect(frame_with([TOP], seed=3), screen=SCREEN)
    assert r["found"] and r["lens"] == "top"
    assert close(r["lenses"]["top"]["corners"], expected_corners(TOP), 6.0)
    assert not r["lenses"]["bottom"]["found"]


def test_both_lenses_larger_quad_wins():
    small = [(400, 1700), (800, 1690), (805, 1920), (398, 1925)]
    r = M.detect(frame_with([TOP, small], seed=4), screen=SCREEN)
    assert r["lenses"]["top"]["found"] and r["lenses"]["bottom"]["found"]
    assert r["lens"] == "top"


@pytest.mark.parametrize("rgb", [(200, 30, 30), (30, 160, 40), (30, 40, 200), (128, 128, 128)])
def test_centre_colour_on_coloured_background(rgb):
    r = M.detect(frame_with([BOTTOM], rgb=rgb, seed=5), screen=SCREEN)
    b = r["lenses"]["bottom"]
    assert r["found"] and r["lens"] == "bottom"
    assert all(abs(c - e) <= 8 for c, e in zip(b["centre_rgb"], rgb))


def test_lamp_glow_is_not_a_marker():
    r = M.detect(frame_with([BOTTOM], seed=6, lamps=[(1950, 1700), (200, 2400)]), screen=SCREEN)
    assert r["found"] and r["lens"] == "bottom"
    assert close(r["lenses"]["bottom"]["corners"], expected_corners(BOTTOM), 6.0)


def test_no_target_and_no_markers():
    r = M.detect(frame_with([]), screen=SCREEN)
    assert not r["found"] and r["lens"] is None
    r = M.detect(frame_with([BOTTOM], markers=False), screen=SCREEN)
    assert not r["found"]
    flat = np.full((FH, FW, 3), 100, np.uint8)
    r = M.detect(flat, screen=SCREEN)
    assert not r["found"] and r["lenses"]["top"]["info"]["why"] == "LOW_CONTRAST"


def test_three_markers_only_is_not_found():
    f = frame_with([BOTTOM], seed=7)
    tl = expected_corners(BOTTOM)[0]                            # paint over one corner marker
    f[int(tl[1]) - 60:int(tl[1]) + 60, int(tl[0]) - 60:int(tl[0]) + 60] = 40
    r = M.detect(f, screen=SCREEN)
    assert not r["lenses"]["bottom"]["found"]


def test_reduced_frame_with_upscale_gives_full_frame_coordinates():
    f = frame_with([BOTTOM], seed=8)
    small = f.reshape(FH // 4, 4, FW // 4, 4, 3).mean(axis=(1, 3)).astype(np.uint8)
    r = M.detect(small, screen=SCREEN, scale=1, upscale=4)
    assert r["found"] and r["lens"] == "bottom" and r["frame"] == [FW, FH]
    assert close(r["lenses"]["bottom"]["corners"], expected_corners(BOTTOM), 10.0)


def test_sample_colour_and_colour_class_follow_the_screen():
    f = frame_with([BOTTOM], seed=9)
    hm = M.detect(f, screen=SCREEN)["lenses"]["bottom"]["h"]
    palette = [(40, 40, 40), (200, 0, 0), (0, 160, 0), (0, 0, 200)]
    for k, rgb in enumerate(palette):
        g = frame_with([BOTTOM], rgb=rgb, seed=10 + k)
        small = g.reshape(FH // 2, 2, FW // 2, 2, 3).mean(axis=(1, 3))
        c = M.sample_colour(small, hm, scale=2)
        assert M.colour_class(c, palette) == k
    assert M.colour_class(None, palette) is None


def test_single_lens_frame_and_pil_input():
    from PIL import Image
    f = frame_with([BOTTOM], seed=11)[1296:]                    # one lens only (wider than tall)
    q = [(x, y - 1296) for x, y in BOTTOM]
    r = M.detect(Image.fromarray(f), screen=SCREEN)
    assert r["found"] and r["lens"] == "full"
    assert close(r["lenses"]["full"]["corners"], expected_corners(q), 6.0)
