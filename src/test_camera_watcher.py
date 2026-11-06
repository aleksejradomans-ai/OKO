"""CAMERA_WATCHER (03.11.2026): кадр экрана ТВ с камеры, без сети (кадр подменяется картинкой)."""
import ast
import datetime as dt
from pathlib import Path

import pytest
from PIL import Image

import camera_watcher as C

SRC = Path(__file__).resolve().parent


@pytest.fixture
def cam(monkeypatch, tmp_path):
    monkeypatch.setenv("OKO_CAM_HOST", "10.0.0.243")
    monkeypatch.setenv("OKO_CAM_USER", "u_test")
    monkeypatch.setenv("OKO_CAM_PASS", "s3cret")
    monkeypatch.setenv("OKO_CAM_DIR", str(tmp_path))
    # .env Ока загружен в окружение сюиты (config.load_dotenv): живые OKO_CAM_* не должны попадать в тесты.
    # 03.11 16:01: OKO_CAM_TV_QUAD из .env (новый ракурс) сдвинул экран в тесте -> «включён» на чёрном.
    for k in ("OKO_CAM_TV_BOX", "OKO_CAM_TV_QUAD", "OKO_CAM_CHANNEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(C, "ROOT", tmp_path)                   # живой .env Ока в тесты не подмешивается
    return tmp_path


def frame(colour=(20, 40, 200), size=(2304, 2592), box=C.DEFAULT_BOX):
    img = Image.new("RGB", size, (200, 200, 190))          # стена
    img.paste(Image.new("RGB", (box[2] - box[0], box[3] - box[1]), colour), box[:2])
    return img


T0 = dt.datetime(2026, 11, 3, 14, 10, 0).astimezone()


def test_snapshot_on_blue_saves_and_reports(cam):
    s = C.snapshot(grab=lambda url: frame((20, 40, 200)), now=T0)
    assert s.screen == "on" and s.colour == "синий" and s.change is None
    assert s.frame_path.exists() and s.crop_path.exists()
    b = C.tv_box()                     # тёмно-синяя заливка видна детектору как экран -- углы могли пересчитаться
    assert Image.open(s.crop_path).size == (b[2] - b[0], b[3] - b[1])
    t = s.text()
    assert "экран включён" in t and "синий" in t and "оценка по картинке" in t


def test_snapshot_off_and_change(cam):
    C.snapshot(grab=lambda url: frame((20, 40, 200)), now=T0)
    s = C.snapshot(grab=lambda url: frame((5, 5, 5)), now=T0 + dt.timedelta(seconds=5))
    assert s.screen == "off" and "ВЫКЛЮЧЕН" in s.text()
    assert s.change is not None and s.change > 10


def screen_frame(fill=(4, 4, 6), wall=(235, 235, 230)):
    """Полный кадр: белая стена, экран -- четырёхугольник DEFAULT_QUAD (повёрнут, в перспективе, как вживую)."""
    from PIL import ImageDraw
    img = Image.new("RGB", (2304, 2592), wall)
    ImageDraw.Draw(img).polygon(list(C.DEFAULT_QUAD), fill=fill)
    return img


def test_black_screen_with_white_wall_in_box_is_off(cam):
    """Живой кадр 14:26: в рамку экрана попадает белая стена. Признаки -- по 4 углам экрана, стена не считается."""
    s = C.snapshot(grab=lambda url: screen_frame(), now=T0)
    assert s.screen == "off", s.luma


def test_dark_menu_icons_at_edge_counted():
    """Живой кадр 15:27:24: иконки и уведомление у самых краёв экрана -- считаются (по 4 углам, не по центру 70 %).
    Числа живого кадра (детали 2.34 %) -- в test_screen_state_live_calibration. Здесь -- что края попадают в маску:
    белая точка у угла экрана меняет признаки, а у стены за рамкой -- нет."""
    from PIL import ImageDraw
    b, q = C.DEFAULT_BOX, C.quad_in_box(C.DEFAULT_QUAD, C.DEFAULT_BOX)
    base = screen_frame(fill=(40, 44, 55))
    inside = base.copy()
    ImageDraw.Draw(inside).rectangle((700, 1400, 760, 1440), fill=(255, 255, 255))     # у верхнего левого угла экрана
    outside = base.copy()
    ImageDraw.Draw(outside).rectangle((520, 1360, 580, 1380), fill=(10, 10, 10))      # стена за рамкой экрана
    f0, f1, f2 = (C.features(im.crop(b), q) for im in (base, inside, outside))
    assert f1["luma"] > f0["luma"] and f1["detail"] > f0["detail"]
    assert f2["luma"] == f0["luma"] and f2["detail"] == f0["detail"]

def test_quad_from_env(cam, monkeypatch):
    monkeypatch.setenv("OKO_CAM_TV_QUAD", "1,2,3,4,5,6,7,8")
    assert C.tv_quad() == ((1, 2), (3, 4), (5, 6), (7, 8))
    monkeypatch.setenv("OKO_CAM_TV_QUAD", "1,2,3")
    assert C.tv_quad() == C.DEFAULT_QUAD


def test_same_picture_no_change(cam):
    C.snapshot(grab=lambda url: frame((200, 30, 30)), now=T0)
    s = C.snapshot(grab=lambda url: frame((200, 30, 30)), now=T0 + dt.timedelta(seconds=5))
    assert s.colour == "красный" and s.change == 0


@pytest.mark.parametrize("luma,std,detail,state", [
    (34.4, 10.7, 0.63, "off"),     # живой 15:20:58: ТВ выключен пультом, глянец отражает комнату
    (43.3, 10.8, 0.77, "off"),     # живой 15:27:02: выключен
    (55.2, 23.3, 2.34, "on"),      # живой 15:27:24: тёмное меню, иконки у краёв (по центру 70 % было «выкл»)
    (85.2, 49.2, 8.77, "on"),      # живой 15:18:05: фильм
    (30.0, 12.0, 2.00, "on"),      # тёмная сцена с огнями/титрами
    (5.0, 1.0, 0.0, "off"),
    (42.4, 23.5, 0.54, "off"),     # живой 16:52:58: ночной режим камеры (ИК), ТВ выключен -- было «включён»
    (121.0, 78.0, 11.49, "on"),    # живой 16:53:09: ИК, заставка после пробуждения по сети
    (45.0, 25.0, 1.20, "on"),      # пограничные детали + большой разброс -- «включён» (детали вне бликов не считались)
    (55.2, 23.3, 2.43, "on"),      # живой 15:27:24 по углам экрана: тёмное меню
])
def test_screen_state_live_calibration(luma, std, detail, state):
    assert C.screen_state(luma, std, detail) == state


def test_lamp_reflections_on_black_screen_are_off():
    """Живой 17:24:15: ТВ выключен, люстра горит -- блики в стекле: яркость 29.1, разброс 25.5, детали 1.49,
    насыщенность 0.42, детали вне бликов 0.91. Было «экран светится (яркость 29)»."""
    assert C.screen_state(29.1, 25.5, 1.49, sat=0.42, bare=0.91) == "off"
    assert C.screen_state(45.0, 25.0, 1.50, bare=1.40) == "on"     # детали и вне бликов -- картинка


def test_dusk_blue_reflection_on_black_screen_is_off():
    """Живой 17:37:22/27: ТВ ушёл в ожидание (позже разбужен по сети: яркость 96), сумерки -- в стекле синева окна:
    яркость 20-23, детали 0.85-1.06, «синий». Было «экран НЕ погас» (решала насыщенность)."""
    assert C.screen_state(20.0, 24.0, 0.85, sat=0.6) == "off"
    assert C.screen_state(23.0, 26.0, 1.06, sat=0.6, bare=1.0) == "off"
    assert C.screen_state(41.0, 0.0, 0.0, sat=0.9) == "on"       # заливка цветом при яркости выше SAT_LUMA -- светит


def test_dark_blue_frame_after_standby_verifies_off(cam):
    dusk = screen_frame(fill=(12, 18, 40))                       # яркость ~18, насыщенность 0.7
    out = C.verify_screen("off", grab=seq(dusk), sleep=lambda s: None)
    assert out.startswith("📷 По кадру камеры через 3 с: экран погас")


def test_features_bright_spots_on_dark_screen(cam):
    """Синтетика: чёрный экран + несколько ярких пятен-ламп -> детали только у пятен -> «выкл»."""
    from PIL import ImageDraw
    img = screen_frame(fill=(18, 18, 22))
    d = ImageDraw.Draw(img)
    for i in range(6):
        x, y = 820 + i * 140, 1650 + (i % 2) * 120
        d.ellipse((x, y, x + 34, y + 34), fill=(250, 240, 200))
    s = C.snapshot(grab=lambda url: img, now=T0)
    assert s.screen == "off", s.text()


def test_uniform_colour_fill_is_on():
    assert C.screen_state(41.0, 0.0, 0.0, sat=0.9) == "on"        # заливка синим без деталей -- экран светит
    assert C.screen_state(43.3, 10.8, 0.77, sat=0.25) == "off"    # живой 15:27:02
    assert C.screen_state(34.4, 10.7, 0.63, sat=0.28) == "off"    # живой 15:20:58


@pytest.mark.parametrize("rgb,name", [
    ((32, 37, 41), "чёрный"),      # живой 15:20:58: было «синий»
    ((210, 30, 30), "красный"), ((230, 140, 20), "оранжевый"), ((220, 210, 30), "жёлтый"),
    ((30, 200, 40), "зелёный"), ((40, 180, 220), "голубой"), ((30, 40, 210), "синий"),
    ((150, 40, 210), "фиолетовый"), ((240, 240, 240), "белый"), ((120, 120, 120), "серый"), ((10, 10, 10), "чёрный"),
])
def test_colour_name(rgb, name):
    assert C.colour_name(rgb) == name


def test_password_never_in_error(cam):
    def boom(url):
        assert "s3cret" in url                                   # в URL пароль есть -- камера его требует
        raise OSError(f"connect failed {url}")
    with pytest.raises(RuntimeError) as e:
        C.snapshot(grab=boom, now=T0)
    assert "s3cret" not in str(e.value) and "***" in str(e.value)


def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "ROOT", tmp_path)                    # нет .env
    for k in ("OKO_CAM_HOST", "OKO_CAM_USER"):
        monkeypatch.delenv(k, raising=False)
    assert not C.enabled()
    with pytest.raises(RuntimeError):
        C.snapshot(grab=lambda url: frame())


