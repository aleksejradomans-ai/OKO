"""amb_music.py -- подсветка ТВ под музыку СВОИМ цветом (AMB_MUSIC, 04.11.2026).

Зачем. Встроенный режим ТВ «под музыку» (FOLLOW_AUDIO) цвета выбирает сам -- задать свой API не даёт
(ambilight_supportedstyles: у FOLLOW_AUDIO только алгоритмы). Оператор: «оттенок в 2 режимах: статика либо переход;
статику корректировать как на ноуте -- ползунки RGB».

Как. Микрофон ПК слышит комнату (звук играет из ТВ) -> уровень -> яркость; цвет -- наш, лампы поштучно в режиме
`expert` (CONFIRMED 27.10: в expert запись пикселя читается обратно, в lounge -- нет).
  статика  -- все 13 ламп одним цветом R,G,B;
  переход  -- градиент по кругу ламп (левый край -> верх -> правый край) от цвета A к цвету B; оттенок идёт ПО ЧИСЛУ
              (синий 170 -> жёлтый 40 через бирюзовый и зелёный, как подсветка клавиатуры Nitro 5 на фото 19:08).

Проверено живьём 04.11 до кода: микрофон «Набор микрофонов (Realtek)» слышит музыку из ТВ и под пользователем, и под
SYSTEM (служба OkoBot = LocalSystem): rms -43..-30 dBFS, 0 сбоев. sounddevice 0.5.6 поставлен в oko_bot (19:15).

Без музыки (04.11 19:37, Оператор: «статичным без музыки управлять подсветкой»): тот же мост, микрофон ЗАКРЫТ,
лампы ровным светом, яркость -- ползунок ☀ (bright, %). «подсветка 0 120 255», «подсветка без музыки зелёная»,
кнопка «💡 Без музыки» на пульте. Кадр повторяется раз в 5 с (KEEP_S): так замечается выключенный ТВ. Предел
max_hours -- только под музыку.

Микрофон -- принцип Оператора (04.11 20:08): «мы не прячем микрофон; значок должен быть виден; я должен знать, когда
голос активен; идём честно, без скрытых трекингов». Отсюда:
  * мост запускается В СЕАНСЕ ПОЛЬЗОВАТЕЛЯ -- задачей планировщика OkoAmbBridge (pythonw, вход «только при входе
    пользователя»); служба лишь просит её запуститься (schtasks /run). Так это обычная программа рабочего стола, и
    Windows показывает значок микрофона;
  * ЖЁСТКО: в сеансе службы (сеанс 0, значка нет) мост микрофон НЕ ОТКРЫВАЕТ -- mic_allowed(); вместо реакции на звук
    ровный свет и «🎙 микрофон: запрещён (сеанс службы)». «Без музыки» микрофон не нужен -- может жить и в службе;
  * что делается со звуком: on_block получает 60 мс, считает ОДНО число -- громкость -- и отбрасывает звук. Не пишется,
    не распознаётся, не уходит в сеть/Telegram/модель; в ТВ уходят только цвета. Состояние -- «🎙 …» в /amb и журнал.

Устройство. Мост -- ОТДЕЛЬНЫЙ процесс (`python amb_music.py run`), бот им управляет файлом-пультом:
  data\\amb_music.json         -- желаемое состояние (вкл, режим, цвета, чувствительность); мост перечитывает на лету,
                                  поэтому ползунок в Telegram меняет цвет без перезапуска;
  data\\amb_music_status.json  -- что мост делает на самом деле (пульс раз в 1 с, кадры, сбои, режим ТВ, причина
                                  остановки). Живость -- по свежести пульса, НЕ по os.kill(pid, 0): на Windows
                                  os.kill с сигналом 0 вызывает TerminateProcess и убил бы процесс.
Бот не ждёт сеть ТВ в событийном цикле и не держит аудиопоток в своём процессе.

Возврат. При старте мост запоминает прежний стиль подсветки (и цвет lounge), при остановке -- возвращает его и
читает режим обратно. Остановка: «/amb стоп», кнопка ⏹, любая команда, меняющая стиль подсветки (tv_bot_handler),
смена режима с пульта (мост видит не-expert и уходит, а не воюет с человеком), ТВ молчит 30 с, предел max_hours.

НЕ ПРОВЕРЕНО (PENDING, живой пруф): порядок ламп внутри стороны (флаги flip_* в файле-пульте), что возврат стиля
выводит ТВ из expert.
"""
from __future__ import annotations

