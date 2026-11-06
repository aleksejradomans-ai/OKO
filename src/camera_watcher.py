"""CAMERA_WATCHER (03.11.2026): кадр экрана ТВ с камеры по запросу Оператора.

Камера: Wi-Fi PTZ с двумя объективами на прошивке Xiongmai (приложение iCSee); адрес и вход -- OKO_CAM_* в .env.
RTSP проверен 03.11: `rtsp://H:554/user=U&password=P&channel=1&stream=0.sdp?real_stream`, кадр 2304x2592 = два
объектива по 2304x1296 одной картинкой. Где на кадре экран ТВ -- OKO_CAM_TV_BOX (зависит от того, куда смотрит камера).

Что делает snapshot():
  1. один кадр из RTSP (PyAV, TCP), без постоянного потока;
  2. вырезка экрана ТВ по OKO_CAM_TV_BOX, сохранение кадра и вырезки в data\\cam\\ с временем в имени;
  3. признаки без нейросети: яркость (экран выключен / включён), преобладающий цвет, изменение с прошлого кадра.

Zero-Trust: модуль не пишет в историю и в контекст мозга, текст пользователя не видит. Пароль камеры -- только из
окружения (.env), в журнал и в текст ответа не попадает. Признаки -- оценка по картинке, а не факт о ТВ: так и
подписываются («по кадру камеры»). PTZ-поворот камеры сдвигает экран -- координаты меняются в .env.
"""
from __future__ import annotations

import colorsys
import datetime as _dt
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("oko.cam")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOX = (450, 1340, 1730, 2200)      # x0, y0, x1, y1 на полном кадре 2304x2592 (ракурс 03.11 15:27, нижний объектив)
# Четыре угла экрана на полном кадре (по часовой от верхнего левого) -- экран на кадре повёрнут и в перспективе.
# Живой кадр 15:27:24: иконки меню и уведомление стоят у самых краёв экрана -- прямоугольник «центр 70 %» их вырезал,
# и включённое тёмное меню выглядело как выключенный экран (изменение 4 %). Признаки -- по всей площади экрана.
DEFAULT_QUAD = ((650, 1364), (1710, 1584), (1664, 2184), (470, 1946))
QUAD_SHRINK = 0.97                         # чуть внутрь от углов: рамка ТВ и стена не попадают
# «Экран выключен» -- живая калибровка 03.11 по площади экрана (4 угла), днём:
#   выключен 15:20:58 / 15:27:02:      яркость 34 / 43, разброс 10.7 / 10.8, резких деталей 0.63 / 0.77 %;
#   включён, тёмное меню 15:27:24:     яркость 55,      разброс 23.3,        деталей 2.34 % (иконки, уведомление);
#   включён, фильм 15:18:05:           яркость 85,      разброс 49.2,        деталей 8.77 %.
# Выключенная матрица -- тёмная, ровная и без резких деталей (отражение комнаты размыто). Порог по одной яркости (28)
# дал «включён» на выключенном ТВ; по центру 70 % -- «выключен» на тёмном меню.
OFF_LUMA = 60
OFF_STD = 17
OFF_DETAIL = 2.0          # % пикселей с резким перепадом (FIND_EDGES > EDGE_LEVEL). 17:24 (03.11): было 1.4 --
                          # выключенный экран при свете люстры дал 1.49 (блики ламп в глянце) -> ложное «светится».
                          # Тёмное меню живое: 2.34-2.43 -- выше порога.
EDGE_LEVEL = 40
OFF_SAT = 0.45            # выключен -- почти без цвета (живые по площади экрана 0.25-0.28: синева окна в отражении)
SAT_LUMA = 35             # 17:37 (03.11): выключенный экран в сумерки -- яркость 20-23, «синий» (синева окна в стекле);
                          # цвет на такой темноте -- отражение, не свечение. Цвет решает только при яркости >= SAT_LUMA.