def test_box_from_env_and_too_small_frame(cam, monkeypatch):
    monkeypatch.setenv("OKO_CAM_TV_BOX", "10, 20, 110, 80")
    assert C.tv_box() == (10, 20, 110, 80)
    monkeypatch.setenv("OKO_CAM_TV_BOX", "битое")
    assert C.tv_box() == C.DEFAULT_BOX
    with pytest.raises(RuntimeError, match="меньше области"):
        C.snapshot(grab=lambda url: Image.new("RGB", (640, 480)), now=T0)


def test_rtsp_url_channel(cam, monkeypatch):
    assert C.rtsp_url() == "rtsp://10.0.0.243:554/user=u_test&password=s3cret&channel=1&stream=0.sdp?real_stream"
    monkeypatch.setenv("OKO_CAM_CHANNEL", "2")
    assert "channel=2" in C.rtsp_url()


def test_cleanup_keeps_last(cam):
    for i in range(30):
        C.snapshot(grab=lambda url: frame(), now=T0 + dt.timedelta(seconds=i))
    assert len(list(cam.glob("cam_*.jpg"))) <= C.KEEP_FILES


# --- проводка в bot.py ---------------------------------------------------------------------------
BOT = (SRC / "bot.py").read_text(encoding="utf-8-sig")
TREE = ast.parse(BOT)


