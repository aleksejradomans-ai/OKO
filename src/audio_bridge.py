r"""audio_bridge.py — синхрон: уровень звука на ПК -> подсветка телевизора.

Задача. Телевизор свой звук анализирует сам (FOLLOW_AUDIO), но звука с ПК
он не слышит: колонки подключены к компьютеру, в телевизор сигнал не
идёт. Значит реакция подсветки на то, что играет НА ПК, возможна только
через нас: читаем уровень у себя, пересчитываем в цвет, отдаём телевизору
в режиме FOLLOW_COLOR. Это и есть передача между картой сигнала на ПК и
картой вывода на телевизоре.

Что здесь проверяемо, а что нет. Арифметика — отображение уровня в цвет,
сглаживание, ограничение частоты отправки — чистая и покрыта тестами
(`test_audio_bridge.py`). Захват с устройства и отправка по сети живут в
`run_bridge` и проверяются на машине: это устройства и HTTPS, их в тестах
подменять бессмысленно, зато легко подменить сам смысл проверки.

ЧЕСТНО ПРО ЧАСТОТУ. Каждая отправка — POST по HTTPS с digest-авторизацией,
то есть десятки миллисекунд в лучшем случае. Реально выходит 5-8 обновлений
в секунду, и это ПОТОЛОК, а не цель: подсветка пойдёт за огибающей
громкости, а не за ударами. Попадание в бит по такому каналу недостижимо,
и обещать его нельзя.

Поэтому отправка идёт из отдельного потока по принципу «важно последнее»:
захват никогда не ждёт сеть, а телевизор получает самое свежее значение, а
не очередь устаревших. Очередь здесь была бы хуже всего — она копит
задержку и подсветка начинает отставать от звука тем сильнее, чем дольше
работает.

ЗАХВАТ. На этой машине WASAPI loopback недоступен (старая сборка
sounddevice: `WasapiSettings(loopback=True)` не принимается), но есть
«Стерео микшер (Realtek HD Audio Stereo in)» — обычное устройство ВВОДА,
которое отдаёт то, что играет. Это тот же результат без обновления
пакетов, поэтому мост работает через выбор входного устройства, а не
через loopback-флаг.

Стерео микшер на этой машине виден ТОЛЬКО через host API «Windows WDM-KS»
(#11), а он не поддерживает чтение по запросу: `stream.read()` падает с
`Blocking API not supported yet [PaErrorCode -9999]`. Поэтому захват идёт
колбэком — PortAudio сам вызывает нашу функцию на каждый блок. Это не
обходной путь, а единственный поддерживаемый режим для WDM-KS, и он же
корректнее по сути: блок обрабатывается тогда, когда он готов, без
опроса. Побочная польза — переполнения буфера видно сразу, по флагу
состояния, а не по пропавшему звуку.

Если устройство «Стерео микшер» включить в параметрах звука Windows
(«Запись» -> показать отключённые -> включить), оно появится и в MME, и в
WASAPI, где работает и чтение по запросу. Код от этого не зависит:
колбэк работает в обоих случаях.

ОТКЛЮЧЁННЫЙ МИКШЕР ОТДАЁТ ТИШИНУ, А НЕ ОШИБКУ. Проверено 27.10: поток
открылся, блоки шли ровно (500 блоков, ни одного переполнения), а уровень
стоял около -90 dBFS, то есть цифровая тишина. Это важный случай: по
отсутствию ошибки нельзя заключить, что захват работает. Поэтому мост
сам говорит об этом в конце прогона, а не оставляет человека гадать,
почему подсветка не шевелится.

Второй путь, на случай если микшер в системе мёртв (на новых драйверах
Realtek его часто нет вовсе): читать само устройство ВЫВОДА через WASAPI
loopback.

ВАЖНО, проверено 27.10: `sounddevice` этого НЕ УМЕЕТ — ни 0.5.5, ни 0.5.6
не принимают `WasapiSettings(loopback=True)`. PortAudio флаг поддерживает,
но обёртка его не пробрасывает, поэтому обновление пакета задачу не решает
(обновили — ошибка та же). Рабочий путь — библиотека `soundcard`, у неё
loopback заявлен как штатная возможность:
`get_microphone(speaker.name, include_loopback=True)`.

Отсюда два бэкенда захвата:
  --backend sd (по умолчанию) — PortAudio/sounddevice, устройство ВВОДА
      (стерео микшер). Колбэк, int16.
  --backend sc — soundcard, устройство ВЫВОДА через loopback. Отдаёт
      float32, поэтому проходит через `audio_meter.pcm16_from_float`
      (обрезка вместо переполнения — см. тесты измерителя).
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio_meter

# Границы рабочего диапазона в dBFS. Ниже FLOOR — считаем тишиной, выше
# CEIL — упор. Диапазон узкий сознательно: обычная музыка живёт около
# -30..-12 dBFS, и если растянуть шкалу на весь -90..0, подсветка почти
# не будет шевелиться.
FLOOR_DBFS = -48.0
CEIL_DBFS = -12.0

# Сглаживание: доля нового значения в каждом шаге. Без него подсветка
# дёргается на каждом слоге, с ним — следует за огибающей.
# Отдельные коэффициенты на рост и спад: атака быстрая, чтобы удар был
# виден, спад медленный, чтобы между словами не проваливалось в темноту.
ATTACK = 0.55
RELEASE = 0.12

# Не отправлять чаще, чем раз в это время. Канал всё равно не успеет, а
# лишние запросы только копят задержку.
MIN_INTERVAL_S = 0.14

# Не отправлять, если изменение меньше этого: телевизор не отличит, а
# запрос стоит столько же, сколько осмысленный.
MIN_DELTA = 0.03

SAT = 255

# --- пространственная раскладка по светодиодам ---------------------------
# Топология «тв амби»: left 3, top 7, right 3, bottom 0 — 13 маркеров.
# Левый столбец показывает ЛЕВЫЙ канал, правый — ПРАВЫЙ, верхняя полоса —
# общий уровень. Это карта сигнала в пространстве, а не определение
# положения источника: в сведённом стерео нет данных о том, где источник
# находится, есть только соотношение каналов.


def hue_to_rgb(hue: int, value: float = 1.0) -> tuple[int, int, int]:
    """Оттенок 0..255 (как в `ambilight/lounge`) -> RGB при полной
    насыщенности. Шесть секторов по 42-43 единицы; значение value
    масштабирует яркость.
    """
    h = (int(hue) % 256) / 256.0 * 6.0
    v = clamp(value, 0.0, 1.0) * 255.0
    i = int(h) % 6
    f = h - int(h)
    p, q, t = 0.0, v * (1.0 - f), v * f
    table = ((v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q))
    r, g, b = table[i]
    return int(round(r)), int(round(g)), int(round(b))


def bar_pixels(level: float, count: int, hue: int) -> dict[str, dict[str, int]]:
    """Полоска из `count` светодиодов, заполненная на `level`.

    Частично попавший светодиод горит пропорционально — иначе на трёх
    точках шкала была бы грубой: три ступени вместо плавного хода.
    """
    out: dict[str, dict[str, int]] = {}
    lvl = clamp(level, 0.0, 1.0)
    filled = lvl * count
    for i in range(count):
        share = clamp(filled - i, 0.0, 1.0)
        if share <= 0.0:
            out[str(i)] = {"r": 0, "g": 0, "b": 0}
            continue
        r, g, b = hue_to_rgb(hue, share)
        out[str(i)] = {"r": r, "g": g, "b": b}
    return out


def render_pixels(level_left: float, level_right: float, topology: dict,
                  hue: int) -> dict[str, dict[str, dict[str, int]]]:
    """Полная раскладка: столбцы по каналам, верх — общий уровень.

    `bottom` пропускается, если светодиодов там нет (на этом телевизоре их
    ноль) — отправлять сторону с нулём точек бессмысленно, а клиент на это
    справедливо отвечает ошибкой.
    """
    lvl_l = clamp(level_left, 0.0, 1.0)
    lvl_r = clamp(level_right, 0.0, 1.0)
    overall = max(lvl_l, lvl_r)
    out: dict[str, dict[str, dict[str, int]]] = {}
    for side, lvl in (("left", lvl_l), ("right", lvl_r), ("top", overall)):
        count = int(topology.get(side, 0) or 0)
        if count > 0:
            out[side] = bar_pixels(lvl, count, hue)
    return out


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def level_from_dbfs(dbfs: float,
                    floor: float = FLOOR_DBFS,
                    ceil: float = CEIL_DBFS) -> float:
    """dBFS -> 0..1 по линейной шкале ДЕЦИБЕЛ, не амплитуды.

    По амплитуде почти весь диапазон музыки сжался бы у нуля: разница
    между -40 и -20 dBFS — это 0.01 и 0.1 по амплитуде, глазом такое не
    различить. В децибелах шаг равномерный, и подсветка отрабатывает
    весь диапазон.
    """
    if ceil <= floor:
        raise ValueError(f"ceil ({ceil}) должен быть больше floor ({floor})")
    if dbfs == -math.inf:
        return 0.0
    return clamp((dbfs - floor) / (ceil - floor), 0.0, 1.0)


@dataclass
class Smoother:
    """Экспоненциальное сглаживание с разной атакой и спадом."""

    attack: float = ATTACK
    release: float = RELEASE
    value: float = 0.0

    def __post_init__(self) -> None:
        for name, k in (("attack", self.attack), ("release", self.release)):
            if not 0.0 < k <= 1.0:
                raise ValueError(f"{name} должен быть в (0, 1], получено {k}")

    def push(self, target: float) -> float:
        k = self.attack if target > self.value else self.release
        self.value += (target - self.value) * k
        return self.value


def level_to_color(level: float, hue: int = 0) -> tuple[int, int, int]:
    """0..1 -> (hue, saturation, brightness) для `ambilight/lounge`.

    Яркость не опускается до нуля: нулевая даёт мгновенное погасание, и
    на паузах между фразами подсветка мигала бы. Нижняя граница — слабое
    свечение, то есть «тихо», а не «выключено».
    """
    lvl = clamp(level, 0.0, 1.0)
    brightness = int(round(12 + lvl * (255 - 12)))
    return int(hue) % 256, SAT, brightness


class Pacer:
    """Пропускает отправку, если рано или если изменение незначимо.

    Решение «отправлять или нет» вынесено сюда и проверяется тестами
    отдельно от сети: именно здесь легче всего получить либо поток
    бесполезных запросов, либо замерзшую подсветку.
    """

    def __init__(self, min_interval_s: float = MIN_INTERVAL_S,
                 min_delta: float = MIN_DELTA) -> None:
        self.min_interval_s = float(min_interval_s)
        self.min_delta = float(min_delta)
        self._last_sent: float | None = None
        self._last_at: float = -math.inf

    def should_send(self, level: float, now: float) -> bool:
        if self._last_sent is None:
            return True
        if now - self._last_at < self.min_interval_s:
            return False
        return abs(level - self._last_sent) >= self.min_delta

    def mark(self, level: float, now: float) -> None:
        self._last_sent = level
        self._last_at = now


# --------------------------------------------------------------------------
# Ввод-вывод: устройства и сеть. Тестами не покрывается (см. docstring).
# --------------------------------------------------------------------------

def list_inputs() -> int:
    try:
        import sounddevice as sd
    except ImportError:
        print("нет пакета sounddevice: pip install sounddevice")
        return 2
    print(f"{'#':>3} {'in':>3} {'sr':>7}  host  name")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        host = sd.query_hostapis(d["hostapi"])["name"]
        hint = ""
        name = d["name"].lower()
        if "stereo" in name or "микшер" in name or "mix" in name:
            hint = "  <- похоже на стерео микшер: отдаёт то, что играет"
        print(f"{i:>3} {d['max_input_channels']:>3} "
              f"{int(d['default_samplerate']):>7}  {host[:12]:<12} "
              f"{d['name'][:38]}{hint}")
    return 0


def _play_test_tone(delay_s: float = 1.0, seconds: float = 3.0,
                    freq: int = 440, amp: float = 0.25,
                    sample_rate: int = 48000) -> None:
    """Проигрывает тон на устройстве вывода по умолчанию — для самопроверки.

    Зачем это нужно. Захват может работать идеально и при этом показывать
    тишину просто потому, что в момент замера ничего не играло, или
    потому, что звук идёт на ДРУГОЙ выход, а не на тот, который мы
    слушаем. Эти два случая в выводе выглядят одинаково: `-inf dBFS`.
    Отличить их можно только одним способом — заставить звук играть самим
    и посмотреть, виден ли он в захвате.
    """
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        return
    time.sleep(delay_s)
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    wave = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype("<i2")
    try:
        sd.play(wave, sample_rate)
        sd.wait()
    except Exception as e:
        print(f"самопроверка: не удалось проиграть тон: {type(e).__name__}: {e}")


def _selftest_verdict(peak_dbfs: float, backend: str) -> None:
    """Вывод по самопроверке. Разделяет три разных диагноза, которые без
    тона неразличимы."""
    print("--- самопроверка")
    if peak_dbfs == -math.inf:
        print("тон НЕ ВИДЕН вовсе (ровные нули). Захват слушает не тот тракт, "
              "по которому идёт звук.")
        if backend == "sc":
            print("проверьте, совпадает ли устройство вывода по умолчанию с тем, "
                  "что печатает «захват:» выше; список: python -c "
                  "\"import soundcard; print(soundcard.all_speakers())\"")
        else:
            print("для входа «Стерео микшер» это обычно значит, что он "
                  "отключён или заглушён в параметрах звука Windows")
    elif peak_dbfs < audio_meter.SILENCE_DBFS:
        print(f"тон еле заметен (пик {peak_dbfs:.1f} dBFS): тракт тот, но "
              f"уровень захвата почти на нуле — поднимите уровень устройства "
              f"записи в параметрах звука")
    else:
        print(f"тон ВИДЕН (пик {peak_dbfs:.1f} dBFS): тракт захвата рабочий. "
              f"Если музыка при этом не видна — она играет в другое "
              f"устройство вывода")


def _make_sender(tv, hue: int, sent: dict, latest: dict, lock, stop):
    """Поток отправки: «важно последнее», захват никогда не ждёт сеть."""
    pacer = Pacer()

    def sender() -> None:
        while not stop.is_set():
            now = time.monotonic()
            with lock:
                level = latest["level"]
            if pacer.should_send(level, now):
                h, s, b = level_to_color(level, hue)
                if tv is not None:
                    try:
                        tv.set_lounge_color(h, s, b)
                        sent["n"] += 1
                    except Exception:
                        sent["fail"] += 1
                else:
                    sent["n"] += 1
                pacer.mark(level, now)
            time.sleep(0.02)

    return sender


def run_soundcard(device: str | None, seconds: float, hue: int,
                  dry_run: bool, selftest: bool = False,
                  floor: float = FLOOR_DBFS, ceil: float = CEIL_DBFS,
                  block_ms: int = 60) -> int:
    """Захват устройства ВЫВОДА через soundcard (WASAPI loopback).

    НЕ ПРОВЕРЕНО на машине Оператора: библиотека может быть не
    установлена, а поведение loopback зависит от драйвера. Поэтому путь
    отдельный, а не подмена основного: если он не заработает, основной
    остаётся прежним.
    """
    try:
        import soundcard as sc
    except ImportError:
        print("нет пакета soundcard: pip install soundcard")
        return 2

    tv = None
    if not dry_run:
        try:
            from tv_control import TVClient, TVError
            tv = TVClient.from_env()
            tv.set_ambilight_power(True)
            tv.set_ambilight_style("FOLLOW_COLOR", "MANUAL_HUE")
        except Exception as e:
            print(f"телевизор недоступен: {e}")
            return 1

    try:
        speaker = sc.default_speaker() if device is None else sc.get_speaker(device)
        mic = sc.get_microphone(str(speaker.name), include_loopback=True)
    except Exception as e:
        print(f"не удалось открыть loopback: {type(e).__name__}: {e}")
        print("список устройств: python -c \"import soundcard;"
              "print(soundcard.all_speakers())\"")
        return 1

    sr = 48000
    frames = max(1, int(sr * block_ms / 1000))
    print(f"захват: {speaker.name} [soundcard loopback] {sr} Гц")
    print(f"отправка: {'НЕТ (dry-run)' if dry_run else 'ambilight/lounge'}  "
          f"hue={hue}  потолок {1 / MIN_INTERVAL_S:.1f} обн./с")

    smoother = Smoother()
    stop = threading.Event()
    latest = {"level": 0.0}
    lock = threading.Lock()
    sent = {"n": 0, "fail": 0}
    worker = threading.Thread(
        target=_make_sender(tv, hue, sent, latest, lock, stop), daemon=True)
    worker.start()

    peak_dbfs = -math.inf
    blocks = 0
    shown = 0.0
    if selftest:
        print("самопроверка: через 1 с проиграю тон 440 Гц на 3 с")
        threading.Thread(target=_play_test_tone, daemon=True).start()
    deadline = time.monotonic() + seconds
    try:
        with mic.recorder(samplerate=sr, channels=2, blocksize=frames) as rec:
            while time.monotonic() < deadline:
                data = rec.record(numframes=frames)
                pcm = audio_meter.pcm16_from_float(data)
                stats = audio_meter.analyze(pcm, channels=2, sample_rate=sr)
                level = smoother.push(level_from_dbfs(stats.rms_dbfs, floor, ceil))
                blocks += 1
                peak_dbfs = max(peak_dbfs, stats.peak_dbfs)
                with lock:
                    latest["level"] = level
                now = time.monotonic()
                if now - shown > 0.5:
                    shown = now
                    bar = "#" * int(level * 40)
                    print(f"rms {stats.rms_dbfs:7.1f}  peak {stats.peak_dbfs:7.1f}"
                          f"  lvl {level:4.2f} |{bar:<40}|"
                          f" бл {blocks} отпр {sent['n']}")
    except KeyboardInterrupt:
        print("остановлено")
    except Exception as e:
        print(f"захват не удался: {type(e).__name__}: {e}")
        stop.set()
        worker.join(timeout=1.0)
        return 1
    finally:
        stop.set()
        worker.join(timeout=1.0)

    if selftest:
        _selftest_verdict(peak_dbfs, "sc")
    elif blocks and peak_dbfs < audio_meter.SILENCE_DBFS:
        print(f"ТИШИНА за весь прогон (максимум {peak_dbfs:.1f} dBFS): "
              f"на устройстве вывода ничего не играло")
    print(f"итого отправок: {sent['n']}, сбоев: {sent['fail']}")
    return 0


def run_bridge(device: int | None, seconds: float, hue: int,
               dry_run: bool, use_loopback: bool = False,
               selftest: bool = False, floor: float = FLOOR_DBFS,
               ceil: float = CEIL_DBFS, pixels: bool = False,
               block_ms: int = 60) -> int:
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as e:
        print(f"нет пакета: {e.name}")
        return 2

    tv = None
    topology: dict = {}
    if not dry_run:
        try:
            from tv_control import TVClient, TVError
        except ImportError as e:
            print(f"нет tv_control: {e}")
            return 2
        try:
            tv = TVClient.from_env()
            tv.set_ambilight_power(True)
            if pixels:
                # Поштучная запись работает только в expert: в lounge
                # телевизор пересчитывает пиксели сам (проверено).
                tv.set_ambilight_mode("expert")
                topology = tv.get_topology() or {}
                print(f"топология: {topology}")
            else:
                tv.set_ambilight_style("FOLLOW_COLOR", "MANUAL_HUE")
        except (TVError, ValueError) as e:
            print(f"телевизор недоступен: {e}")
            return 1
    elif pixels:
        topology = {"left": 3, "top": 7, "right": 3, "bottom": 0}

    # WASAPI loopback читает УСТРОЙСТВО ВЫВОДА, поэтому и справка о нём
    # запрашивается как об output: у такого устройства max_input_channels
    # равен нулю, и обычный путь выбрал бы ноль каналов.
    extra = None
    if use_loopback:
        try:
            extra = sd.WasapiSettings(loopback=True)
        except Exception as e:
            print(f"WASAPI loopback недоступен в этой сборке sounddevice "
                  f"({type(e).__name__}). Обновите: pip install -U sounddevice, "
                  f"либо используйте вход «Стерео микшер» без --loopback")
            return 2
        info = (sd.query_devices(device, "output") if device is not None
                else sd.query_devices(kind="output"))
        channels = min(2, int(info["max_output_channels"]))
    else:
        info = (sd.query_devices(device, "input") if device is not None
                else sd.query_devices(kind="input"))
        channels = min(2, int(info["max_input_channels"]))
    sr = int(info["default_samplerate"])
    print(f"захват: #{device if device is not None else 'default'} "
          f"{info['name']} {sr} Гц {channels} кан."
          f"{'  [WASAPI loopback]' if use_loopback else ''}")
    if dry_run:
        target = "НЕТ (dry-run)"
    elif pixels:
        target = "ambilight/cached (expert, поштучно)"
    else:
        target = "ambilight/lounge (заливка)"
    print(f"отправка: {target}  "
          f"hue={hue}  диапазон {floor:.0f}..{ceil:.0f} dBFS  "
          f"потолок {1 / MIN_INTERVAL_S:.1f} обн./с")

    smoother = Smoother()
    smooth_l, smooth_r = Smoother(), Smoother()
    pacer = Pacer()
    stop = threading.Event()

    # «Важно последнее»: поток отправки берёт текущее значение, а не
    # очередь. Очередь копила бы задержку — подсветка отставала бы от
    # звука тем сильнее, чем дольше работает мост.
    latest = {"level": 0.0, "left": 0.0, "right": 0.0}
    lock = threading.Lock()
    sent = {"n": 0, "fail": 0}

    def sender() -> None:
        while not stop.is_set():
            now = time.monotonic()
            with lock:
                level = latest["level"]
                lvl_l, lvl_r = latest["left"], latest["right"]
            if pacer.should_send(level, now):
                if tv is not None:
                    try:
                        if pixels:
                            tv.set_pixels(render_pixels(lvl_l, lvl_r, topology, hue))
                        else:
                            h, s, b = level_to_color(level, hue)
                            tv.set_lounge_color(h, s, b)
                        sent["n"] += 1
                    except Exception:
                        sent["fail"] += 1
                else:
                    sent["n"] += 1
                pacer.mark(level, now)
            time.sleep(0.02)

    worker = threading.Thread(target=sender, daemon=True)
    worker.start()

    frames = max(1, int(sr * block_ms / 1000))

    # Состояние, которое колбэк отдаёт наружу. Колбэк вызывается из
    # аудиопотока PortAudio, поэтому в нём нельзя ни ждать сеть, ни
    # печатать: только посчитать и положить результат.
    shared = {"dbfs": -math.inf, "block_peak": -math.inf,
              "peak_dbfs": -math.inf, "over": 0, "blocks": 0}

    def on_block(indata, n_frames, time_info, status) -> None:
        if status:
            shared["over"] += 1
        pcm = bytes(memoryview(indata).cast("B"))
        stats = audio_meter.analyze(pcm, channels=channels, sample_rate=sr)
        level = smoother.push(level_from_dbfs(stats.rms_dbfs, floor, ceil))
        # Каналы считаются отдельно: столбцы подсветки показывают именно
        # левый и правый, а не общий уровень дважды.
        per = stats.per_channel_rms or [0.0]
        lvl_l = smooth_l.push(level_from_dbfs(audio_meter.dbfs(per[0]), floor, ceil))
        lvl_r = smooth_r.push(
            level_from_dbfs(audio_meter.dbfs(per[-1]), floor, ceil))
        shared["dbfs"] = stats.rms_dbfs
        shared["block_peak"] = stats.peak_dbfs
        if stats.peak_dbfs > shared["peak_dbfs"]:
            shared["peak_dbfs"] = stats.peak_dbfs
        shared["blocks"] += 1
        with lock:
            latest["level"] = level
            latest["left"] = lvl_l
            latest["right"] = lvl_r

    if selftest:
        print("самопроверка: через 1 с проиграю тон 440 Гц на 3 с")
        threading.Thread(target=_play_test_tone, daemon=True).start()
    deadline = time.monotonic() + seconds
    try:
        with sd.InputStream(device=device, samplerate=sr, channels=channels,
                            dtype="int16", blocksize=frames,
                            callback=on_block, extra_settings=extra):
            while time.monotonic() < deadline:
                time.sleep(0.5)
                with lock:
                    level = latest["level"]
                    lvl_l, lvl_r = latest["left"], latest["right"]
                bar = "#" * int(level * 40)
                # rms и peak — мгновенные, по последнему блоку; lvl —
                # сглаженный. Без пика строка «-inf dBFS 0.88» читается как
                # противоречие, хотя это просто спад сглаживателя после
                # окончания звука.
                chans = f" L{lvl_l:4.2f} R{lvl_r:4.2f}" if pixels else ""
                print(f"rms {shared['dbfs']:7.1f}  peak {shared['block_peak']:7.1f}"
                      f"  lvl {level:4.2f}{chans} |{bar:<40}|"
                      f" бл {shared['blocks']} отпр {sent['n']}"
                      + (f"  OVERFLOW x{shared['over']}" if shared["over"] else ""))
    except KeyboardInterrupt:
        print("остановлено")
    except Exception as e:
        print(f"захват не удался: {type(e).__name__}: {e}")
        if "-9999" in str(e) or "Blocking" in str(e):
            print("подсказка: включите «Стерео микшер» в параметрах звука "
                  "Windows (Запись -> показать отключённые устройства), "
                  "тогда он появится в MME/WASAPI — попробуйте его номер "
                  "из --list-inputs")
        stop.set()
        return 1
    finally:
        stop.set()
        worker.join(timeout=1.0)

    if selftest:
        _selftest_verdict(shared["peak_dbfs"], "sd")
    elif shared["blocks"] == 0:
        print("ни одного блока не пришло: устройство не отдало данных")
    elif shared["peak_dbfs"] < audio_meter.SILENCE_DBFS:
        # Разделение диагнозов: поток шёл, ошибок не было, но за всё время
        # не появилось НИ ОДНОГО звука выше порога тишины. Для устройства
        # вывода это значит «ничего не играло», для входа — почти всегда
        # «устройство отключено в системе»: отключённый микшер виден через
        # WDM-KS и исправно отдаёт нули.
        print(f"ТИШИНА за весь прогон (максимум {shared['peak_dbfs']:.1f} dBFS "
              f"при пороге {audio_meter.SILENCE_DBFS:.0f}).")
        if use_loopback:
            print("устройство вывода молчало — проверьте, что звук реально играл")
        else:
            print("похоже, «Стерео микшер» ОТКЛЮЧЁН в параметрах звука Windows: "
                  "Звук -> Запись -> правый клик -> показать отключённые -> "
                  "включить. Если его там нет совсем (новые драйверы Realtek "
                  "его не дают), используйте: pip install -U sounddevice, "
                  "затем audio_bridge.py --loopback")

    print(f"итого отправок: {sent['n']}, сбоев: {sent['fail']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-inputs", action="store_true")
    ap.add_argument("--device", type=int, default=None,
                    help="номер входного устройства (стерео микшер)")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--hue", type=int, default=0, help="оттенок 0..255")
    ap.add_argument("--dry-run", action="store_true",
                    help="мерить и показывать, в телевизор не писать")
    ap.add_argument("--loopback", action="store_true",
                    help="бэкенд sd: пробовать WASAPI loopback "
                         "(не поддерживается sounddevice 0.5.5/0.5.6)")
    ap.add_argument("--backend", choices=["sd", "sc"], default="sd",
                    help="sd — sounddevice (вход, стерео микшер); "
                         "sc — soundcard (выход, WASAPI loopback)")
    ap.add_argument("--speaker", default=None,
                    help="бэкенд sc: имя устройства вывода (по умолчанию системное)")
    ap.add_argument("--selftest", action="store_true",
                    help="проиграть тон самим и проверить, виден ли он в захвате")
    ap.add_argument("--speakers", action="store_true",
                    help="бэкенд sc: список устройств вывода")
    # Микрофон слышит комнату целиком, и его уровни заметно ниже, чем у
    # петли вывода: тихая комната живёт около -60 dBFS, а не -48. С
    # жёстким диапазоном подсветка на микрофоне стояла бы на нуле,
    # поэтому границы выведены в аргументы, а не зашиты.
    ap.add_argument("--floor", type=float, default=FLOOR_DBFS,
                    help=f"уровень тишины в dBFS (по умолчанию {FLOOR_DBFS:.0f}; "
                         f"для микрофона обычно -65..-55)")
    ap.add_argument("--pixels", action="store_true",
                    help="раскладывать звук по отдельным светодиодам "
                         "(режим expert): столбцы — каналы, верх — общий уровень")
    ap.add_argument("--ceil", type=float, default=CEIL_DBFS,
                    help=f"уровень полной яркости в dBFS (по умолчанию {CEIL_DBFS:.0f}; "
                         f"для микрофона обычно -25..-15)")
    args = ap.parse_args()

    if args.list_inputs:
        return list_inputs()
    if args.speakers:
        try:
            import soundcard as sc
        except ImportError:
            print("нет пакета soundcard: pip install soundcard")
            return 2
        default = sc.default_speaker()
        for sp in sc.all_speakers():
            mark = "  <- по умолчанию" if str(sp.name) == str(default.name) else ""
            print(f"{sp.name}{mark}")
        return 0
    if args.ceil <= args.floor:
        print(f"--ceil ({args.ceil}) должен быть больше --floor ({args.floor})")
        return 2
    if args.backend == "sc":
        return run_soundcard(args.speaker, args.seconds, args.hue, args.dry_run,
                             selftest=args.selftest,
                             floor=args.floor, ceil=args.ceil)
    return run_bridge(args.device, args.seconds, args.hue, args.dry_run,
                      use_loopback=args.loopback, selftest=args.selftest,
                      floor=args.floor, ceil=args.ceil, pixels=args.pixels)


if __name__ == "__main__":
    raise SystemExit(main())