GREY_SAT = 0.30           # для названия цвета: тёмное и ненасыщенное -- «чёрный»
KEEP_FILES = 40                            # столько последних файлов в data\cam (2 на снимок) -- без раздувания диска
DIFF_SIZE = (64, 40)


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is not None and v.strip():
        return v.strip()
    p = ROOT / ".env"                      # вне службы (ручной запуск) .env может быть не загружен
    try:
        for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return default


def enabled() -> bool:
    return bool(_env("OKO_CAM_HOST") and _env("OKO_CAM_USER"))


def out_dir() -> Path:
    return Path(_env("OKO_CAM_DIR") or (ROOT / "data" / "cam"))


# --- автокалибровка углов экрана (03.11 16:03: камеру сдвинули -- углы из .env указали на стену, чёрный экран
# посчитался «включён», разброс 54 от стены) ----------------------------------------------------------------------
CALIB_FILE = "tv_quad.json"                # data\cam\tv_quad.json -- состояние, не код и не .env
LENS_HALF = (0, 1296, 2304, 2592)          # нижний объектив на полном кадре
DETECT_SCALE = 8
MIN_AREA, MAX_AREA = 0.06, 0.70            # доля половины кадра: экран не точка и не весь кадр
MOVED_PX = 40                              # углы сдвинулись больше -- камеру переставили