def _src(name):
    node = next(n for n in ast.walk(TREE) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)
    return ast.get_source_segment(BOT, node)


def test_cam_command_admin_only_no_history():
    body = _src("handle_cam")
    assert body.index("config.ADMIN_USER_IDS") < body.index("camera_watcher.snapshot")
    assert "asyncio.to_thread(camera_watcher.snapshot)" in body
    assert "_save_exchange" not in body and "_event(" not in body and "process_logic" not in body
    assert 'CommandHandler("cam", handle_cam)' in BOT
# Команда /cam в router.BOT_COMMANDS проверяется в test_d1_d2.py (сверка с CommandHandler в bot.py): router приватен,
# а этот тест публичен -- импорт router отсюда дал бы разрыв в зеркале (export_public, 03.11).


# --- автокалибровка углов экрана (03.11 16:03: камеру сдвинули, углы из .env указали на стену) -------------------
MOVED = ((772, 1476), (1820, 1540), (1884, 2140), (676, 2084))     # живой кадр 16:03:42 (ТВ выключен), найдено детектором


def wall_with_screen(quad, fill=(6, 6, 8), wall=(225, 222, 215)):
    from PIL import ImageDraw
    img = Image.new("RGB", (2304, 2592), wall)
    ImageDraw.Draw(img).polygon(list(quad), fill=fill)
    return img