import colorsys
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
SIDES_RING = ("left", "top", "right")          # обход по кругу; снизу ламп нет (topology bottom 0)
DEFAULT_TOPOLOGY = {"left": 3, "top": 7, "right": 3, "bottom": 0}
STATIC, GRADIENT = "static", "gradient"
MODE_SAY = {STATIC: "статика", GRADIENT: "переход"}
MIN_K = 0.06                                   # тишина -- слабое свечение, а не темнота (мигание на паузах)
BEAT_FRESH_S = 5.0
SEND_GAP_S = 0.14                              # потолок ~7 обн./с (HTTPS+digest), как у audio_bridge
TV_SILENT_S = 30.0
KEEP_S = 5.0                                   # без музыки: повтор кадра (пиксели могли сбиться, ТВ могли выключить)
MODE_CHECK_S = 10.0
STEPS = (-32, -8, 8, 32)
TASK_NAME = "OkoAmbBridge"                     # задача планировщика: мост в сеансе пользователя (значок микрофона)
MIC_DENIED = "запрещён (сеанс службы — значка Windows нет)"

# Цвета словами: основа -> RGB. Окончания любые (синий/синего/синюю), граница слова обязательна.
COLOR_STEMS: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("красн", (255, 0, 0)), ("оранжев", (255, 100, 0)), ("желт", (255, 220, 0)), ("зелен", (0, 255, 0)),
    ("бирюзов", (0, 255, 180)), ("голуб", (0, 170, 255)), ("син", (0, 0, 255)), ("фиолетов", (150, 0, 255)),
    ("сиренев", (200, 120, 255)), ("розов", (255, 60, 150)), ("бел", (255, 255, 255)), ("тепл", (255, 150, 60)),
    ("холодн", (150, 200, 255)),
)
_ENDS = r"(ый|ий|ой|ая|яя|ую|юю|ое|ее|ого|его|ому|ему|ым|им|ом|ем|ые|ие|ых|их|ей|ую)?"
PRESETS = (("🔴", "ff0000"), ("🟢", "00ff00"), ("🔵", "0000ff"), ("🟡", "ffdc00"), ("🟣", "9600ff"), ("⚪", "ffffff"))

MUSIC_RE = re.compile(r"(?<![a-zа-я])(музык\w*|music|muzyk\w*|muzik\w*)(?![a-zа-я])")
GRAD_RE = re.compile(r"(?<![a-zа-я])(переход\w*|градиент\w*|радуг\w*|perehod\w*|gradient\w*|raduga)(?![a-zа-я])")
RANGE_RE = re.compile(r"(?<![a-zа-я])от\s+\S+\s+(до|к)\s+\S+")
RGB_RE = re.compile(r"(?<!\d)(\d{1,3})[\s,;]+(\d{1,3})[\s,;]+(\d{1,3})(?!\d)")
NO_MUSIC_RE = re.compile(r"(?<![a-zа-я])(без\s+музык\w*|bez\s+muzyk\w*|просто\s+свет|статик\w*)(?![a-zа-я])")
HEX_RE = re.compile(r"(?<![0-9a-z])#?([0-9a-f]{6})(?![0-9a-z])")

RGB = tuple[int, int, int]


# --- состояние (файл-пульт) ------------------------------------------------------------------------------

@dataclass
class State:
    on: bool = False
    mode: str = STATIC
    rgb: list = field(default_factory=lambda: [0, 120, 255])
    a: list = field(default_factory=lambda: [0, 0, 255])          # переход: начало (синий)
    b: list = field(default_factory=lambda: [255, 255, 0])        # переход: конец (жёлтый)
    edit: str = "a"                                               # какой конец перехода правят ползунки
    floor: float = -58.0                                          # тишина комнаты, dBFS (замер 04.11: музыка -43..-30)
    ceil: float = -28.0
    device: int | None = None                                     # вход sounddevice; None -- по умолчанию
    flip_left: bool = False
    flip_top: bool = False
    flip_right: bool = False
    max_hours: float = 4.0                                        # предел -- только с музыкой (микрофон открыт)
    music: bool = True                                            # False -- просто свет: без микрофона, яркость bright
    bright: int = 100                                             # % яркости без музыки


def _rgb(v: Any, default: RGB) -> list:
    try:
        r, g, b = (max(0, min(255, int(x))) for x in v)
        return [r, g, b]
    except (TypeError, ValueError):
        return list(default)