def detect_quad(img, half=LENS_HALF, scale=DETECT_SCALE):
    """Тёмный экран (выключен или тёмное меню) на светлой стене -> 4 угла на полном кадре, по часовой от верхнего
    левого; None -- не найден. Самое большое тёмное связное пятно в половине кадра. На светлой картинке не работает:
    там самое тёмное -- мебель (проверено 03.11 на 4 кадрах) -- поэтому калибровать при ТЁМНОМ экране."""
    from collections import deque
    from PIL import ImageFilter
    x0, y0, x1, y1 = half
    if img.width < x1 or img.height < y1:
        return None
    g = img.crop(half).convert("L").resize(((x1 - x0) // scale, (y1 - y0) // scale)).filter(ImageFilter.MedianFilter(3))
    w, h = g.size
    hist, need, acc, p75 = g.histogram(), w * h * 0.75, 0, 255
    for level, n in enumerate(hist):                        # 75-й процентиль яркости -- стена
        acc += n
        if acc >= need:
            p75 = level
            break
    dark = min(90, p75 * 0.45)                              # заметно темнее стены
    px = g.load()
    seen = bytearray(w * h)
    best: list = []
    for sy in range(h):
        for sx in range(w):
            if seen[sy * w + sx] or px[sx, sy] >= dark:
                continue
            comp, q = [], deque([(sx, sy)])
            seen[sy * w + sx] = 1
            while q:
                x, y = q.popleft()
                comp.append((x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and px[nx, ny] < dark:
                        seen[ny * w + nx] = 1
                        q.append((nx, ny))
            if len(comp) > len(best):
                best = comp
    if not (MIN_AREA <= len(best) / (w * h) <= MAX_AREA):
        return None
    if any(p[0] in (0, w - 1) or p[1] in (0, h - 1) for p in best):
        return None                                         # касается края -- это мебель/тень, не экран на стене
    tl = min(best, key=lambda p: p[0] + p[1])
    br = max(best, key=lambda p: p[0] + p[1])
    tr = max(best, key=lambda p: p[0] - p[1])
    bl = min(best, key=lambda p: p[0] - p[1])
    quad = tuple((x0 + p[0] * scale + scale // 2, y0 + p[1] * scale + scale // 2) for p in (tl, tr, br, bl))
    width = ((quad[1][0] - quad[0][0]) + (quad[2][0] - quad[3][0])) / 2
    height = ((quad[3][1] - quad[0][1]) + (quad[2][1] - quad[1][1])) / 2
    if not (1.2 <= width / max(1, height) <= 2.6):          # экран ~16:9 с перспективой
        return None
    return quad


def box_for(quad, pad=30) -> tuple[int, int, int, int]:
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]
    return max(0, min(xs) - pad), max(0, min(ys) - pad), max(xs) + pad, max(ys) + pad


def saved_quad() -> Optional[tuple]:
    try:
        data = json.loads((out_dir() / CALIB_FILE).read_text(encoding="utf-8"))
        q = tuple(tuple(int(v) for v in p) for p in data["quad"])
        return q if len(q) == 4 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_quad(quad, source: str) -> None:
    d = out_dir()
    d.mkdir(parents=True, exist_ok=True)
    data = {"quad": [list(p) for p in quad], "source": source,
            "ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    (d / CALIB_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def quad_shift(a, b) -> float:
    return max(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q in zip(a, b))


def tv_quad() -> tuple[tuple[int, int], ...]:
    """Углы экрана: автокалибровка (data/cam/tv_quad.json) > OKO_CAM_TV_QUAD > по умолчанию."""
    q = saved_quad()
    if q:
        return q
    raw = _env("OKO_CAM_TV_QUAD")
    if raw:
        try:
            v = [int(x) for x in raw.replace(" ", "").split(",")]
            if len(v) == 8:
                return tuple((v[i], v[i + 1]) for i in range(0, 8, 2))
        except ValueError:
            pass
        logger.warning("[CAM] OKO_CAM_TV_QUAD битый (нужно 8 чисел), беру углы по умолчанию")
    return DEFAULT_QUAD


def quad_in_box(quad, box) -> list[tuple[float, float]]:
    """Углы экрана -> координаты внутри вырезки box, чуть сжатые к центру."""
    cx = sum(p[0] for p in quad) / 4
    cy = sum(p[1] for p in quad) / 4
    return [(cx + (x - cx) * QUAD_SHRINK - box[0], cy + (y - cy) * QUAD_SHRINK - box[1]) for x, y in quad]


def tv_box() -> tuple[int, int, int, int]:
    q = saved_quad()
    if q:
        return box_for(q)
    raw = _env("OKO_CAM_TV_BOX")
    if raw:
        try:
            x0, y0, x1, y1 = (int(v) for v in raw.replace(" ", "").split(","))
            if x1 > x0 and y1 > y0:
                return x0, y0, x1, y1
        except ValueError:
            logger.warning("[CAM] OKO_CAM_TV_BOX битый, беру ракурс по умолчанию")
    return DEFAULT_BOX


def rtsp_url() -> str:
    host, user, pw = _env("OKO_CAM_HOST"), _env("OKO_CAM_USER"), _env("OKO_CAM_PASS")
    ch = _env("OKO_CAM_CHANNEL", "1")
    return f"rtsp://{host}:554/user={user}&password={pw}&channel={ch}&stream=0.sdp?real_stream"


def mask(text: str) -> str:
    pw = _env("OKO_CAM_PASS")
    return text.replace(pw, "***") if pw else text


def grab_frame(url: str, timeout_s: float = 8.0):
    """Один кадр из RTSP -> PIL.Image. Первый ключевой кадр или первый кадр после 3 с."""
    import av
    t0 = time.monotonic()
    c = av.open(url, options={"rtsp_transport": "tcp", "stimeout": str(int(timeout_s * 1e6))},
                timeout=(timeout_s, timeout_s))
    try:
        for frame in c.decode(video=0):
            if frame.key_frame or time.monotonic() - t0 > 3:
                return frame.to_image()
    finally:
        c.close()
    raise RuntimeError("поток без кадров")


# --- признаки без нейросети -----------------------------------------------------------------------

_COLOURS = ((15, "красный"), (40, "оранжевый"), (70, "жёлтый"), (160, "зелёный"), (200, "голубой"),
            (255, "синий"), (290, "фиолетовый"), (330, "розовый"), (360, "красный"))


def colour_name(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0.0, min(255.0, v)) / 255.0 for v in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if v < 0.12 or (v < 0.25 and s < GREY_SAT):    # 15:20: (32,37,41) называлось «синий» -- это тёмно-серый
        return "чёрный"
    if s < 0.18:
        return "белый" if v > 0.75 else "серый"
    deg = h * 360
    return next(name for edge, name in _COLOURS if deg < edge)


INSET = 0.15        # признаки -- по центру вырезки: в рамку OKO_CAM_TV_BOX попадает белая стена (живой кадр 14:26)


def inner(crop):
    """Центр вырезки без полей (стена, рамка ТВ, перспектива): иначе чёрный экран + белая стена = «включён»."""
    w, h = crop.size
    return crop.crop((int(w * INSET), int(h * INSET), int(w * (1 - INSET)), int(h * (1 - INSET))))


NIGHT_DETAIL = 1.0       # 16:52 (ночной режим камеры, ИК): выключенный экран -- разброс 23.5 (ИК-засветка
                         # пятном и полосой), но деталей 0.54 %. При деталях < 1.0 разброс не решает.


# BLIKI (03.11 17:24): выключенный экран + люстра -- в глянце яркие пятна ламп, их края дают «детали».
# Детали без бликов: перепады дальше HL_RADIUS px от пикселей ярче HL_LEVEL. Живые: блики 17:24 -- 0.91 %
# (все детали 1.49); заставка/меню -- почти всё ярко, поэтому эта мера -- только для пограничной зоны деталей.
HL_LEVEL = 150
HL_RADIUS = 15            # MaxFilter, нечётный
BARE_DETAIL = 1.2


def bare_detail(gray, mask=None) -> float:
    """% пикселей с резким перепадом вне окрестности бликов (ярких пятен)."""
    from PIL import ImageChops, ImageFilter, ImageStat
    hl = gray.point(lambda v: 255 if v > HL_LEVEL else 0).filter(ImageFilter.MaxFilter(HL_RADIUS))
    edges = gray.filter(ImageFilter.FIND_EDGES).point(lambda v: 255 if v > EDGE_LEVEL else 0)
    return ImageStat.Stat(ImageChops.subtract(edges, hl), mask).mean[0] / 255 * 100


def screen_state(luma: float, std: float, detail: float, sat: float = 0.0, bare: Optional[float] = None) -> str:
    """off -- тёмный, без резких деталей и без цвета (выключенная матрица: отражение комнаты/ИК размыты); иначе on.
    Пограничная зона деталей 1.0-2.0 %: «on», только если разброс большой И детали есть вне бликов (bare; None --
    не считались, берутся все детали). Живые: выкл 0.31-0.77 % (день и ИК), выкл + люстра 1.49 (вне бликов 0.91),
    тёмное меню 2.34-2.43, заставка 10-16, фильм 8.77."""
    if luma >= OFF_LUMA or detail >= OFF_DETAIL or (sat >= OFF_SAT and luma >= SAT_LUMA):
        return "on"
    if detail < NIGHT_DETAIL or std < OFF_STD:
        return "off"
    return "on" if (detail if bare is None else bare) >= BARE_DETAIL else "off"


def screen_mask(size, poly):
    from PIL import Image, ImageDraw
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).polygon(poly, fill=255)
    return m


def features(crop, poly=None) -> dict:
    """poly -- углы экрана в координатах вырезки; None -- центр вырезки (без полей)."""
    from PIL import ImageFilter, ImageStat
    if poly is None:
        img = inner(crop).convert("RGB")
        mask = None
    else:
        img = crop.convert("RGB")
        mask = screen_mask(img.size, poly)
    mean = ImageStat.Stat(img, mask).mean
    gray = img.convert("L")
    std = ImageStat.Stat(gray, mask).stddev[0]
    edges = gray.filter(ImageFilter.FIND_EDGES).point(lambda v: 255 if v > EDGE_LEVEL else 0)
    detail = ImageStat.Stat(edges, mask).mean[0] / 255 * 100
    luma = 0.299 * mean[0] + 0.587 * mean[1] + 0.114 * mean[2]
    mx, mn = max(mean), min(mean)
    sat = (mx - mn) / mx if mx >= 20 else 0.0     # почти чёрное: 4 vs 6 -- не цвет, а шум
    bare = bare_detail(gray, mask) if NIGHT_DETAIL <= detail < OFF_DETAIL else None   # дорого -- только в зоне
    out = {"luma": round(luma, 1), "std": round(std, 1), "detail": round(detail, 2), "sat": round(sat, 2),
           "rgb": tuple(round(v) for v in mean), "screen": screen_state(luma, std, detail, sat, bare),
           "colour": colour_name(tuple(mean))}
    if bare is not None:
        out["bare"] = round(bare, 2)
    return out


def change_pct(crop, prev, poly=None) -> Optional[float]:
    """Изменение картинки экрана против прошлой вырезки: доля пикселей, изменившихся заметно (> 25 из 255), 0..100.
    Среднее отличие (как было) размывало мелкие иконки по всей площади: выкл -> тёмное меню давало 4 %."""
    if prev is None or prev.size != crop.size:
        return None
    from PIL import ImageChops, ImageStat
    d = ImageChops.difference(crop.convert("L"), prev.convert("L")).point(lambda v: 255 if v > 25 else 0)
    mask = screen_mask(crop.size, poly) if poly is not None else None
    if mask is None:
        d = inner(d)
    return round(ImageStat.Stat(d, mask).mean[0] / 255 * 100, 1)


# --- снимок ---------------------------------------------------------------------------------------

@dataclass
class Shot:
    ts: _dt.datetime
    frame_path: Path
    crop_path: Path
    luma: float
    screen: str
    colour: str
    change: Optional[float]
    prev_ts: Optional[str]
    note: str = ""
    night: bool = False

    def text(self) -> str:
        state = "экран, похоже, ВЫКЛЮЧЕН" if self.screen == "off" else "экран включён"
        lines = [f"📷 ТВ по кадру камеры, {self.ts:%H:%M:%S}: {state} (яркость {self.luma:.0f}/255)."]
        if self.screen == "on":
            lines.append("Цвет не различить: камера в ночном режиме (ч/б, ИК)." if self.night
                         else f"Преобладает цвет: {self.colour}.")
        if self.change is not None:
            lines.append(f"Изменение с прошлого кадра ({self.prev_ts}): {self.change:.0f}%.")
        if self.note:
            lines.append(self.note)
        lines.append("Это оценка по картинке, а не ответ телевизора.")
        return "\n".join(lines)


def _cleanup(d: Path, keep: int = KEEP_FILES) -> None:
    files = sorted(d.glob("cam_*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        try:
            p.unlink()
        except OSError:
            pass


def snapshot(grab: Optional[Callable[[str], object]] = None, now: Optional[_dt.datetime] = None) -> Shot:
    """Кадр -> вырезка экрана -> признаки. grab подменяется в тестах (без сети)."""
    from PIL import Image
    if not enabled():
        raise RuntimeError("камера не настроена: нет OKO_CAM_HOST / OKO_CAM_USER в .env")
    now = now or _dt.datetime.now().astimezone()
    d = out_dir()
    d.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        img = (grab or grab_frame)(rtsp_url())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(mask(f"{type(e).__name__}: {e}")) from None
    quad, note = tv_quad(), ""
    found = detect_quad(img)          # тёмный экран виден целиком -- сверка углов (камеру могли сдвинуть)
    if found and quad_shift(found, quad) > MOVED_PX:
        save_quad(found, "auto")
        logger.warning("[CAM] углы экрана сдвинулись на %.0f px -- пересчитаны по кадру", quad_shift(found, quad))
        quad, note = found, "⚙️ Камеру, похоже, сдвинули: углы экрана пересчитаны по этому кадру."
    box = box_for(quad) if (found or saved_quad()) else tv_box()
    if img.width < box[2] or img.height < box[3]:
        raise RuntimeError(f"кадр {img.width}x{img.height} меньше области экрана {box} -- проверь OKO_CAM_TV_BOX")
    crop = img.crop(box)
    poly = quad_in_box(quad, box)
    last = d / "last_tv.png"
    prev, prev_ts = None, None
    if last.exists() and not note:   # после пересчёта углов прошлая вырезка -- другой участок, сравнивать нельзя
        try:
            prev = Image.open(last)
            prev.load()
            prev_ts = _dt.datetime.fromtimestamp(last.stat().st_mtime).strftime("%H:%M:%S")
        except OSError:
            prev = None
    f = features(crop, poly)
    night = _is_night(img)
    ch = change_pct(crop, prev, poly)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    frame_path, crop_path = d / f"cam_{stamp}_frame.jpg", d / f"cam_{stamp}_tv.jpg"
    img.convert("RGB").save(frame_path, quality=85)
    crop.convert("RGB").save(crop_path, quality=90)
    crop.save(last)
    _cleanup(d)
    logger.info("[CAM] snapshot ms=%d luma=%.0f std=%.0f detail=%.2f bare=%s sat=%.2f screen=%s colour=%s change=%s",
                int((time.monotonic() - t0) * 1000), f["luma"], f["std"], f["detail"], f.get("bare"), f["sat"],
                f["screen"], f["colour"], ch)
    return Shot(now, frame_path, crop_path, f["luma"], f["screen"], f["colour"], ch, prev_ts, note, night)


def _is_night(img) -> bool:
    """Ночной режим камеры: кадр чёрно-белый (ИК). Проверка на уменьшенной копии: каналы почти совпадают."""
    from PIL import ImageChops, ImageStat
    small = img.convert("RGB").resize((96, 108))
    r, g, b = small.split()
    diff = ImageStat.Stat(ImageChops.difference(r, g)).mean[0] + ImageStat.Stat(ImageChops.difference(g, b)).mean[0]
    return diff < 2.0


def calibrate(grab: Optional[Callable[[str], object]] = None) -> tuple[str, Optional[Path]]:
    """/cam calib: найти экран на кадре (ТВ выключен или тёмное меню) и сохранить углы. -> (текст, картинка с рамкой)."""
    from PIL import ImageDraw
    if not enabled():
        raise RuntimeError("камера не настроена: нет OKO_CAM_HOST / OKO_CAM_USER в .env")
    try:
        img = (grab or grab_frame)(rtsp_url())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(mask(f"{type(e).__name__}: {e}")) from None
    quad = detect_quad(img)
    if not quad:
        return ("📷 Экран на кадре не найден. Калибровка -- при ТЁМНОМ экране (ТВ выключен пультом) и с экраном "
                "целиком в нижнем объективе. Выключи ТВ и повтори /cam calib."), None
    old = tv_quad()
    save_quad(quad, "calib")
    path = _calib_vis(img, quad)
    logger.info("[CAM] calib quad=%s shift=%.0f", quad, quad_shift(quad, old))
    return (f"📷 Экран найден, углы сохранены (сдвиг от прежних {quad_shift(quad, old):.0f} px). "
            "Зелёная рамка должна лечь по краям экрана."), path


def _calib_vis(img, quad) -> Path:
    """Кадр с зелёной рамкой найденных углов -> файл в data\\cam (для ответа в Telegram)."""
    from PIL import ImageDraw
    d = out_dir()
    d.mkdir(parents=True, exist_ok=True)
    off = box_for(quad, pad=120)
    vis = img.convert("RGB").crop(off)
    ImageDraw.Draw(vis).polygon([(x - off[0], y - off[1]) for x, y in quad], outline=(0, 255, 0), width=6)
    path = d / f"calib_{_dt.datetime.now():%Y%m%d_%H%M%S}.jpg"
    vis.save(path, quality=85)
    return path


# --- CALIB_AUTO (04.11): калибровка без выключения ТВ -- бот гасит экран через меню ---------------------------
# Путь Philips 50PUS9010 / Titan OS (карта tv_menu_map_draft.json, кадры oko_read\\cam\\seq\\seq_20261104_1447*,
# _1500*; 2 прогона подряд 23/23): главный экран -> ↑↑ верхняя строка -> →×6 «Настройки» -> ок -> ↓×6 «Общие» -> →
# -> ↓↓ «Питание и энергопотр.» -> → (окно «Уведомление об экологичности», фокус на OK) -> ок -> ↓ «Выкл. экран» -> ок.
# «ок» -- ровно 3 раза и только на этих узлах (тест). Шестерёнка пульта по сети не нажимается (5 имён проверено).
# Экран гаснет через ~7-10 с после последнего «ок» (1 замер 15:07) -- ждём по кадру (detect_quad), не по таймеру.
# Любая кнопка возвращает экран: в конце Home (заодно закрывает меню, если путь сбился).
SCREEN_OFF_KEYS: tuple[str, ...] = (
    ("Home",) + ("CursorUp",) * 2 + ("CursorRight",) * 6 + ("Confirm",) + ("CursorDown",) * 6
    + ("CursorRight",) + ("CursorDown",) * 2 + ("CursorRight", "Confirm", "CursorDown", "Confirm")
)
MENU_STEP_S = 3.0          # пауза после кнопки: успешные прогоны 04.11 шли с шагом ~3.8 с (1.6 с + снятие кадра)
DARK_WAIT_S = 30.0         # сколько ждать гашения после последнего «ок»
DARK_POLL_S = 1.0
RESTORE_KEY = "Home"
SCREEN_OFF_TARGET = "settings.general.power:Выкл. экран"


def calibrate_auto(tv, grab: Optional[Callable[[str], object]] = None, sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic, step_s: Optional[float] = None,
                   wait_s: float = DARK_WAIT_S) -> tuple[str, Optional[Path]]:
    """/cam calib auto: SCREEN_OFF_KEYS -> ждать тёмный экран по кадру -> сохранить углы -> Home.
    tv -- TVClient (send_key). Не бросает на сбоях ТВ/камеры: текст с номером шага. Пульт во время прогона не трогать."""
    if not enabled():
        raise RuntimeError("камера не настроена: нет OKO_CAM_HOST / OKO_CAM_USER в .env")
    if step_s is None:
        try:
            step_s = float(_env("OKO_CAM_MENU_STEP_S", str(MENU_STEP_S)))
        except ValueError:
            step_s = MENU_STEP_S
    g = grab or grab_frame
    # 06.11 (п.5.4, решение Оператора: отказ): путь — из карты меню; карта не строит маршрут -> кнопки вслепую не жмём.
    try:
        import tv_menu_map
        keys = tuple(tv_menu_map.route(SCREEN_OFF_TARGET, press=True))
    except (ValueError, OSError) as e:  # tv_menu_map.MapError -- подкласс ValueError
        logger.warning("[CAM] calib auto: маршрут по карте не построен: %s", type(e).__name__)
        return f"📷 Автокалибровка остановлена: маршрут в карте меню не найден ({e}). Углы не менялись.", None
    total = len(keys)
    try:
        for i, key in enumerate(keys, 1):
            try:
                tv.send_key(key)
            except Exception as e:  # noqa: BLE001 -- ТВ не ответил: дальше слепо не идём
                logger.warning("[CAM] calib auto: шаг %d/%d %s: %s", i, total, key, type(e).__name__)
                return (f"📷 Автокалибровка остановлена на шаге {i}/{total} ({key}): телевизор не принял кнопку "
                        f"({type(e).__name__}). Углы не менялись."), None
            sleep(step_s)
        t0, tries, img, quad = clock(), 0, None, None
        while True:
            try:
                img = g(rtsp_url())
            except Exception as e:  # noqa: BLE001
                return f"📷 Автокалибровка: кадр не снят ({mask(type(e).__name__ + ': ' + str(e))}). Углы не менялись.", None
            tries += 1
            quad = detect_quad(img)
            if quad or clock() - t0 >= wait_s:
                break
            sleep(DARK_POLL_S)
        waited = clock() - t0
        if not quad:
            logger.warning("[CAM] calib auto: экран не найден за %.0f с (%d кадров)", waited, tries)
            return (f"📷 Автокалибровка: за {waited:.0f} с ({tries} кадров) тёмный экран на кадре не найден — "
                    "либо экран не погас (путь по меню сбился), либо ТВ не целиком в нижнем объективе. "
                    "Углы не менялись."), None
        old = tv_quad()
        save_quad(quad, "auto_menu")
        path = _calib_vis(img, quad)
        logger.info("[CAM] calib auto quad=%s shift=%.0f wait=%.1f frames=%d", quad, quad_shift(quad, old), waited, tries)
        return (f"📷 Автокалибровка: экран погашен через меню ТВ, найден через {waited:.0f} с, углы сохранены "
                f"(сдвиг от прежних {quad_shift(quad, old):.0f} px). Экран включён обратно."), path
    finally:
        try:
            tv.send_key(RESTORE_KEY)
        except Exception as e:  # noqa: BLE001 -- вернуть экран не удалось: скажет следующий /cam
            logger.warning("[CAM] calib auto: %s не нажата: %s", RESTORE_KEY, type(e).__name__)


# --- проверка команды по кадру (CAM_VERIFY, 03.11) ---------------------------------------------------------------
# Задержка ТВ+камера (tools/cam_latency.py, 03.11): медиана 0.4 с, выброс 2.5 с. Экрану нужно ещё погаснуть --
# первая проверка через 3 с, при «ещё светит» -- вторая через 3 с. Снимок сам подключается к камере заново
# (2.5-4.5 с), поэтому кадр всегда новее команды.
VERIFY_WAITS = (3.0, 3.0)


def verify_screen(expect: str, waits=VERIFY_WAITS, grab: Optional[Callable[[str], object]] = None,
                  sleep: Callable[[float], None] = time.sleep, retry: Optional[Callable[[], object]] = None) -> str:
    """Ожидание «off» / «on» -> строка для ответа о команде. Исключения (камера недоступна) -- наружу:
    вызывающий пишет «проверить не удалось». Ответ телевизора здесь не учитывается вовсе.
    «on» засчитывается уверенно, только если экран ЯРКИЙ (яркость >= OFF_LUMA): после пробуждения ТВ показывает
    заставку (живые 94-133). Тусклое «on» (17:24: блики люстры, яркость 29) -- предупреждение, не «светится».
    retry -- повтор действия один раз, если первый кадр не яркий (Wake-on-LAN: 17:24 первый пакет не разбудил ТВ,
    второй через 30 с -- разбудил)."""
    shot = None
    retried = False
    for i, w in enumerate(waits):
        sleep(w)
        shot = snapshot(grab=grab)
        if expect == "off" and shot.screen == "off":
            break
        if expect == "on" and shot.screen == "on" and shot.luma >= OFF_LUMA:
            break
        if expect == "on" and retry is not None and not retried and i + 1 < len(waits):
            retry()
            retried = True
    assert shot is not None
    waited = sum(waits[:i + 1])
    again = " Пакет пробуждения отправлял повторно." if retried else ""
    if expect == "off":
        if shot.screen == "off":
            return f"📷 По кадру камеры через {waited:.0f} с: экран погас (яркость {shot.luma:.0f}/255)."
        return (f"⚠️ По кадру камеры через {waited:.0f} с экран НЕ погас (яркость {shot.luma:.0f}/255) — "
                "команда не дошла или телевизор её не выполнил. Проверь ТВ.")
    if shot.screen == "on" and shot.luma >= OFF_LUMA:
        return f"📷 По кадру камеры через {waited:.0f} с: экран светится (яркость {shot.luma:.0f}/255).{again}"
    if shot.screen == "on":
        return (f"⚠️ По кадру камеры через {waited:.0f} с экран тусклый (яркость {shot.luma:.0f}/255): включён ли он "
                f"или это блики ламп в стекле — по кадру не уверен. Проверь ТВ.{again}")
    return f"⚠️ По кадру камеры через {waited:.0f} с экран тёмный (яркость {shot.luma:.0f}/255) — проверь ТВ.{again}"