def test_detect_quad_finds_dark_screen():
    q = C.detect_quad(wall_with_screen(MOVED))
    assert q is not None and C.quad_shift(q, MOVED) <= 3 * C.DETECT_SCALE


def test_detect_quad_rejects_bright_screen_and_edge_furniture():
    assert C.detect_quad(wall_with_screen(MOVED, fill=(200, 210, 230))) is None   # светлая картинка -- не калибровка
    from PIL import ImageDraw
    img = Image.new("RGB", (2304, 2592), (225, 222, 215))
    ImageDraw.Draw(img).rectangle((0, 2300, 2303, 2591), fill=(30, 25, 20))          # тумба у нижнего края кадра
    assert C.detect_quad(img) is None


def test_snapshot_recalibrates_when_camera_moved(cam):
    """Живой случай 16:03:42: ТВ выключен, камера сдвинута -- было «включён» (стена в рамке). Теперь углы
    пересчитываются по кадру, экран -- «выключен», сравнение с прошлым кадром не делается (другой участок)."""
    C.snapshot(grab=lambda url: screen_frame(fill=(40, 60, 160)), now=T0)            # старый ракурс
    s = C.snapshot(grab=lambda url: wall_with_screen(MOVED), now=T0 + dt.timedelta(seconds=30))
    assert s.screen == "off" and "сдвинули" in s.text() and s.change is None
    assert C.quad_shift(C.saved_quad(), MOVED) <= 3 * C.DETECT_SCALE
    s2 = C.snapshot(grab=lambda url: wall_with_screen(MOVED), now=T0 + dt.timedelta(seconds=60))
    assert s2.note == "" and s2.change == 0                                           # углы уже верные


def test_calibrate_saves_and_draws(cam):
    text, path = C.calibrate(grab=lambda url: wall_with_screen(MOVED))
    assert "сохранены" in text and path is not None and path.exists()
    assert C.tv_quad() == C.saved_quad()
    text, path = C.calibrate(grab=lambda url: wall_with_screen(MOVED, fill=(200, 210, 230)))
    assert path is None and "не найден" in text


def test_cam_calib_command_wired():
    body = _src("handle_cam")
    assert '"calib"' in body and "camera_watcher.calibrate" in body
    assert body.index("config.ADMIN_USER_IDS") < body.index("camera_watcher.calibrate")


# --- CAM_VERIFY (03.11): «выключи телевизор» -> Standby -> кадр -> погас ли экран, одним вызовом ----------------
def seq(*frames):
    it = iter(frames)
    return lambda url: next(it)


def test_verify_off_first_shot(cam):
    waits = []
    out = C.verify_screen("off", grab=seq(screen_frame()), sleep=waits.append)
    assert out.startswith("📷 По кадру камеры через 3 с: экран погас") and waits == [3.0]


def test_verify_off_second_shot(cam):
    bright = screen_frame(fill=(200, 200, 220))
    out = C.verify_screen("off", grab=seq(bright, screen_frame()), sleep=lambda s: None)
    assert "через 6 с: экран погас" in out


def test_verify_off_failed(cam):
    bright = screen_frame(fill=(200, 200, 220))
    out = C.verify_screen("off", grab=seq(bright, bright), sleep=lambda s: None)
    assert "НЕ погас" in out and out.startswith("⚠️")
    for lie in ("выключен", "выполнено", "успешно"):           # тот же запрет, что у пометки слепых команд
        assert lie not in out.lower()


def test_tv_handler_runs_registered_check_after_standby(monkeypatch):
    import tv_bot_handler as H

    class TV:
        host, moved_from = "10.0.0.41", None
        def send_key(self, key): self.key = key

    store = H.PendingStore()
    monkeypatch.setitem(H.VERIFY, "standby", lambda: "📷 КАДР-ПРОВЕРКА")
    H.handle(1, "выключи телевизор", lambda: TV(), pending=store, admin_ids={1})
    out = H.handle(1, "да", lambda: TV(), pending=store, admin_ids={1})
    assert out.endswith("📷 КАДР-ПРОВЕРКА")

    def boom():
        raise OSError("camera down")
    monkeypatch.setitem(H.VERIFY, "standby", boom)
    H.handle(1, "выключи телевизор", lambda: TV(), pending=store, admin_ids={1})
    out = H.handle(1, "да", lambda: TV(), pending=store, admin_ids={1})
    assert "Проверить по кадру камеры не удалось (OSError)" in out and "Отправил" in out