def state_from(d: dict) -> State:
    s = State()
    if not isinstance(d, dict):
        return s
    s.on = bool(d.get("on", s.on))
    s.mode = d.get("mode") if d.get("mode") in (STATIC, GRADIENT) else s.mode
    s.rgb, s.a, s.b = _rgb(d.get("rgb"), s.rgb), _rgb(d.get("a"), s.a), _rgb(d.get("b"), s.b)
    s.edit = d.get("edit") if d.get("edit") in ("a", "b") else s.edit
    for k in ("floor", "ceil", "max_hours"):
        try:
            setattr(s, k, float(d.get(k, getattr(s, k))))
        except (TypeError, ValueError):
            pass
    if s.ceil <= s.floor:
        s.floor, s.ceil = State.floor, State.ceil
    dev = d.get("device")
    s.device = dev if isinstance(dev, int) and not isinstance(dev, bool) else None
    for k in ("flip_left", "flip_top", "flip_right"):
        setattr(s, k, bool(d.get(k, False)))
    s.music = bool(d.get("music", True))
    try:
        s.bright = max(5, min(100, int(d.get("bright", 100))))
    except (TypeError, ValueError):
        s.bright = 100
    return s


def _paths() -> tuple[Path, Path]:
    import config  # noqa: PLC0415  (поздний импорт: чистые функции тестируются без config)
    base = Path(config.BASE_DIR)
    return base / "amb_music.json", base / "amb_music_status.json"


def read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


REPLACE_TRIES = 20
REPLACE_GAP_S = 0.05


