"""CAM_LATENCY (03.11.2026): задержка камеры -- через сколько секунд изменение у ТВ видно в потоке RTSP.

Зачем: проверка команд ТВ по кадру («нажал -> снял кадр -> сверил») требует знать, сколько ждать после действия.
Снимок раньше задержки покажет СТАРУЮ картинку и даст ложное «не выполнено».

Как меряем: поток камеры держится открытым, кадры читаются непрерывно (иначе буфер RTSP отдаёт старые кадры).
Действие с точным временем -- смена цвета подсветки красный <-> синий (через ambilight/lounge, проверено 03.11:
меняет лампы, Оператор видит её днём). Для каждого кадра -- средний цвет полосы стены вокруг экрана (OKO_CAM_TV_BOX
с полем). Задержка = время первого кадра, где полоса сменила цвет, минус время команды. Это задержка «ТВ + камера»;
чистая задержка камеры -- не больше этого числа.

После проб цвет подсветки возвращается к тому, что был до запуска. Только подсветка, кнопки ТВ не нажимаются.
Запуск: python tools\\cam_latency.py [проб, по умолчанию 6]
"""
import statistics
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent.parent / "src")]
import config  # noqa: E402,F401  -- .env
import camera_watcher as C  # noqa: E402
from tv_control import TVClient  # noqa: E402

PAD = 0.18                 # поле вокруг экрана: полоса стены, где видна подсветка
SETTLE_S = 4.0             # пауза между пробами
TIMEOUT_S = 8.0            # не увидели смену за это время -- проба «нет реакции»
COLOURS = ((0, "красный"), (170, "синий"))
SCALE = 4                  # кадр уменьшается в 4 раза до анализа: полный 2304x2592 не успевает за потоком -> старые кадры


def ring_box(box, w, h):
    x0, y0, x1, y1 = box
    dx, dy = int((x1 - x0) * PAD), int((y1 - y0) * PAD)
    return max(0, x0 - dx), max(0, y0 - dy), min(w, x1 + dx), min(h, y1 + dy)


def ring_rgb(img, box):
    """Средний цвет полосы вокруг экрана (рамка outer минус экран)."""
    from PIL import ImageStat
    outer = ring_box(box, img.width, img.height)
    big = ImageStat.Stat(img.crop(outer).convert("RGB"))
    inner = ImageStat.Stat(img.crop(box).convert("RGB"))
    a_big = (outer[2] - outer[0]) * (outer[3] - outer[1])
    a_in = (box[2] - box[0]) * (box[3] - box[1])
    return tuple((big.sum[i] - inner.sum[i]) / max(1, a_big - a_in) for i in range(3))


def redness(rgb):
    r, g, b = rgb
    return r - b               # красный -> большое, синий -> отрицательное


class Reader(threading.Thread):
    def __init__(self, url, box):
        super().__init__(daemon=True)
        self.url, self.box = url, tuple(v // SCALE for v in box)
        self.frames = deque(maxlen=600)          # (t_получения, redness)
        self.err = None
        self.stop = False

    def run(self):
        import av
        try:
            c = av.open(self.url, options={"rtsp_transport": "tcp", "stimeout": "8000000"}, timeout=(8, 8))
            for frame in c.decode(video=0):
                t = time.monotonic()
                img = frame.reformat(width=frame.width // SCALE, height=frame.height // SCALE).to_image()
                self.frames.append((t, redness(ring_rgb(img, self.box))))
                if self.stop:
                    break
            c.close()
        except Exception as e:  # noqa: BLE001
            self.err = C.mask(f"{type(e).__name__}: {e}")


def main(n: int) -> int:
    box = C.tv_box()
    tv = TVClient.from_env()
    before = (tv.get_lounge_color() or {}).get("color", {})
    print(f"подсветка до проб: hue {before.get('hue')}, sat {before.get('saturation')}, bri {before.get('brightness')}")
    rd = Reader(C.rtsp_url(), box)
    rd.start()
    t_wait = time.monotonic()
    while len(rd.frames) < 10 and time.monotonic() - t_wait < 15 and not rd.err:
        time.sleep(0.2)
    if rd.err or len(rd.frames) < 10:
        print(f"FATAL: поток камеры не идёт: {rd.err or 'нет кадров'}")
        return 2
    t_a, t_b = rd.frames[0][0], rd.frames[-1][0]
    print(f"поток: {len(rd.frames) / max(0.1, t_b - t_a):.1f} кадр/с")
    tv.set_ambilight_power(True)
    tv.set_ambilight_style("FOLLOW_COLOR", "MANUAL_HUE")
    lat = []
    try:
        for i in range(n):
            hue, name = COLOURS[i % 2]
            time.sleep(SETTLE_S)
            base = [v for t, v in list(rd.frames)[-15:]]
            ref = statistics.median(base)
            t0 = time.monotonic()
            tv.set_lounge_color(hue, 255, 255)
            t_cmd = time.monotonic() - t0
            want_up = name == "красный"
            hit = None
            while time.monotonic() - t0 < TIMEOUT_S:
                for t, v in list(rd.frames):
                    if t > t0 and ((v - ref > 12) if want_up else (ref - v > 12)):
                        hit = t
                        break
                if hit:
                    break
                time.sleep(0.05)
            if hit:
                lat.append(hit - t0)
                print(f"проба {i + 1}: -> {name:8} задержка {hit - t0:.2f} с (запрос к ТВ {t_cmd:.2f} с)")
            else:
                print(f"проба {i + 1}: -> {name:8} смена на кадре НЕ видна за {TIMEOUT_S:.0f} с (запрос {t_cmd:.2f} с)")
    finally:
        tv.set_lounge_color(int(before.get("hue") or 0), int(before.get("saturation") or 255),
                            int(before.get("brightness") if before.get("brightness") is not None else 255))
        rd.stop = True
    if not lat:
        print("RESULT: смена цвета на кадре не видна -- подсветку камера не различает (поле PAD? минимальная яркость?)")
        return 1
    print(f"RESULT: задержка ТВ+камера: мин {min(lat):.2f} / медиана {statistics.median(lat):.2f} / "
          f"макс {max(lat):.2f} с, проб {len(lat)}/{n}")
    print(f"рекомендация: ждать после команды не меньше {max(lat) + 0.5:.1f} с перед проверочным кадром")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 6))