def test_bot_registers_standby_check_only_with_camera():
    i = BOT.index('tv_bot_handler.VERIFY["standby"]')
    assert "if camera_watcher.enabled():" in BOT[i - 200:i]
    assert 'camera_watcher.verify_screen("off")' in BOT[i:i + 120]


def test_night_mode_grey_frame(cam):
    """Ночной режим камеры (ч/б): цвет не называется, вместо него -- пометка о ИК."""
    grey = screen_frame(fill=(120, 120, 120), wall=(160, 160, 160))
    from PIL import ImageDraw
    d = ImageDraw.Draw(grey)
    for i in range(12):
        d.rectangle((800 + i * 70, 1700, 840 + i * 70, 1760), fill=(250, 250, 250))
    s = C.snapshot(grab=lambda url: grey, now=T0)
    assert s.night and s.screen == "on" and "ночном режиме" in s.text() and "Преобладает" not in s.text()
    s = C.snapshot(grab=lambda url: screen_frame(fill=(20, 40, 200)), now=T0 + dt.timedelta(seconds=5))
    assert not s.night


def test_bot_registers_power_on_check_only_with_camera():
    """03.11: «включи телевизор» (Wake-on-LAN) -> кадр -> засветился ли экран; ожидание дольше: экран -- через ~5 с."""
    i = BOT.index('tv_bot_handler.VERIFY["power_on"]')
    assert "if camera_watcher.enabled():" in BOT[i - 300:i]
    assert 'camera_watcher.verify_screen("on"' in BOT[i:i + 120]
    assert "retry=_wol_again" in BOT[i:i + 200] and "def _wol_again" in BOT


def test_verify_on(cam):
    dark, lit = screen_frame(), screen_frame(fill=(200, 200, 220))
    from PIL import ImageDraw
    d = ImageDraw.Draw(lit)
    for i in range(14):
        d.rectangle((700 + i * 70, 1700, 740 + i * 70, 1760), fill=(30, 30, 30))
    out = C.verify_screen("on", waits=(4.0, 4.0), grab=seq(dark, lit), sleep=lambda s: None)
    assert out.startswith("📷 По кадру камеры через 8 с: экран светится")
    out = C.verify_screen("on", waits=(4.0, 4.0), grab=seq(dark, dark), sleep=lambda s: None)
    assert out.startswith("⚠️ По кадру камеры") and "тёмный" in out


def test_verify_on_dim_is_warning_not_lit(cam):
    """17:24: «on» при яркости 29 -- не «светится», а предупреждение (могут быть блики)."""
    dim = screen_frame(fill=(25, 40, 95))                        # тусклая синяя заливка: «on» по цвету, яркость ~42
    assert C.snapshot(grab=lambda url: dim, now=T0).screen == "on"
    out = C.verify_screen("on", waits=(4.0, 4.0), grab=seq(dim, dim), sleep=lambda s: None)
    assert out.startswith("⚠️ По кадру камеры") and "тусклый" in out and "светится" not in out


def test_verify_on_retries_once_when_dark(cam):
    calls = []
    dark, lit = screen_frame(), screen_frame(fill=(200, 200, 220))
    out = C.verify_screen("on", waits=(4.0, 4.0, 4.0), grab=seq(dark, dark, dark), sleep=lambda s: None,
                          retry=lambda: calls.append(1))
    assert calls == [1] and "тёмный" in out and "повторно" in out
    calls.clear()
    out = C.verify_screen("on", waits=(4.0, 4.0, 4.0), grab=seq(dark, lit), sleep=lambda s: None,
                          retry=lambda: calls.append(1))
    assert calls == [1] and out.startswith("📷 По кадру камеры через 8 с: экран светится") and "повторно" in out
    calls.clear()
    C.verify_screen("on", waits=(4.0, 4.0), grab=seq(lit), sleep=lambda s: None, retry=lambda: calls.append(1))
    assert calls == []


def test_verify_off_never_retries(cam):
    calls = []
    bright = screen_frame(fill=(200, 200, 220))
    C.verify_screen("off", grab=seq(bright, bright), sleep=lambda s: None, retry=lambda: calls.append(1))
    assert calls == []