def write_json(path: Path, data: dict, sleep=time.sleep) -> None:
    """Атомарно: мост читает файл в любой момент -- полузаписанный JSON недопустим.

    WINDOWS: os.replace на файл, который в этот миг открыт другим процессом на чтение, падает с PermissionError
    (WinError 5). Живой случай 04.11 22:47/22:5x: Оператор жал кнопки /amb, бот читал статус, мост в тот же миг его
    переписывал -> исключение в цикле -> мост остановился и вернул «за картинкой» («сбилось на заводские»).
    Поэтому -- повтор до 1 с; последняя ошибка наружу."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == REPLACE_TRIES - 1:
                raise
            sleep(REPLACE_GAP_S)


def load_state(path: Path) -> State:
    return state_from(read_json(path))


def save_state(path: Path, s: State) -> None:
    write_json(path, asdict(s))


# --- цвет -----------------------------------------------------------------------------------------------

def hexs(c) -> str:
    return "#{:02X}{:02X}{:02X}".format(*c)


def find_colors(t: str) -> list[RGB]:
    """Цвета словами в порядке появления: «от синего до желтого» -> [синий, жёлтый]."""
    hits = []
    for stem, rgb in COLOR_STEMS:
        for m in re.finditer(rf"(?<![a-zа-я]){stem}{_ENDS}(?![a-zа-я])", t):
            hits.append((m.start(), rgb))
    return [rgb for _, rgb in sorted(hits)]


def parse_rgb(t: str) -> RGB | None:
    m = RGB_RE.search(t)
    if m:
        vals = [int(x) for x in m.groups()]
        if all(0 <= v <= 255 for v in vals):
            return tuple(vals)  # type: ignore[return-value]
    m = HEX_RE.search(t)
    if m:
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return None


def parse_music(t: str) -> dict | None:
    """Нормализованный текст -> обновление состояния, либо None (не наш режим).
    Наш режим: «музык*» + цвет/RGB/переход, либо «переход/градиент» сам по себе. «подсветка музыка» без цвета --
    НЕ наш: остаётся встроенный режим ТВ (amb_audio), прежние команды не меняются."""
    music, grad = bool(MUSIC_RE.search(t)), bool(GRAD_RE.search(t) or RANGE_RE.search(t))
    quiet = bool(NO_MUSIC_RE.search(t))
    exact = parse_rgb(t)
    if not (music or grad or exact or quiet):
        return None
    colors = find_colors(t)
    # 04.11 19:37 (Оператор): «статичным без музыки управлять подсветкой». Явное «без музыки» или R G B без слова
    # «музыка» -- просто свет (микрофон закрыт); «под музыку» -- реакция на звук. Переход без слова -- как раньше.
    react = music and not quiet
    if grad or len(colors) >= 2:
        upd: dict = {"mode": GRADIENT, "music": react or (not quiet and not exact)}
        if len(colors) >= 2:
            upd["a"], upd["b"] = list(colors[0]), list(colors[-1])
        return upd
    rgb = exact or (colors[0] if colors else None)
    if rgb is None:
        return {"music": False} if quiet else None      # «подсветка без музыки» -- тот же цвет, без реакции на звук
    return {"mode": STATIC, "rgb": list(rgb), "music": react}


def _hsv(c) -> tuple[float, float, float]:
    return colorsys.rgb_to_hsv(c[0] / 255.0, c[1] / 255.0, c[2] / 255.0)


def blend(a, b, t: float) -> RGB:
    """Переход по оттенку ПО ЧИСЛУ (без перехода через 0): синий -> жёлтый идёт через бирюзовый и зелёный.
    У серого (насыщенность 0) оттенка нет -- берётся оттенок второго конца."""
    ha, sa, va = _hsv(a)
    hb, sb, vb = _hsv(b)
    if sa == 0:
        ha = hb
    if sb == 0:
        hb = ha
    t = max(0.0, min(1.0, t))
    r, g, bl = colorsys.hsv_to_rgb(ha + (hb - ha) * t, sa + (sb - sa) * t, va + (vb - va) * t)
    return int(round(r * 255)), int(round(g * 255)), int(round(bl * 255))


def ring(topology: dict, s: State) -> list[tuple[str, int]]:
    """Лампы по кругу: левый край, верх, правый край. Порядок внутри стороны -- предположение (PENDING живой
    сверки), правится флагами flip_* в файле-пульте без кода."""
    out = []
    for side in SIDES_RING:
        n = int((topology or {}).get(side, 0) or 0)
        idx = list(range(n))
        if getattr(s, f"flip_{side}"):
            idx.reverse()
        out += [(side, i) for i in idx]
    return out


def base_colors(topology: dict, s: State) -> dict[tuple[str, int], RGB]:
    lamps = ring(topology, s)
    if s.mode == STATIC or len(lamps) < 2:
        return {lamp: tuple(s.rgb) for lamp in lamps}  # type: ignore[misc]
    last = len(lamps) - 1
    return {lamp: blend(s.a, s.b, i / last) for i, lamp in enumerate(lamps)}


def frame(colors: dict, level: float) -> dict[str, dict[str, dict[str, int]]]:
    """Цвета x уровень звука -> тело ambilight/cached (layer1). Тишина -- MIN_K, не ноль."""
    k = MIN_K + (1.0 - MIN_K) * max(0.0, min(1.0, level))
    out: dict[str, dict[str, dict[str, int]]] = {}
    for (side, i), (r, g, b) in colors.items():
        out.setdefault(side, {})[str(i)] = {"r": int(round(r * k)), "g": int(round(g * k)), "b": int(round(b * k))}
    return out


# --- текст и ползунки -----------------------------------------------------------------------------------

def describe(s: State) -> str:
    how = "" if s.music else f", без музыки, яркость {s.bright}%"
    if s.mode == STATIC:
        r, g, b = s.rgb
        return f"статика, цвет R{r} G{g} B{b} ({hexs(s.rgb)}){how}"
    return f"переход {hexs(s.a)} → {hexs(s.b)}{how}"


def title(s: State) -> str:
    return "режим под музыку" if s.music else "режим свой свет (без музыки)"


def alive(st: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return bool(st.get("running")) and (now - float(st.get("beat") or 0)) <= BEAT_FRESH_S


def panel(s: State, st: dict, now: float | None = None) -> tuple[str, list[list[tuple[str, str]]]]:
    """Текст + кнопки (подпись, callback_data). Telegram-объекты собирает бот: модуль тестируется без telegram."""
    run = alive(st, now)
    head = "▶ работает" if run else "⏹ остановлена" + (f" ({st['reason']})" if st.get("reason") else "")
    lines = [f"{'🎵 Подсветка под музыку' if s.music else '💡 Подсветка, свой свет'}: {head}", f"Режим: {describe(s)}"]
    if s.mode == GRADIENT:
        lines.append(f"Ползунки правят: {'начало' if s.edit == 'a' else 'конец'} перехода")
    if run:
        lines.append(f"Кадров {st.get('frames', 0)}, сбоев {st.get('fails', 0)}, режим ТВ {st.get('tv_mode', '?')}")
        lines.append(f"🎙 микрофон: {st.get('mic', '?')} (только громкость, звук не сохраняется)")
    cur = s.rgb if s.mode == STATIC else (s.a if s.edit == "a" else s.b)
    rows: list[list[tuple[str, str]]] = [[
        ("● Статика" if s.mode == STATIC else "Статика", "amb:m:static"),
        ("● Переход" if s.mode == GRADIENT else "Переход", "amb:m:gradient"),
    ]]
    if s.mode == GRADIENT:
        rows.append([("● Начало" if s.edit == "a" else "Начало", "amb:e:a"),
                     ("● Конец" if s.edit == "b" else "Конец", "amb:e:b")])
    for ch, v in zip("rgb", cur):
        row = [(f"{ch.upper()}{d:+d}", f"amb:c:{ch}:{d:+d}") for d in STEPS[:2]]
        row.append((f"{ch.upper()} {v}", "amb:nop"))
        row += [(f"{ch.upper()}{d:+d}", f"amb:c:{ch}:{d:+d}") for d in STEPS[2:]]
        rows.append(row)
    rows.append([("● 🎵 Музыка" if s.music else "🎵 Музыка", "amb:s:on"),
                 ("● 💡 Без музыки" if not s.music else "💡 Без музыки", "amb:s:off")])
    if not s.music:
        rows.append([("☀−20", "amb:b:-20"), ("☀−5", "amb:b:-5"), (f"☀ {s.bright}%", "amb:nop"),
                     ("☀+5", "amb:b:+5"), ("☀+20", "amb:b:+20")])
    rows.append([(e, f"amb:p:{h}") for e, h in PRESETS])
    rows.append([("⏹ Стоп", "amb:stop") if run else ("▶ Старт", "amb:go"), ("🔄", "amb:nop")])
    return "\n".join(lines), rows


def apply_button(s: State, data: str) -> tuple[State, str]:
    """callback_data -> новое состояние + действие: 'save' | 'go' | 'stop' | ''."""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "amb":
        return s, ""
    op = parts[1]
    if op == "m" and len(parts) == 3 and parts[2] in (STATIC, GRADIENT):
        s.mode = parts[2]
        return s, "save"
    if op == "s" and len(parts) == 3 and parts[2] in ("on", "off"):
        s.music = parts[2] == "on"
        return s, "save"
    if op == "b" and len(parts) == 3:
        try:
            s.bright = max(5, min(100, s.bright + int(parts[2])))
        except ValueError:
            return s, ""
        return s, "save"
    if op == "e" and len(parts) == 3 and parts[2] in ("a", "b"):
        s.edit = parts[2]
        return s, "save"
    target = "rgb" if s.mode == STATIC else s.edit
    if op == "c" and len(parts) == 4 and parts[2] in ("r", "g", "b"):
        try:
            d = int(parts[3])
        except ValueError:
            return s, ""
        c = list(getattr(s, target))
        i = "rgb".index(parts[2])
        c[i] = max(0, min(255, c[i] + d))
        setattr(s, target, c)
        return s, "save"
    if op == "p" and len(parts) == 3:
        rgb = parse_rgb(parts[2])
        if rgb is None:
            return s, ""
        setattr(s, target, list(rgb))
        return s, "save"
    if op == "go":
        return s, "go"
    if op == "stop":
        return s, "stop"
    return s, ""


# --- управление мостом (сторона бота) --------------------------------------------------------------------

def session_id() -> int | None:
    """Сеанс Windows этого процесса: 0 -- службы (значка микрофона нет), 1+ -- пользователя. None -- не Windows,
    -1 -- не удалось узнать (на Windows считается запретом)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:  # noqa: BLE001
        pass
    return -1


