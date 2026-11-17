"""amb_music.py -- подсветка ТВ под музыку СВОИМ цветом (AMB_MUSIC, 04.11.2026).

Зачем. Встроенный режим ТВ «под музыку» (FOLLOW_AUDIO) цвета выбирает сам -- задать свой API не даёт
(ambilight_supportedstyles: у FOLLOW_AUDIO только алгоритмы). Оператор: «оттенок в 2 режимах: статика либо переход;
статику корректировать как на ноуте -- ползунки RGB».

Как. Микрофон ПК слышит комнату (звук играет из ТВ) -> уровень -> яркость; цвет -- наш, светодиоды подсветки ТВ поштучно в режиме
`expert` (CONFIRMED 27.10: в expert запись пикселя читается обратно, в lounge -- нет).
  статика  -- все 13 светодиодов подсветки ТВ одним цветом R,G,B;   (R2b 11.11: «лампы» -- только Deltaco на потолке)
  переход  -- градиент по кругу светодиодов (левый край -> верх -> правый край) от цвета A к цвету B; оттенок идёт ПО ЧИСЛУ
              (синий 170 -> жёлтый 40 через бирюзовый и зелёный, как радужная подсветка клавиатуры ноутбука).

Проверено живьём 04.11 до кода: микрофон «Набор микрофонов (Realtek)» слышит музыку из ТВ и под пользователем, и под
SYSTEM (служба OkoBot = LocalSystem). Пакет sounddevice ставится в venv службы (requirements).

Без музыки (04.11 19:37, Оператор: «статичным без музыки управлять подсветкой»): тот же мост, микрофон ЗАКРЫТ,
светодиоды ровным светом, яркость -- ползунок ☀ (bright, %). «подсветка 0 120 255», «подсветка без музыки зелёная»,
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

Wi-Fi лампы (AMB_LAMPS, 09.11, SAFE-09). 4 лампы Deltaco (Tuya, протокол 3.5, по локальной сети, без облака) на
потолочной штанге левее ТВ: позиции 1..4 слева направо, №4 -- ближе всех к ТВ. Порядок -- data\\amb_lamps.json
(список id слева направо), ключи/адреса -- data\\tuya\\devices.json (вне git). Режимы (кнопки «💡 Лампы» в /amb):
  off    -- лампы не трогаются;
  sync   -- тот же цвет/переход и та же яркость, что у подсветки ТВ, все сразу;
  ripple   -- (В СЕЙФЕ, SAFE-13, решение Оператора 09.11 12:47) волна от ТВ №4 -> №1 с нарастанием к дальней. Код
            сохранён и протестирован, но кнопки нет: старое значение «ripple» в пульте работает как «sync».
  Вспышка под музыку -- СИНХРОННАЯ на всех (решение 12:47). Выравнивание: у каждой лампы своя задержка ответа
  (замер в работе, среднее по последним командам); быстрые лампы ждут разницу до самой медленной (не больше
  LAMP_ALIGN_MAX_S), чтобы все менялись в один момент (видео 09.11: лампы отставали от ТВ на 0.07-0.57 с).
  Без музыки -- ровный свет яркости ☀ в обоих режимах. Каждая лампа -- свой поток, «последний кадр побеждает», команда
  считается выполненной только по ответу лампы; при остановке моста лампам возвращается прежний свет (режим --
  последним, иначе запись цвета снова включает цветной режим: живой случай 09.11 11:53).

Лампы за картинкой (AMB_VIDEO, 09.11 16:3x, выбор Оператора). Кнопка «🎬 За картинкой»: подсветку ведёт САМ ТВ по
видео (стиль FOLLOW_VIDEO, режим internal), мост в ТВ ничего не пишет и микрофон не открывает -- только читает то, что
ТВ показывает (GET ambilight/processed; проба 16:30: ответ ~0.12 с, 15 разных цветов из 15 чтений), и даёт лампам
средний цвет всей подсветки ТВ; дальние лампы тусклее (VIDEO_RAMP), №4 у ТВ -- полная. «🎨 Свой цвет» -- как раньше.

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
SIDES_RING = ("left", "top", "right")          # обход по кругу; снизу светодиодов нет (topology bottom 0)
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

# --- Wi-Fi лампы (AMB_LAMPS 09.11) ---
LAMP_MODES = ("off", "sync", "ripple")
LAMP_SAY = {"off": "выкл", "sync": "синхрон", "ripple": "волна"}
LAMPS_FILE = "amb_lamps.json"                  # data\amb_lamps.json: {"order": [id_1, id_2, id_3, id_4]} слева направо
RIPPLE_STEP_S = 0.07                             # задержка волны на каждую следующую лампу (№4 -> №1: 0.21 с всего)
RIPPLE_DECAY_S = 0.45                            # затухание вспышки
RAMP = (1.0, 0.85, 0.7, 0.55)                  # волна: сила позиций 1..4 -- дальняя (1) ярче всех, у ТВ (4) слабее
LAMP_GAP_S = 0.12                              # v8: не чаще кадра на лампу (ТВ читается раз в VIDEO_READ_S = 0.12 с)
LAMP_ALIGN_MAX_S = 0.3                         # выравнивание: быстрая лампа ждёт медленную не дольше
LAMP_BUTTONS = ("off", "sync")                 # ripple -- в сейфе (SAFE-13), кнопки нет
LAMP_FLOOR = 0.03                              # лампы под музыку: глубже затухают, чем ТВ (MIN_K) -- 09.11 13:11
LAMP_GAIN_DEFAULT, LAMP_DECAY_DEFAULT = 1.3, 0.6   # «резкость +30 %», «затухание длиннее» (Оператор 09.11 13:11)
# Канал ламп «под музыку» DP 28 (control_data): «<переход><HHHHSSSSVVVV><яркость белого 4><температура 4>».
# Переход 1 -- плавно (выбор Оператора 09.11 14:17, вариант C), 0 -- прыжок. DP 24 (обычный цвет) лампа ведёт своим
# медленным переходом -- «ленивые» (14:09). Не подтвердился DP 28 дважды подряд -- эта лампа уходит на DP 24.
LAMP_FX = ("smooth", "sharp")
LAMP_FX_SAY = {"smooth": "〰 Плавно", "sharp": "⚡ Резко"}
LAMP_DP_MUSIC, LAMP_DP_COLOUR = "28", "24"
LAMP_PUSH_S = 0.05                             # как часто мост пересчитывает цели ламп
LAMP_RETRY_S = 1.0                             # пауза перед переподключением лампы после сбоя (v8.1: было 2.0)
LAMP_MIN_V = 10                                # Tuya: яркость цвета 10..1000
# DP 28 без ответа (09.11 17:41: 175 уходов на DP 24 за 6736 команд -- пропуск ответа бывает у ВСЕХ ламп, это не
# отказ канала). Правило: лампа, хоть раз подтвердившая DP 28 на этом соединении, на DP 24 НЕ уходит -- кадр просто
# не подтверждён, следующий его заменит. На DP 24 -- только лампа, не подтвердившая DP 28 ни разу за LAMP_DP28_PROBE
# попыток подряд. LAMP_DP28_DROP пропусков подряд -- переподключение (сокет мог сбиться).
LAMP_DP28_PROBE = 3
LAMP_DP28_DROP = 5
# v7b (09.11 вечер): «подтвердила DP 28» помнится на весь запуск моста, а не на соединение -- иначе всплеск молчания,
# переживший переподключение (5 пропусков -> новое соединение -> ещё 3), навсегда сажал лампу на DP 24 (грабля v4).
# Лампа, НИ РАЗУ не подтвердившая DP 28, сидит на DP 24 не навсегда: через LAMP_DP28_RETRY_S снова пробует DP 28.
LAMP_DP28_RETRY_S = 30.0
# v8 (09.11 19:16, lamp_diag EXEC): лампа SH-LGU10RGB02 v3.5 на DP 28 отвечает ПУСТЫМ ACK (retcode 0) без dps;
# tinytuya ждал второй пакет до таймаута -> 2.0-3.6 с на кадр (B). set_retry(False) + подтверждение по retcode:
# 36-368 мс (C). Без ожидания ответа (nowait): 10.5 мс (D). Поток кадров -- nowait; раз в LAMP_CHECK_S -- кадр с
# подтверждением (связь жива? + повтор последнего цвета, чтобы потерянный кадр не «подвисал»).
# v8.1 (20:10, статус v8: контрольный кадр 262-1687 мс, без ответа ~2.8-3.4 с) -- контрольный кадр держит поток
# кадров лампы, поэтому реже и с коротким таймаутом.
LAMP_CHECK_S = 5.0
LAMP_CHECK_FAILS = 3                           # столько контрольных кадров подряд без подтверждения -> переподключение
LAMP_CHECK_TIMEOUT_S = 1.0                     # ожидание подтверждения (было 2 с; tinytuya на таймауте ещё и повторяет)
LAMP_DRAIN_MAX = 40                            # перед контрольным кадром вычитать накопившиеся ACK, не больше
LAMP_DRAIN_TIMEOUT_S = 0.03
LAMP_DP28_DEAD = 3                             # столько переподключений «DP 28 молчит» подряд без единого ответа --
                                               # канал музыки у лампы считать пропавшим (DP 24 + повтор через RETRY)
# --- источник цвета (AMB_VIDEO 09.11): own -- наш цвет в expert; video -- ТВ сам по картинке, лампы за ним ---
SOURCES = ("own", "video")
SOURCE_SAY = {"own": "🎨 Свой цвет", "video": "🎬 За картинкой"}
VIDEO_READ_S = 0.12                            # чтение цвета с ТВ (проба 16:30: медиана ответа 0.11-0.13 с)
VIDEO_RAMP = (0.7, 0.8, 0.9, 1.0)              # сила позиций 1..4: дальняя тусклее, у ТВ (4) полная
VIDEO_SETTING = "NATURAL"
# Баланс белого ламп за картинкой. Видео 09.11 16:54/16:58 (без поправки): лампы против стены у ТВ -- зелёный
# x0.65-0.85, синий x1.3-1.45 (сиреневые на тёплом белом). 17:33/17:38 (поправка 0.96/1/0.8): зелёный x0.65, красный
# x1.33 (розовые). Общее: у ламп не хватает ЗЕЛЁНОГО. Здесь -- половина оценки; замер камерой (потолок против стены,
# автобаланс камеры) грубый -- точная сверка фиксированными цветами (PENDING, поворотная камера).
LAMP_WB = (0.8, 1.0, 0.74)                      # стиль ТВ «за картинкой», если раньше был другой (16:30 стоял Natural)

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
    lamps: str = "off"                                            # Wi-Fi лампы: off | sync | ripple (AMB_LAMPS)
    lamp_gain: float = LAMP_GAIN_DEFAULT                          # резкость вспышки ламп: уровень x gain
    lamp_decay: float = LAMP_DECAY_DEFAULT                        # затухание вспышки ламп, с
    lamp_fx: str = "smooth"                                       # переход лампы на канале DP 28: smooth | sharp
    source: str = "own"                                           # own -- наш цвет; video -- ТВ сам по картинке


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
    s.lamps = d.get("lamps") if d.get("lamps") in LAMP_MODES else "off"
    s.source = d.get("source") if d.get("source") in SOURCES else "own"
    if s.lamps == "ripple":                    # волна в сейфе (SAFE-13): старое значение пульта = синхрон
        s.lamps = "sync"
    try:
        s.lamp_gain = round(max(0.5, min(3.0, float(d.get("lamp_gain", LAMP_GAIN_DEFAULT)))), 2)
    except (TypeError, ValueError):
        s.lamp_gain = LAMP_GAIN_DEFAULT
    try:
        s.lamp_decay = round(max(0.15, min(2.0, float(d.get("lamp_decay", LAMP_DECAY_DEFAULT)))), 2)
    except (TypeError, ValueError):
        s.lamp_decay = LAMP_DECAY_DEFAULT
    s.lamp_fx = d.get("lamp_fx") if d.get("lamp_fx") in LAMP_FX else "smooth"
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


# --- Wi-Fi лампы: чистые функции (AMB_LAMPS 09.11) --------------------------------------------------------

def tuya_hsv(rgb, k: float) -> str:
    """RGB x сила 0..1 -> строка цвета Tuya HHHHSSSSVVVV (H 0..360, S/V 0..1000). Яркость не ниже LAMP_MIN_V;
    V квантуется шагом 10 -- мелкие колебания громкости не плодят команды."""
    h, sat, v = _hsv(rgb)
    vv = int(round(1000 * v * max(0.0, min(1.0, k)) / 10.0)) * 10
    return f"{int(round(h * 360)) % 360:04x}{int(round(sat * 1000)):04x}{max(LAMP_MIN_V, min(1000, vv)):04x}"


def lamp_base(s: State, n: int = 4) -> dict[int, RGB]:
    """Цвет каждой позиции 1..n (1 -- дальняя слева, n -- у ТВ). Статика -- один цвет; переход продолжает кольцо ТВ
    от его левого края: у ТВ (n) начало перехода A, дальняя (1) -- конец B."""
    if s.mode == STATIC or n < 2:
        return {p: tuple(s.rgb) for p in range(1, n + 1)}  # type: ignore[misc]
    return {p: blend(s.a, s.b, (n - p) / (n - 1)) for p in range(1, n + 1)}


def video_rgb(js) -> RGB | None:
    """Ответ ambilight/processed -> общий цвет подсветки ТВ: среднее светодиодов С ВЕСОМ ПО ЯРКОСТИ (вес = max(r,g,b);
    v8, 09.11: тёмные края кадра не разбавляют цвет). Все чёрные -- (0,0,0). Нет ни одной лампы -- None."""
    layer = js.get("layer1", js) if isinstance(js, dict) else None
    acc, wsum, n = [0, 0, 0], 0, 0
    for leds in (layer.values() if isinstance(layer, dict) else ()):
        for c in (leds.values() if isinstance(leds, dict) else ()):
            try:
                rgb = [max(0, min(255, int(c[k]))) for k in ("r", "g", "b")]
            except (KeyError, TypeError, ValueError):
                continue
            w = max(rgb)
            acc, wsum, n = [a + x * w for a, x in zip(acc, rgb)], wsum + w, n + 1
    if not n:
        return None
    return tuple(int(round(a / wsum)) for a in acc) if wsum else (0, 0, 0)  # type: ignore[return-value]


def lamp_wb(rgb, wb: tuple = LAMP_WB) -> RGB:
    """Поправка баланса белого ламп: каналы x wb, затем растянуть, чтобы самый яркий канал остался прежним
    (яркость задаёт картинка, поправка меняет только оттенок)."""
    r = [c * k for c, k in zip(rgb, wb)]
    top, want = max(r), max(rgb)
    s = (want / top) if top > 0 else 0.0
    return tuple(int(round(max(0, min(255, c * s)))) for c in r)  # type: ignore[return-value]


def video_levels(n: int = 4, ramp: tuple = VIDEO_RAMP) -> dict[int, float]:
    """Сила позиций 1..n за картинкой: ramp прижат к ТВ (последний элемент -- лампа n у ТВ)."""
    r = tuple(ramp)[-n:] if n <= len(ramp) else (ramp[0],) * (n - len(ramp)) + tuple(ramp)
    return {p: float(r[p - 1]) for p in range(1, n + 1)}


class Envelope:
    """Огибающая уровня: мгновенный подъём, экспоненциальное затухание; хранит историю, чтобы волна брала значение
    «как было lag секунд назад»."""

    def __init__(self, decay: float = RIPPLE_DECAY_S, keep: int = 400):
        from collections import deque
        self.decay, self.hist = decay, deque(maxlen=keep)
        self.t, self.v = None, 0.0

    def push(self, t: float, level: float) -> float:
        import math
        if self.t is not None and t > self.t:
            self.v *= math.exp(-(t - self.t) / self.decay)
        self.v, self.t = max(self.v, max(0.0, min(1.0, level))), t
        self.hist.append((t, self.v))
        return self.v

    def at(self, t: float) -> float:
        val = 0.0
        for ts, v in self.hist:
            if ts > t:
                break
            val = v
        return val


def lamp_levels(mode: str, now: float, env: Envelope, n: int = 4, step: float = RIPPLE_STEP_S,
                ramp: tuple = RAMP, gain: float = 1.0, floor: float = MIN_K) -> dict[int, float]:
    """Сила 0..1 каждой позиции. sync -- все как ТВ сейчас; ripple -- позиция p берёт уровень (n-p)*step назад
    (№n у ТВ первым) и умножается на ramp[p-1]. Тишина -- MIN_K, не темнота (как у ТВ)."""
    out = {}
    for p in range(1, n + 1):
        if mode == "ripple":
            v = env.at(now - (n - p) * step) * (ramp[p - 1] if p - 1 < len(ramp) else 1.0)
        else:
            v = env.at(now)
        out[p] = floor + (1.0 - floor) * max(0.0, min(1.0, v * gain))
    return out


def load_lamp_layout(base: Path) -> list[dict]:
    """[{pos, id, ip, key, version, name}] слева направо. Нет файлов/лампы без адреса -- пропуск (лампы просто
    не используются, мост ТВ работает как раньше)."""
    order = (read_json(Path(base) / LAMPS_FILE) or {}).get("order") or []
    devs = read_json(Path(base) / "tuya" / "devices.json")
    by_id = {d.get("id"): d for d in devs} if isinstance(devs, list) else {}
    out = []
    for pos, did in enumerate(order, 1):
        d = by_id.get(did) or {}
        if d.get("ip") and d.get("key") and d.get("version"):
            out.append({"pos": pos, "id": did, "ip": d["ip"], "key": d["key"], "version": str(d["version"]),
                        "name": d.get("name", f"лампа {pos}")})
    return out


def _tuya_factory(d: dict):
    import tinytuya  # noqa: PLC0415 - только в мосте; тесты подменяют фабрику
    b = tinytuya.BulbDevice(d["id"], d["ip"], d["key"], version=float(d["version"]))
    b.set_socketTimeout(2)
    b.set_socketRetryLimit(1)
    b.set_socketPersistent(True)
    b.set_retry(False)        # v8: после пустого ACK не ждать второй пакет (было +2 с на кадр)
    return b


def _acked(r) -> bool:
    return isinstance(r, dict) and "dps" in r and "Error" not in r


def _confirmed(b, r) -> bool:
    """Команда принята лампой: ответ с dps ИЛИ пустой ACK с кодом 0 (так DP 28 отвечает SH-LGU10RGB02, EXEC 19:16)."""
    if isinstance(r, dict):
        return "Error" not in r and "Err" not in r
    return r is None and getattr(b, "cmd_retcode", None) == 0


def _tmo(b, t: float) -> None:
    if hasattr(b, "set_socketTimeout"):
        b.set_socketTimeout(t)


def _drain(b) -> int:
    """Вычитать из сокета накопившиеся ответы на кадры nowait (иначе буфер растёт, а контрольный кадр читает старый
    ACK). Сколько пакетов вычитано."""
    if not hasattr(b, "receive"):
        return 0
    n, end = 0, time.monotonic() + 0.15
    try:
        b.set_socketTimeout(LAMP_DRAIN_TIMEOUT_S)
        while n < LAMP_DRAIN_MAX and time.monotonic() < end:
            b.raw_recv = []
            b.receive()
            if not getattr(b, "raw_recv", None):
                break
            n += 1
    finally:
        b.set_socketTimeout(2)
    return n


class LampOut:
    """Отправка в лампы: поток на лампу, «последний кадр побеждает», выполнено -- только по ответу лампы.
    Сбой лампы не трогает ни ТВ, ни другие лампы; через LAMP_RETRY_S -- новое соединение."""

    def __init__(self, lamps: list[dict], factory: Callable[[dict], Any] = _tuya_factory, gap: float = LAMP_GAP_S,
                 retry: float = LAMP_RETRY_S, check: float = LAMP_CHECK_S):
        self.lamps, self.factory, self.gap, self.retry, self.check = lamps, factory, gap, retry, check
        self.fast = 0                                       # кадров без ожидания ответа (nowait)
        self.want: dict[int, str] = {}
        self.ok: dict[int, bool] = {d["pos"]: False for d in lamps}
        self.fails, self.sent = 0, 0
        self.lat: dict[int, float] = {}                     # средняя задержка ответа лампы, с
        self.fx = "smooth"                                  # переход на канале DP 28 (мост меняет на лету)
        self.colour_dp: dict[int, str] = {}                 # лампа, не принявшая DP 28, -> DP 24
        self.dp28_seen: dict[int, bool] = {}                # лампа подтвердила DP 28 за запуск моста (v7b)
        self.dp28_at: dict[int, float] = {}                 # лампа на DP 24 -- когда снова пробовать DP 28
        self.drops28: dict[int, int] = {}                   # переподключения «DP 28 молчит» подряд без ответа
        self.miss_s = 0.0                                   # среднее время кадра DP 28 без ответа, с (для статуса)
        self.dp24_count = 0                                 # сколько раз лампы уходили на DP 24 за работу
        self.dp28_unacked = 0                               # кадры DP 28 без ответа (не отказ -- для статуса)
        self.prev: dict[int, dict] = {}
        self.dev: dict[int, Any] = {}
        self._cv = threading.Condition()
        self._stop = False
        self._th: list[threading.Thread] = []

    def start(self) -> "LampOut":
        for d in self.lamps:
            th = threading.Thread(target=self._worker, args=(d,), daemon=True, name=f"lamp{d['pos']}")
            self._th.append(th)
            th.start()
        return self

    def push(self, pos: int, hexv: str) -> None:
        with self._cv:
            self.want[pos] = hexv
            self._cv.notify_all()

    def _open(self, d: dict):
        b = self.factory(d)
        if d["pos"] not in self.prev:
            st = b.status()
            if _acked(st):
                self.prev[d["pos"]] = {k: st["dps"][k] for k in ("20", "21", "22", "23", "24") if k in st["dps"]}
        return b

    def _send(self, b, pos: int, dp: str, val: str, confirm: bool):
        """Один кадр. confirm -- ждать подтверждения (контрольный кадр), иначе nowait. Ответ: принят? (nowait -- True,
        если сокет не вернул ошибку)."""
        if not confirm:
            r = b.set_value(dp, val, nowait=True)
            if isinstance(r, dict) and ("Error" in r or "Err" in r):
                raise OSError("кадр не отправлен")
            self.fast += 1
            return True
        _drain(b)
        b.cmd_retcode = None
        t0 = time.monotonic()
        _tmo(b, LAMP_CHECK_TIMEOUT_S)
        try:
            r = b.set_value(dp, val)
        finally:
            _tmo(b, 2)
        ok = _confirmed(b, r)
        if ok:
            dt = time.monotonic() - t0
            self.lat[pos] = dt if pos not in self.lat else 0.8 * self.lat[pos] + 0.2 * dt
        return ok

    def _worker(self, d: dict) -> None:
        pos, b, last, colour_mode, next_try = d["pos"], None, None, False, 0.0
        sent_at, check_at, bad = 0.0, 0.0, 0
        while True:
            with self._cv:
                while (not self._stop and self.want.get(pos) in (None, last)
                       and (last is None or time.monotonic() < check_at)):
                    self._cv.wait(min(0.5, max(0.01, check_at - time.monotonic())) if last is not None else 0.5)
                if self._stop:
                    break
                target = self.want.get(pos)
            now = time.monotonic()
            if now < next_try:
                time.sleep(min(0.2, next_try - now))
                continue
            if now - sent_at < self.gap:                  # не чаще кадра в gap; ждём -- берём самый свежий
                time.sleep(self.gap - (now - sent_at))
                with self._cv:
                    target = self.want.get(pos, target)
            try:
                if b is None:
                    b, colour_mode = self._open(d), False
                    self.dev[pos] = b
                    self.colour_dp.pop(pos, None)          # новое соединение -- снова канал музыки
                    bad, check_at = 0, 0.0                 # первый кадр соединения -- с подтверждением
                if not colour_mode:
                    if not _confirmed(b, b.set_value("21", "colour")):
                        raise OSError("режим colour не подтверждён")
                    colour_mode = True
                t0 = time.monotonic()
                if self.colour_dp.get(pos) == LAMP_DP_COLOUR and t0 >= self.dp28_at.get(pos, 0.0):
                    self.colour_dp.pop(pos, None)          # срок вышел -- снова пробуем канал музыки
                    bad, check_at = 0, 0.0
                dp = self.colour_dp.get(pos, LAMP_DP_MUSIC)
                val = target if dp == LAMP_DP_COLOUR else ("1" if self.fx == "smooth" else "0") + target + "00000000"
                confirm = t0 >= check_at
                if self._send(b, pos, dp, val, confirm):
                    if confirm:
                        bad, self.ok[pos] = 0, True
                        if dp == LAMP_DP_MUSIC:
                            self.dp28_seen[pos], self.drops28[pos] = True, 0
                else:
                    bad += 1
                    self.dp28_unacked += 1
                    ms = time.monotonic() - t0
                    self.miss_s = ms if self.dp28_unacked == 1 else 0.8 * self.miss_s + 0.2 * ms
                    dead = self.drops28.get(pos, 0) >= LAMP_DP28_DEAD
                    probing = dp == LAMP_DP_MUSIC and (dead or not self.dp28_seen.get(pos))
                    if probing and bad >= LAMP_DP28_PROBE:      # канал музыки у лампы так и не ответил
                        if not _confirmed(b, b.set_value(LAMP_DP_COLOUR, target)):
                            raise OSError("цвет не подтверждён")
                        self.colour_dp[pos] = LAMP_DP_COLOUR
                        self.dp28_at[pos] = time.monotonic() + LAMP_DP28_RETRY_S
                        self.dp24_count += 1
                        bad = 0
                    elif not probing and bad >= LAMP_CHECK_FAILS:
                        if dp == LAMP_DP_MUSIC:
                            self.drops28[pos] = self.drops28.get(pos, 0) + 1
                        raise OSError("контрольный кадр без подтверждения подряд -- переподключение")
                    else:
                        sent_at = time.monotonic()         # следующий кадр -- снова контрольный
                        continue
                last, sent_at = target, time.monotonic()
                if confirm:
                    check_at = sent_at + self.check
                self.sent += 1
            except Exception:  # noqa: BLE001 - лампа недоступна: не мешаем остальным, переподключимся
                self.fails += 1
                self.ok[pos] = False
                b, next_try = None, time.monotonic() + self.retry
        self.dev[pos] = b

    def align(self, pos: int) -> float:
        """v8: в отправке не используется (кадры nowait уходят за ~10 мс, ждать медленную лампу -- только добавлять
        задержку к ТВ). Оставлено для статуса/будущего. Сколько этой лампе подождать, чтобы смениться вместе с самой медленной (на связи)."""
        live = [v for p, v in self.lat.items() if self.ok.get(p)]
        if pos not in self.lat or len(live) < 2:
            return 0.0
        return max(0.0, min(LAMP_ALIGN_MAX_S, max(live) - self.lat[pos]))

    def stats(self) -> str:
        lat = " ".join(f"{p}:{int(self.lat[p] * 1000)}" for p in sorted(self.lat))
        old = [str(p) for p, v in sorted(self.colour_dp.items()) if v == LAMP_DP_COLOUR]
        return (f"на связи {sum(self.ok.values())}/{len(self.lamps)}, команд {self.sent} (без ожидания {self.fast}), "
                f"сбоев {self.fails}" + (f", ответ ламп мс {lat}" if lat else "") + (f", без канала музыки: {','.join(old)}" if old else "")
                + (f", уходов на DP 24: {self.dp24_count}" if self.dp24_count else "")
                + (f", контрольных без ответа: {self.dp28_unacked} (~{int(self.miss_s * 1000)} мс)" if self.dp28_unacked else ""))

    def close(self, restore: bool = True) -> str:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        for th in self._th:
            th.join(3)
        if not restore:
            return ""
        done = 0
        for d in self.lamps:
            was = self.prev.get(d["pos"])
            if not was:
                continue
            try:
                b = self.factory(d)
                rest = {k: v for k, v in was.items() if k != "21"}
                if rest:
                    b.set_multiple_values(rest)
                    time.sleep(0.3)
                if "21" in was:
                    b.set_value("21", was["21"])     # режим ПОСЛЕДНИМ (09.11 11:53)
                done += 1
            except Exception:  # noqa: BLE001
                pass
        return f"лампам вернул прежний свет: {done}/{len(self.lamps)}"


# --- текст и ползунки -----------------------------------------------------------------------------------

def describe(s: State) -> str:
    if s.source == "video":
        return "за картинкой: подсветку ведёт сам ТВ, лампы -- её средний цвет"
    how = "" if s.music else f", без музыки, яркость {s.bright}%"
    if s.mode == STATIC:
        r, g, b = s.rgb
        return f"статика, цвет R{r} G{g} B{b} ({hexs(s.rgb)}){how}"
    return f"переход {hexs(s.a)} → {hexs(s.b)}{how}"


def title(s: State) -> str:
    if s.source == "video":
        return "режим за картинкой (цвет считает ТВ)"
    return "режим под музыку" if s.music else "режим свой свет (без музыки)"


def alive(st: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return bool(st.get("running")) and (now - float(st.get("beat") or 0)) <= BEAT_FRESH_S


def panel(s: State, st: dict, now: float | None = None) -> tuple[str, list[list[tuple[str, str]]]]:
    """Текст + кнопки (подпись, callback_data). Telegram-объекты собирает бот: модуль тестируется без telegram."""
    run = alive(st, now)
    head = "▶ работает" if run else "⏹ остановлена" + (f" ({st['reason']})" if st.get("reason") else "")
    name = "🎬 Подсветка за картинкой" if s.source == "video" else (
        "🎵 Подсветка под музыку" if s.music else "💡 Подсветка, свой свет")
    lines = [f"{name}: {head}", f"Режим: {describe(s)}"]
    if s.mode == GRADIENT and s.source == "own":
        lines.append(f"Ползунки правят: {'начало' if s.edit == 'a' else 'конец'} перехода")
    if run:
        if s.source == "video":
            lines.append(f"Чтений цвета с ТВ {st.get('frames', 0)}, сбоев {st.get('fails', 0)}, режим ТВ "
                         f"{st.get('tv_mode', '?')}, цвет {st.get('video_rgb', '—')}")
        else:
            lines.append(f"Кадров {st.get('frames', 0)}, сбоев {st.get('fails', 0)}, режим ТВ {st.get('tv_mode', '?')}")
        lines.append(f"🎙 микрофон: {st.get('mic', '?')} (только громкость, звук не сохраняется)")
        if s.lamps != "off":
            lines.append(f"💡 Лампы: {LAMP_SAY[s.lamps]} — {st.get('lamps', 'ещё не подключены')}")
    cur = s.rgb if s.mode == STATIC else (s.a if s.edit == "a" else s.b)
    rows: list[list[tuple[str, str]]] = [[((f"● {SOURCE_SAY[x]}" if s.source == x else SOURCE_SAY[x]), f"amb:src:{x}")
                                          for x in SOURCES]]
    if s.source == "own":                     # за картинкой цвет считает ТВ: ползунки и музыка не нужны
        rows.append([("● Статика" if s.mode == STATIC else "Статика", "amb:m:static"),
                     ("● Переход" if s.mode == GRADIENT else "Переход", "amb:m:gradient")])
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
    rows.append([((f"● 💡 {LAMP_SAY[m].capitalize()}" if s.lamps == m else f"💡 {LAMP_SAY[m].capitalize()}"), f"amb:l:{m}")
                 for m in LAMP_BUTTONS])
    if s.lamps != "off":
        rows.append([((f"● {LAMP_FX_SAY[f]}" if s.lamp_fx == f else LAMP_FX_SAY[f]), f"amb:lf:{f}") for f in LAMP_FX])
    if s.lamps != "off" and s.music and s.source == "own":
        rows.append([("⚡−", "amb:lg:-0.1"), (f"⚡ резкость {int(round(s.lamp_gain * 100))}%", "amb:nop"),
                     ("⚡+", "amb:lg:+0.1")])
        rows.append([("🌙−", "amb:ld:-0.1"), (f"🌙 затухание {s.lamp_decay:g} с", "amb:nop"), ("🌙+", "amb:ld:+0.1")])
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
    if op == "src" and len(parts) == 3 and parts[2] in SOURCES:
        s.source = parts[2]
        return s, "save"
    if op == "lf" and len(parts) == 3 and parts[2] in LAMP_FX:
        s.lamp_fx = parts[2]
        return s, "save"
    if op in ("lg", "ld") and len(parts) == 3:
        try:
            dv = float(parts[2])
        except ValueError:
            return s, ""
        if op == "lg":
            s.lamp_gain = round(max(0.5, min(3.0, s.lamp_gain + dv)), 2)
        else:
            s.lamp_decay = round(max(0.15, min(2.0, s.lamp_decay + dv)), 2)
        return s, "save"
    if op == "l" and len(parts) == 3 and parts[2] in LAMP_BUTTONS:
        s.lamps = parts[2]
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
        if not s.music or s.source == "video":   # микрофон не нужен -- можно в службе
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
    """Чтение с ТВ с повтором. ТВ иногда отдаёт пустой ответ вместо JSON (наблюдалось 03.11; живой случай 04.11 22:42:
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


def enter_source(tv, source: str, prev: dict | None) -> str:
    """Перевести ТВ под источник цвета; ответ -- режим, прочитанный с ТВ. video: стиль FOLLOW_VIDEO (прежняя
    настройка, если до моста стоял он же), режим internal; own: режим expert (лампы ТВ пишет мост)."""
    if source == "video":
        cfg = (prev or {}).get("config") or {}
        same = str(cfg.get("styleName") or "").upper() == "FOLLOW_VIDEO" and cfg.get("menuSetting")
        tv.set_ambilight_mode("internal")
        tv.set_ambilight_style("FOLLOW_VIDEO", cfg.get("menuSetting") if same else VIDEO_SETTING)
        time.sleep(0.6)
    else:
        tv.set_ambilight_mode("expert")
    return _mode_of(tv)


def _want_mode(source: str) -> str:
    return "internal" if source == "video" else "expert"


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
        st["tv_mode"] = enter_source(tv, s.source, prev)
        topology = _retry(tv.get_topology) or DEFAULT_TOPOLOGY
    except Exception as e:  # noqa: BLE001
        st.update(running=False, reason=f"телевизор недоступен: {type(e).__name__}: {e}"[:200])
        _put(status, st)
        return 1
    if st["tv_mode"] != _want_mode(s.source):
        st.update(running=False, reason=f"ТВ не принял режим {_want_mode(s.source)} (прочитано {st['tv_mode']})")
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
    # --- Wi-Fi лампы (AMB_LAMPS 09.11): отдельные потоки, ТВ от них не зависит ---
    env, lamps_out, last_lamp = Envelope(), None, 0.0
    cur_src, last_vid, vid_rgb = s.source, 0.0, None   # AMB_VIDEO: источник, который сейчас стоит на ТВ
    lamp_base_dir = control.parent

    def lamps_sync() -> None:
        nonlocal lamps_out
        if s.lamps != "off" and lamps_out is None:
            layout = load_lamp_layout(lamp_base_dir)
            if layout:
                lamps_out = LampOut(layout).start()
                print(f"мост: лампы {LAMP_SAY[s.lamps]} -- {len(layout)} шт.", flush=True)
            else:
                st["lamps"] = "нет раскладки (data\\amb_lamps.json / data\\tuya\\devices.json)"
        elif s.lamps == "off" and lamps_out is not None:
            st["lamps_restored"] = lamps_out.close(restore=True)
            lamps_out = None
            st.pop("lamps", None)

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
                if s.source != cur_src:      # 🎨 <-> 🎬 на лету
                    got = enter_source(tv, s.source, prev)
                    if got != _want_mode(s.source):
                        reason = f"ТВ не принял режим {_want_mode(s.source)} (прочитано {got})"
                        break
                    cur_src, last_mode, last_lvl, vid_rgb = s.source, now, -1.0, None
                    st["tv_mode"] = got
                    st.pop("video_rgb", None)
                mic(s.music and s.source == "own")
                lamps_sync()
            if s.music and s.source == "own" and now - music_since > s.max_hours * 3600:
                reason = f"предел {s.max_hours:g} ч под музыку"
                break
            if now - last_mode >= MODE_CHECK_S:
                last_mode = now
                st["tv_mode"] = _mode_of(tv)
                if st["tv_mode"] not in (_want_mode(s.source), "?"):
                    reason = f"режим подсветки сменили (прочитано {st['tv_mode']}) — не спорю с пультом"
                    break
                if s.source == "video":
                    try:
                        style = str((_retry(tv.get_ambilight_config) or {}).get("styleName") or "?")
                    except Exception:  # noqa: BLE001
                        style = "?"
                    if style not in ("FOLLOW_VIDEO", "?"):
                        reason = f"стиль подсветки сменили (прочитано {style}) — не спорю с пультом"
                        break
            if s.source == "video":
                # ТВ сам ведёт подсветку; мост только читает её цвет и даёт лампам. В ТВ ничего не пишется.
                v = 0.0
                if now - last_vid >= VIDEO_READ_S:
                    last_vid = now
                    try:
                        rgb = video_rgb(tv._call("GET", "ambilight/processed"))  # noqa: SLF001
                        last_ok = now
                        if rgb is not None:
                            vid_rgb = rgb
                            st["frames"] += 1
                            st["video_rgb"] = hexs(rgb)
                    except Exception:  # noqa: BLE001
                        st["fails"] += 1
                        if now - last_ok > TV_SILENT_S:
                            reason = f"ТВ не отвечает {int(TV_SILENT_S)} с"
                            break
                if lamps_out is not None and vid_rgb is not None and now - last_lamp >= LAMP_PUSH_S:
                    last_lamp = now
                    lamps_out.fx = s.lamp_fx
                    for p, k in video_levels(len(lamps_out.lamps)).items():
                        lamps_out.push(p, tuya_hsv(lamp_wb(vid_rgb), k))
                    st["lamps"] = lamps_out.stats()
                if now - last_beat >= 1.0:
                    last_beat = now
                    st.update(beat=time.time(), level=0.0, music=False, source="video")
                    _put(status, st)
                time.sleep(0.02)
                continue
            if s.music and allowed:
                with lock:
                    v = level["v"]
                due = now - last_sent >= SEND_GAP_S and abs(v - last_lvl) >= ab.MIN_DELTA
            else:
                # Без музыки -- ровный свет. Повтор раз в KEEP_S: ТВ мог пересчитать пиксели, а молчание ТВ
                # (выключили) замечается только по сбою отправки.
                v = s.bright / 100.0
                due = v != last_lvl or now - last_sent >= KEEP_S
            if lamps_out is not None:
                env.decay = s.lamp_decay
                lamps_out.fx = s.lamp_fx
                env.push(now, v)
                if now - last_lamp >= LAMP_PUSH_S:
                    last_lamp = now
                    if s.music and allowed:
                        lv = lamp_levels(s.lamps, now, env, n=len(lamps_out.lamps), gain=s.lamp_gain, floor=LAMP_FLOOR)
                    else:
                        lv = lamp_levels("sync", now, env, n=len(lamps_out.lamps))
                    bc = lamp_base(s, n=len(lamps_out.lamps))
                    for p in bc:
                        lamps_out.push(p, tuya_hsv(bc[p], lv[p]))
                    st["lamps"] = lamps_out.stats()
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
                st.update(beat=time.time(), level=round(v, 2), music=s.music, source="own")
                _put(status, st)
            time.sleep(0.02)
    except Exception as e:  # noqa: BLE001
        reason = f"сбой: {type(e).__name__}: {e}"[:200]
    finally:
        try:
            mic(False)
        except Exception:  # noqa: BLE001
            pass
        if lamps_out is not None:
            try:
                st["lamps_restored"] = lamps_out.close(restore=True)
            except Exception as e:  # noqa: BLE001
                st["lamps_restored"] = f"лампы: возврат не удался ({type(e).__name__})"
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
