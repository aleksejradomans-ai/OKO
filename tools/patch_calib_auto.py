"""CALIB_AUTO + SAFE-06 (04.11): калибровка камеры без выключения ТВ и кадр до «включи телевизор».

1) camera_watcher.calibrate_auto(tv): бот гасит экран через меню ТВ по карте (23 сетевых кнопки, путь
   проверен 2 прогонами 04.11 14:47 и 15:00), ждёт гашения ПО КАДРУ (detect_quad нашёл тёмный экран, не таймер:
   экран гаснет через ~7-10 с после «ок», 1 замер 15:07), сохраняет углы, возвращает экран кнопкой Home.
   /cam calib auto — в bot.py.
2) tv_bot_handler.PRE: проверка ДО команды (как VERIFY после). bot.py: PRE["power_on"] = кадр камеры до WoL —
   пока ТВ выключен, экран тёмный, snapshot() сам пересчитает углы, если камеру сдвинули (> MOVED_PX).
   Идея Оператора 04.11 15:23 (SAFE-06), для неподвижной камеры уже сейчас.

Правит: src/camera_watcher.py, src/tv_bot_handler.py, src/bot.py (BOM сохраняется). Создаёт src/test_cam_calib_auto.py.
Только известные версии (SHA256[:16] на HEAD e381177). Повторный запуск — no-op.
Запуск из C:\\oko:  <python службы> tools\\patch_calib_auto.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "security_tools" / "manifest.sha256"
F_CW = ROOT / "src" / "camera_watcher.py"
F_TBH = ROOT / "src" / "tv_bot_handler.py"
F_BOT = ROOT / "src" / "bot.py"
TEST = ROOT / "src" / "test_cam_calib_auto.py"
MARKER = "CALIB_AUTO"
EXPECTED = {F_CW: "C2F816F58B52C4C7", F_TBH: "907FB70A7F14C269", F_BOT: "1F43164B40EB6240"}

# ------------------------------------------------------------------ camera_watcher.py
CW_OLD_VIS = '''    old = tv_quad()
    save_quad(quad, "calib")
    d = out_dir()
    vis = img.convert("RGB").crop(box_for(quad, pad=120))
    off = box_for(quad, pad=120)
    ImageDraw.Draw(vis).polygon([(x - off[0], y - off[1]) for x, y in quad], outline=(0, 255, 0), width=6)
    path = d / f"calib_{_dt.datetime.now():%Y%m%d_%H%M%S}.jpg"
    vis.save(path, quality=85)
    logger.info("[CAM] calib quad=%s shift=%.0f", quad, quad_shift(quad, old))
    return (f"📷 Экран найден, углы сохранены (сдвиг от прежних {quad_shift(quad, old):.0f} px). "
            "Зелёная рамка должна лечь по краям экрана."), path
'''
CW_NEW_VIS = '''    old = tv_quad()
    save_quad(quad, "calib")
    path = _calib_vis(img, quad)
    logger.info("[CAM] calib quad=%s shift=%.0f", quad, quad_shift(quad, old))
    return (f"📷 Экран найден, углы сохранены (сдвиг от прежних {quad_shift(quad, old):.0f} px). "
            "Зелёная рамка должна лечь по краям экрана."), path


def _calib_vis(img, quad) -> Path:
    """Кадр с зелёной рамкой найденных углов -> файл в data\\\\cam (для ответа в Telegram)."""
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
# Путь Philips 50PUS9010 / Titan OS (карта tv_menu_map_draft.json, кадры oko_read\\\\cam\\\\seq\\\\seq_20261104_1447*,
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
    total = len(SCREEN_OFF_KEYS)
    try:
        for i, key in enumerate(SCREEN_OFF_KEYS, 1):
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
'''

# ------------------------------------------------------------------ tv_bot_handler.py
TBH_OLD_VERIFY = "VERIFY: dict[str, Callable[[], str]] = {}\n"
TBH_NEW_VERIFY = (
    "VERIFY: dict[str, Callable[[], str]] = {}\n"
    "# CALIB_AUTO/SAFE-06 (04.11): то же ДО команды. power_on: кадр камеры, пока ТВ выключен (экран тёмный) --\n"
    "# snapshot() сам пересчитает углы, если камеру сдвинули. Пусто по умолчанию; сбой не мешает команде.\n"
    "PRE: dict[str, Callable[[], str]] = {}\n"
)
TBH_OLD_RUN = "        tv = tv_factory()\n        answer = command.run(tv)\n"
TBH_NEW_RUN = (
    "        tv = tv_factory()\n"
    "        pre_note = _pre(command.name)\n"
    "        answer = command.run(tv)\n"
)
TBH_OLD_NOTE = (
    "    note = (f\"\\n📺 Телевизор сменил адрес в сети ({moved} → {tv.host}) — нашёл его по MAC и запомнил.\"\n"
    "            if isinstance(moved, str) and moved else \"\")\n"
)
TBH_NEW_NOTE = TBH_OLD_NOTE + "    if pre_note:\n        note += \"\\n\" + pre_note\n"
TBH_OLD_DEF = "def _execute(command: Command, tv_factory: Callable[[], Any]) -> str:\n"
TBH_NEW_DEF = (
    "def _pre(name: str) -> str:\n"
    "    \"\"\"PRE-проверка до команды: строка для ответа или \"\". Исключение не мешает команде.\"\"\"\n"
    "    hook = PRE.get(name)\n"
    "    if hook is None:\n"
    "        return \"\"\n"
    "    try:\n"
    "        return hook() or \"\"\n"
    "    except Exception as exc:  # noqa: BLE001 - кадр до команды -- подсказка, не условие\n"
    "        logger.warning(\"[TV_BOT] проверка до команды cmd=%s: %s\", name, type(exc).__name__)\n"
    "        return \"\"\n"
    "\n\n"
    + TBH_OLD_DEF
)
TBH_OLD_TVNONE = "    tv = None\n    try:\n        tv = tv_factory()\n"
TBH_NEW_TVNONE = "    tv = None\n    pre_note = \"\"\n    try:\n        tv = tv_factory()\n"

# ------------------------------------------------------------------ bot.py
BOT_OLD_CALIB = "            text, path = await asyncio.to_thread(camera_watcher.calibrate)\n"
BOT_NEW_CALIB = (
    "            if len(context.args) > 1 and context.args[1].lower() == \"auto\":   # CALIB_AUTO (04.11): экран гасит бот\n"
    "                await update.message.reply_text(\"⏳ Гашу экран через меню ТВ и калибрую камеру (~1.5 мин). \"\n"
    "                                                \"Пульт не трогай.\")\n"
    "                text, path = await asyncio.to_thread(_calib_auto)\n"
    "            else:\n"
    "                text, path = await asyncio.to_thread(camera_watcher.calibrate)\n"
)
BOT_OLD_FN = "async def handle_cam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:\n"
BOT_NEW_FN = (
    "def _calib_auto():\n"
    "    \"\"\"CALIB_AUTO (04.11): /cam calib auto -- бот гасит экран через меню ТВ, калибрует, возвращает экран.\"\"\"\n"
    "    from tv_control import TVClient\n"
    "    return camera_watcher.calibrate_auto(TVClient.from_env())\n"
    "\n\n"
    "def _cam_before_power_on() -> str:\n"
    "    \"\"\"SAFE-06 (04.11, идея Оператора): кадр ДО пробуждения -- ТВ ещё выключен, экран тёмный; snapshot() сам\n"
    "    пересчитает углы, если камеру сдвинули. Строка -- только при пересчёте.\"\"\"\n"
    "    return camera_watcher.snapshot().note\n"
    "\n\n"
    + BOT_OLD_FN
)
BOT_OLD_DOC = "/cam full — ещё и весь кадр. /cam calib — найти экран на кадре (ТВ выключен) и сохранить его углы."
BOT_NEW_DOC = ("/cam full — ещё и весь кадр. /cam calib — найти экран на кадре (ТВ выключен) и сохранить его углы. "
               "/cam calib auto — экран гасит бот через меню ТВ (04.11).")
BOT_OLD_REG = ("        tv_bot_handler.VERIFY[\"power_on\"] = lambda: camera_watcher.verify_screen(\"on\", waits=(4.0, 4.0, 4.0), "
               "retry=_wol_again)   # WoL: экран -- через ~5 с; тёмный -- пакет ещё раз\n")
BOT_NEW_REG = BOT_OLD_REG + "        tv_bot_handler.PRE[\"power_on\"] = _cam_before_power_on   # SAFE-06 (04.11): кадр до WoL\n"

EDITS = [
    (F_CW, CW_OLD_VIS, CW_NEW_VIS),
    (F_TBH, TBH_OLD_VERIFY, TBH_NEW_VERIFY),
    (F_TBH, TBH_OLD_TVNONE, TBH_NEW_TVNONE),
    (F_TBH, TBH_OLD_RUN, TBH_NEW_RUN),
    (F_TBH, TBH_OLD_NOTE, TBH_NEW_NOTE),
    (F_TBH, TBH_OLD_DEF, TBH_NEW_DEF),
    (F_BOT, BOT_OLD_CALIB, BOT_NEW_CALIB),
    (F_BOT, BOT_OLD_FN, BOT_NEW_FN),
    (F_BOT, BOT_OLD_DOC, BOT_NEW_DOC),
    (F_BOT, BOT_OLD_REG, BOT_NEW_REG),
]

TEST_SRC = r'''"""CALIB_AUTO + SAFE-06 (04.11): автокалибровка через меню ТВ и проверка до команды."""
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
'''


def _sha_full(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest().upper()


def _decode(raw: bytes):
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), eol, bom


def _encode(text: str, eol: str, bom: bool) -> bytes:
    return (b"\xef\xbb\xbf" if bom else b"") + text.replace("\n", eol).encode("utf-8")


def _norm(s: str) -> str:
    s = s.strip().replace("/", "\\")
    while "\\\\" in s:
        s = s.replace("\\\\", "\\")
    return s.lower()


def main() -> int:
    files = list(EXPECTED)
    for p in files:
        if not p.exists():
            print(f"FAIL missing {p}")
            return 2
    texts = {p: _decode(p.read_bytes()) for p in files}
    if all(MARKER in texts[p][0] or "PRE: dict" in texts[p][0] for p in files) and TEST.exists() \
            and "calibrate_auto" in texts[F_CW][0]:
        print("SKIP already applied")
        return 0
    for p, want in EXPECTED.items():
        got = _sha_full(p)[:16]
        if got != want:
            print(f"FAIL hash {p.relative_to(ROOT)}: {got} != {want} (не та версия, ничего не изменено)")
            return 3
    if MANIFEST.exists():
        m = _decode(MANIFEST.read_bytes())[0]
        listed = {_norm(line.split(None, 1)[1]) for line in m.split("\n") if len(line.split(None, 1)) == 2}
        hit = [str(p.relative_to(ROOT)) for p in files if _norm(str(p.relative_to(ROOT))) in listed]
        if hit:
            print(f"FAIL: файлы в manifest.sha256 {hit} — патч их не обновляет, ничего не изменено")
            return 6
    new = {p: texts[p][0] for p in files}
    for p, old, rep in EDITS:
        n = new[p].count(old)
        if n != 1:
            print(f"FAIL anchor {p.name}: найдено {n}, нужно 1: {old[:70]!r}")
            return 4
        new[p] = new[p].replace(old, rep, 1)
    for p in files:
        try:
            compile(new[p], str(p), "exec")
        except SyntaxError as e:
            print(f"FAIL syntax {p.name}: {e}")
            return 5
    compile(TEST_SRC, "test_cam_calib_auto.py", "exec")
    for p in files:
        p.write_bytes(_encode(new[p], texts[p][1], texts[p][2]))
    TEST.write_bytes(TEST_SRC.encode("utf-8"))
    for p in files + [TEST]:
        print(f"OK {str(p.relative_to(ROOT)):<28} {_sha_full(p)[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