def mic_allowed(sid: int | None) -> bool:
    """Микрофон только там, где Windows его показывает: сеанс пользователя (1+). None -- не Windows (тесты)."""
    return sid is None or sid > 0


def _spawn() -> str:
    """Сначала -- в сеансе пользователя (задача OkoAmbBridge). Не вышло -- «service»: ребёнок службы (без микрофона)."""
    try:
        r = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            return "user"
    except (OSError, subprocess.SubprocessError):
        pass
    _spawn_service()
    return "service"


def _spawn_service() -> str:
    """Мост -- ребёнок службы (сеанс 0): только «без музыки», микрофон там запрещён. Вывод -- data\\amb_music.log."""
    control, _ = _paths()
    log = open(control.with_name("amb_music.log"), "ab")  # noqa: SIM115 - дескриптор унаследует дочерний
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([sys.executable, str(HERE / "amb_music.py"), "run"], cwd=str(HERE), stdout=log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    finally:
        log.close()
    return "service"


def _wait(status: Path, cond: Callable[[dict], bool], timeout: float, sleep=time.sleep) -> dict:
    end = time.monotonic() + timeout
    st = read_json(status)
    while not cond(st) and time.monotonic() < end:
        sleep(0.25)
        st = read_json(status)
    return st


def start(update: dict | None = None, *, control: Path | None = None, status: Path | None = None,
          spawn: Callable[[], Any] = _spawn, fallback: Callable[[], Any] = _spawn_service,
          timeout: float = 8.0, sleep=time.sleep) -> str:
    """Включить (или обновить на лету). Ответ -- по файлу статуса моста, не по факту записи пульта."""
    if control is None or status is None:
        control, status = _paths()
    s = load_state(control)
    for k, v in (update or {}).items():
        setattr(s, k, v)
    s = state_from(asdict(s))
    s.on = True
    save_state(control, s)
    st = read_json(status)
    if alive(st):
        return f"Подсветка: {title(s)}, {describe(s)} — обновил на лету. Ползунки: /amb"
    t0 = time.time()
    ready = lambda x: (float(x.get("started") or 0) >= t0 - 1 and  # noqa: E731
                       (int(x.get("frames") or 0) > 0 or not x.get("running", True)))
    where = spawn() or "service"
    st = _wait(status, ready, timeout, sleep)
    note = ""
    if where == "user" and not ready(st):
        # Задача есть, но мост не поднялся -- скорее всего, никто не вошёл в Windows. Без музыки -- в службе (микрофон
        # не нужен); под музыку -- НЕТ: в службе микрофон запрещён, тихо подменять режим нельзя.
        note = "задача в сеансе пользователя не поднялась (вход в Windows выполнен?)"
        if not s.music:
            where = fallback() or "service"
            st = _wait(status, ready, timeout, sleep)
    if st.get("running") and int(st.get("frames") or 0) > 0:
        place = ("в твоём сеансе Windows" if st.get("session") not in (0, -1) else "в сеансе службы")
        mic = f"; 🎙 микрофон: {st.get('mic')}" if st.get("mic") else ""
        return (f"Подсветка: {title(s)}, {describe(s)}; мост {place}{mic}; ТВ в режиме {st.get('tv_mode')} "
                f"(прочитано), кадров {st.get('frames')}. Ползунки: /amb")
    reason = (st.get("reason") if float(st.get("started") or 0) >= t0 - 1 else None) or note or None
    s.on = False
    save_state(control, s)
    return f"Подсветка: {title(s)} НЕ запущена — {reason or 'мост не ответил за ' + str(int(timeout)) + ' с (data/amb_music.log)'}"


def stop(*, control: Path | None = None, status: Path | None = None, timeout: float = 6.0,
         sleep=time.sleep) -> str:
    """Остановить и дождаться возврата прежнего стиля. Пусто, если мост не работал и не должен был."""
    if control is None or status is None:
        control, status = _paths()
    s = load_state(control)
    st = read_json(status)
    if not s.on and not alive(st):
        return ""
    s.on = False
    save_state(control, s)
    if not alive(st):
        return "🎵 Подсветка под музыку: уже не работала."
    st = _wait(status, lambda x: not x.get("running"), timeout, sleep)
    if st.get("running"):
        return "🎵 Подсветка под музыку: попросил остановиться, мост не подтвердил (data/amb_music.log)."
    return f"🎵 Подсветка (свой цвет) остановлена; {st.get('restored') or 'прежний стиль не вернул'}."


def on_button(data: str, *, control: Path | None = None, status: Path | None = None, **kw) -> str:
    if control is None or status is None:
        control, status = _paths()
    s, act = apply_button(load_state(control), data)
    if act == "save":
        save_state(control, s)          # мост работает -- подхватит за 0.25 с
        return ""
    if act == "go":
        return start({}, control=control, status=status, **kw)
    if act == "stop":
        return stop(control=control, status=status) or "🎵 Не работала."
    return ""


def panel_now(**kw) -> tuple[str, list[list[tuple[str, str]]]]:
    control, status = kw.get("control"), kw.get("status")
    if control is None or status is None:
        control, status = _paths()
    return panel(load_state(control), read_json(status))


# --- мост (отдельный процесс) ----------------------------------------------------------------------------

def _retry(fn: Callable[[], Any], tries: int = 3, gap: float = 0.5, sleep=time.sleep) -> Any:
    """Чтение с ТВ с повтором. ТВ иногда отдаёт пустой ответ вместо JSON (HANDOFF 03.11; живой случай 04.11 22:42:
    «GET ambilight/currentconfiguration: ответ не JSON» уронил старт моста). Последняя ошибка -- наружу."""
    for i in range(tries):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            sleep(gap)
    return None


def _mode_of(tv) -> str:
    try:
        return str((_retry(tv.get_ambilight_mode) or {}).get("current"))
    except Exception:  # noqa: BLE001
        return "?"


def restore(tv, prev: dict) -> str:
    """Вернуть стиль, что был до моста. Ответ -- по чтению с ТВ."""
    cfg = prev.get("config") or {}
    style = str(cfg.get("styleName") or "").upper()
    setting = cfg.get("menuSetting")
    if style == "FOLLOW_COLOR":
        tv.set_ambilight_style("FOLLOW_COLOR", setting or "MANUAL_HUE")
        c = (prev.get("lounge") or {}).get("color") or {}
        if c:
            tv.set_lounge_color(int(c.get("hue") or 0), int(c.get("saturation") or 0), int(c.get("brightness") or 0))
    elif style and style != "OFF":
        tv.set_ambilight_style(style, setting)
    else:
        style, setting = "FOLLOW_VIDEO", "STANDARD"
        tv.set_ambilight_style(style, setting)
    time.sleep(0.6)
    if _mode_of(tv) == "expert":         # PENDING: выводит ли стиль ТВ из expert сам
        tv.set_ambilight_mode("internal")
        tv.set_ambilight_style(style, setting or ("MANUAL_HUE" if style == "FOLLOW_COLOR" else None))
        time.sleep(0.6)
    unknown = "" if cfg else "прежний стиль не был прочитан -- "
    got = _retry(tv.get_ambilight_config) or {}
    return f"{unknown}вернул стиль {got.get('styleName')}/{got.get('menuSetting')} (режим {_mode_of(tv)})"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE.parent / ".env", override=False)
    except Exception:  # noqa: BLE001 - нет dotenv -- окружение уже от службы
        pass


def _put(status: Path, st: dict) -> None:
    """Статус -- отчёт, не управление: сбой его записи свет не гасит и мост не останавливает (22:47)."""
    try:
        write_json(status, st)
    except OSError as e:
        st["status_fails"] = st.get("status_fails", 0) + 1
        print(f"мост: статус не записан ({type(e).__name__}) -- работаю дальше", flush=True)


def run() -> int:
    _load_env()
    if sys.stdout is None:                    # pythonw (задача планировщика): печать -- в журнал моста
        log = open(_paths()[0].with_name("amb_music.log"), "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = log
    sid = session_id()
    allowed = mic_allowed(sid)
    import audio_bridge as ab
    import audio_meter
    import sounddevice as sd
    from tv_control import TVClient

    control, status = _paths()
    st: dict = {"pid": os.getpid(), "running": True, "started": time.time(), "beat": time.time(),
                "frames": 0, "fails": 0, "tv_mode": "?", "reason": "", "restored": "", "session": sid,
                "mic": "закрыт"}
    if alive(read_json(status)) and read_json(status).get("pid") != os.getpid():
        print("мост уже работает -- выхожу", flush=True)
        return 0
    _put(status, st)
    s = load_state(control)
    tv = prev = None
    try:
        tv = TVClient.from_env()
        prev = {}
        for key, read in (("config", tv.get_ambilight_config), ("lounge", tv.get_lounge_color)):
            try:
                prev[key] = _retry(read)
            except Exception as e:  # noqa: BLE001 - прежний стиль неизвестен: старт не валим, возврат скажет честно
                print(f"мост: прежний {key} не прочитан ({type(e).__name__}) -- верну «за картинкой»", flush=True)
        tv.set_ambilight_power(True)
        tv.set_ambilight_mode("expert")
        st["tv_mode"] = _mode_of(tv)
        topology = _retry(tv.get_topology) or DEFAULT_TOPOLOGY
    except Exception as e:  # noqa: BLE001
        st.update(running=False, reason=f"телевизор недоступен: {type(e).__name__}: {e}"[:200])
        _put(status, st)
        return 1
    if st["tv_mode"] != "expert":
        st.update(running=False, reason=f"ТВ не принял режим expert (прочитано {st['tv_mode']})")
        st["restored"] = _safe_restore(tv, prev)
        _put(status, st)
        return 1

    smoother = ab.Smoother()
    lock = threading.Lock()
    level = {"v": 0.0}
    cfg = {"floor": s.floor, "ceil": s.ceil}

    def on_block(indata, n_frames, time_info, flags) -> None:
        stats = audio_meter.analyze(bytes(memoryview(indata).cast("B")), channels=ch, sample_rate=sr)
        v = smoother.push(ab.level_from_dbfs(stats.rms_dbfs, cfg["floor"], cfg["ceil"]))
        with lock:
            level["v"] = v

    info = sd.query_devices(s.device, "input") if s.device is not None else sd.query_devices(kind="input")
    ch, sr = min(2, int(info["max_input_channels"])), int(info["default_samplerate"])
    print(f"мост: сеанс {sid} (микрофон {'разрешён' if allowed else 'ЗАПРЕЩЁН'}), вход {info['name']} {sr} Гц, "
          f"ТВ {tv.host}, топология {topology}", flush=True)

    # Микрофон открыт ТОЛЬКО в режиме «под музыку»: без музыки он не нужен, а Windows показывает значок записи.
    # Режим переключается на лету -- поток открывается/закрывается в цикле.
    stream = None

    def mic(on: bool) -> None:
        nonlocal stream
        if on and not allowed:                # сеанс службы: значка нет -> микрофон не открывается (принцип 20:08)
            if st.get("mic") != MIC_DENIED:
                print(f"мост: микрофон ЗАПРЕЩЁН -- сеанс {sid}, значка Windows нет", flush=True)
            st["mic"] = MIC_DENIED
            return
        if on and stream is None:
            stream = sd.InputStream(device=s.device, samplerate=sr, channels=ch, dtype="int16",
                                    blocksize=max(1, sr * 60 // 1000), callback=on_block)
            stream.start()
            print(f"мост: микрофон открыт {time.strftime('%H:%M:%S')}", flush=True)
        elif not on and stream is not None:
            stream.stop()
            stream.close()
            stream = None
            print(f"мост: микрофон закрыт {time.strftime('%H:%M:%S')}", flush=True)
        st["mic"] = "открыт" if stream is not None else "закрыт"

    reason, colors, mtime = "остановлен по команде", base_colors(topology, s), None
    last_sent, last_lvl, last_ok, last_beat, last_mode, last_read = 0.0, -1.0, time.monotonic(), 0.0, 0.0, 0.0
    music_since = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - last_read >= 0.25:              # пульт: цвета/режим меняются на лету
                last_read = now
                try:
                    m = control.stat().st_mtime
                except OSError:
                    m = None
                raw = read_json(control) if m != mtime else None
                if raw:                       # пусто/битый = файл в этот миг переписывают -> не «выкл», читать ещё раз
                    was = s.music
                    mtime, s = m, state_from(raw)
                    colors, last_lvl = base_colors(topology, s), -1.0
                    cfg.update(floor=s.floor, ceil=s.ceil)
                    if s.music and not was:
                        music_since = now
                if not s.on:
                    break
                mic(s.music)
            if s.music and now - music_since > s.max_hours * 3600:
                reason = f"предел {s.max_hours:g} ч под музыку"
                break
            if now - last_mode >= MODE_CHECK_S:
                last_mode = now
                st["tv_mode"] = _mode_of(tv)
                if st["tv_mode"] not in ("expert", "?"):
                    reason = f"режим подсветки сменили (прочитано {st['tv_mode']}) — не спорю с пультом"
                    break
            if s.music and allowed:
                with lock:
                    v = level["v"]
                due = now - last_sent >= SEND_GAP_S and abs(v - last_lvl) >= ab.MIN_DELTA
            else:
                # Без музыки -- ровный свет. Повтор раз в KEEP_S: ТВ мог пересчитать пиксели, а молчание ТВ
                # (выключили) замечается только по сбою отправки.
                v = s.bright / 100.0
                due = v != last_lvl or now - last_sent >= KEEP_S
            if due:
                last_sent = now
                try:
                    tv._call("POST", "ambilight/cached", {"layer1": frame(colors, v)})  # noqa: SLF001
                    st["frames"] += 1
                    last_lvl, last_ok = v, now
                except Exception:  # noqa: BLE001
                    st["fails"] += 1
                    if now - last_ok > TV_SILENT_S:
                        reason = f"ТВ не отвечает {int(TV_SILENT_S)} с"
                        break
            if now - last_beat >= 1.0:
                last_beat = now
                st.update(beat=time.time(), level=round(v, 2), music=s.music)
                _put(status, st)
            time.sleep(0.02)
    except Exception as e:  # noqa: BLE001
        reason = f"сбой: {type(e).__name__}: {e}"[:200]
    finally:
        try:
            mic(False)
        except Exception:  # noqa: BLE001
            pass
    st.update(running=False, reason=reason, beat=time.time())
    st["restored"] = _safe_restore(tv, prev)
    _put(status, st)
    s = load_state(control)
    if s.on:                                  # сам остановился (ТВ, пульт, предел) -- пульт тоже «выкл»
        s.on = False
        save_state(control, s)
    print(f"мост: стоп ({reason}); {st['restored']}", flush=True)
    return 0


def _safe_restore(tv, prev) -> str:
    try:
        return restore(tv, prev or {})
    except Exception as e:  # noqa: BLE001
        return f"прежний стиль вернуть не удалось ({type(e).__name__})"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"
    if cmd == "run":
        return run()
    if cmd == "stop":
        print(stop() or "не работал")
        return 0
    control, status = _paths()
    print(json.dumps({"control": read_json(control), "status": read_json(status)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
