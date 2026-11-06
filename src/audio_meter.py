"""audio_meter.py — измерение того, что система собирается произнести.

Чистая арифметика над PCM: ни звуковых устройств, ни Windows API, ни
сети. Поэтому проверяется тестами целиком, в отличие от платформенной
части (устройства, сессии микшера, петля WASAPI) — она живёт в
`audio_probe.py` и подтверждается только на машине Оператора.

Разделение не косметическое. «Звук ушёл» и «звук был правильным» — разные
утверждения, и смешивать их нельзя: петля WASAPI слышит ЛЮБОЙ звук на
устройстве, включая музыку из телевизора или браузера, поэтому сама по
себе она не доказывает, что произнесено именно наше. Доказуемо здесь
только одно: что в буфере, который мы отдаём устройству, есть сигнал
нужной формы. Это и измеряется.

ПОЧЕМУ НЕ RMS ДЛЯ КЛИППИНГА (поправка к типовой схеме). У синуса полной
шкалы RMS = 0.707, у меандра полной шкалы — 1.0. Значит порог «RMS > 0.95»
недостижим ни для какой реальной речи: проверка выглядит рабочей и не
срабатывает никогда. Клиппинг — это про ОТДЕЛЬНЫЕ отсчёты, упёршиеся в
предел шкалы, поэтому он считается по пикам и по их количеству.

ПОЧЕМУ НЕ LUFS. LUFS — это K-взвешивание плюс гейтинг по окнам (BS.1770),
к RMS он не сводится, и называть одно другим — значит обещать громкостную
норму, которой нет. Здесь честный dBFS по RMS и по пику; если понадобится
настоящий LUFS, это отдельная работа с фильтрами.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# Полная шкала PCM 16 бит. Делим на 32768 (а не 32767): диапазон значений
# несимметричен (-32768..32767), и деление на 32768 даёт ровно [-1, 1).
FULL_SCALE = 32768.0

# Отсчёт считается упёршимся в предел, если его модуль на расстоянии
# меньше одного шага от края шкалы. Порог по МОДУЛЮ, потому что
# ограничение бывает и снизу (-32768), и сверху (32767).
CLIP_LEVEL = 32767

# Тишина: не «ровно нуль», а ниже слышимого порога. Нулевой буфер бывает
# у легитимной паузы, а вот весь отрывок ниже -60 dBFS означает, что
# синтез отдал пустоту или микшер зарезал поток.
SILENCE_DBFS = -60.0

# Доля упёршихся отсчётов, выше которой это уже слышно как хрип, а не
# единичный выброс на согласной.
CLIP_RATIO_WARN = 0.001


def dbfs(level: float) -> float:
    """Линейная амплитуда 0..1 -> dBFS. Нуль даёт -inf, не исключение:
    тишина — это нормальное состояние сигнала, а не ошибка счёта."""
    if level <= 0.0:
        return -math.inf
    return 20.0 * math.log10(level)


def decode_pcm16(data: bytes) -> np.ndarray:
    """Сырые байты PCM 16 бит LE -> массив int16.

    Нечётный хвост отбрасывается, а не ломает разбор. Буфер приходит из
    потока, и его легко получить обрезанным по границе пакета; падать на
    этом нельзя — измеритель не должен быть причиной сбоя озвучки.
    """
    if not data:
        return np.empty(0, dtype=np.int16)
    usable = len(data) - (len(data) % 2)
    return np.frombuffer(data[:usable], dtype="<i2")


def pcm16_from_float(samples: Any) -> bytes:
    """float в диапазоне [-1, 1] -> PCM 16 бит LE.

    Нужно для источников, которые отдают float: WASAPI loopback через
    библиотеку `soundcard` возвращает float32, а весь измеритель и
    телевизорная часть работают с PCM16. Преобразование вынесено сюда, а
    не в мост, потому что это арифметика и её надо проверять тестами.

    Значения вне диапазона ОБРЕЗАЮТСЯ, а не переполняются: при умножении
    без обрезки 1.5 превратилось бы в отрицательное число после приведения
    к int16, то есть громкий участок дал бы противофазный треск вместо
    ограничения. Масштаб 32767, а не 32768: иначе ровная 1.0 даёт
    переполнение на единицу.

    Форма входа произвольная: (n,) или (n, channels). Перемежение каналов
    сохраняется, потому что reshape(-1) у numpy идёт по строкам — кадр за
    кадром, как и ожидает `split_channels`.
    """
    arr = np.asarray(samples, dtype=np.float64).reshape(-1)
    if arr.size == 0:
        return b""
    np.clip(arr, -1.0, 1.0, out=arr)
    return (arr * 32767.0).round().astype("<i2").tobytes()


def split_channels(samples: np.ndarray, channels: int) -> list[np.ndarray]:
    """Разделяет перемежённые отсчёты по каналам.

    Нужно именно по каналам: RMS по смешанному массиву маскирует мёртвый
    канал. Стерео с одним молчащим каналом даёт RMS ровно вдвое меньше по
    мощности — на слух это «звук только слева», а в общей цифре выглядит
    как просто потише.
    """
    if channels < 1:
        raise ValueError(f"channels должно быть >= 1, получено {channels}")
    if channels == 1:
        return [samples]
    usable = len(samples) - (len(samples) % channels)
    if usable == 0:
        return [np.empty(0, dtype=samples.dtype) for _ in range(channels)]
    frame = samples[:usable].reshape(-1, channels)
    return [frame[:, c] for c in range(channels)]


@dataclass(frozen=True)
class AudioStats:
    """Измерения по буферу. Всё в одном объекте, чтобы в телеметрию
    уходил один набор чисел, а не россыпь аргументов."""

    samples: int                 # отсчётов на канал
    channels: int
    sample_rate: int
    duration_s: float
    rms: float                   # максимум по каналам, 0..~1
    peak: float
    rms_dbfs: float
    peak_dbfs: float
    clipped: int                 # отсчётов на пределе шкалы
    clip_ratio: float
    per_channel_rms: list[float] = field(default_factory=list)

    @property
    def silent(self) -> bool:
        return self.rms_dbfs < SILENCE_DBFS

    @property
    def clipping(self) -> bool:
        return self.clip_ratio > CLIP_RATIO_WARN

    @property
    def dead_channels(self) -> list[int]:
        """Каналы, молчащие при живом сигнале в других. Пустой список,
        если молчит всё — это уже `silent`, другой диагноз."""
        if self.silent:
            return []
        return [i for i, r in enumerate(self.per_channel_rms)
                if dbfs(r) < SILENCE_DBFS]


def analyze(data: bytes, channels: int = 1, sample_rate: int = 48000) -> AudioStats:
    """Измеряет буфер PCM 16 бит LE."""
    if sample_rate <= 0:
        raise ValueError(f"sample_rate должен быть > 0, получено {sample_rate}")
    raw = decode_pcm16(data)
    chans = split_channels(raw, channels)
    n = len(chans[0])

    # Счёт во float64: квадрат int16 вылезает за int16 и в целочисленной
    # арифметике молча переполнился бы.
    rms_per_chan: list[float] = []
    peak_raw = 0
    clipped = 0
    for ch in chans:
        if len(ch) == 0:
            rms_per_chan.append(0.0)
            continue
        f = ch.astype(np.float64)
        rms_per_chan.append(float(np.sqrt(np.mean(f * f)) / FULL_SCALE))
        peak_raw = max(peak_raw, int(np.max(np.abs(f))))
        clipped += int(np.count_nonzero(np.abs(ch.astype(np.int32)) >= CLIP_LEVEL))

    rms = max(rms_per_chan) if rms_per_chan else 0.0
    peak = peak_raw / FULL_SCALE
    total = n * channels
    return AudioStats(
        samples=n,
        channels=channels,
        sample_rate=sample_rate,
        duration_s=n / sample_rate,
        rms=rms,
        peak=peak,
        rms_dbfs=dbfs(rms),
        peak_dbfs=dbfs(peak),
        clipped=clipped,
        clip_ratio=(clipped / total) if total else 0.0,
        per_channel_rms=rms_per_chan,
    )


def problems(stats: AudioStats, min_duration_s: float = 0.15) -> list[str]:
    """Список претензий к буферу. Пустой список = претензий нет.

    Список, а не bool: «не прошло» без причины бесполезно в логе, а
    причин может быть несколько сразу (короткий И с клиппингом).

    Проверять нужно ВСЮ реплику, а не каждый пакет по отдельности: в
    начале и конце синтеза тишина законна, и покадровая проверка давала
    бы ложную тревогу на каждой паузе между словами.
    """
    out: list[str] = []
    if stats.samples == 0:
        out.append("пустой буфер")
        return out
    if stats.duration_s < min_duration_s:
        out.append(f"слишком коротко: {stats.duration_s * 1000:.0f} мс "
                   f"< {min_duration_s * 1000:.0f} мс")
    if stats.silent:
        out.append(f"тишина: RMS {stats.rms_dbfs:.1f} dBFS < {SILENCE_DBFS:.0f}")
    if stats.clipping:
        out.append(f"клиппинг: {stats.clipped} отсчётов "
                   f"({stats.clip_ratio * 100:.2f}%)")
    dead = stats.dead_channels
    if dead:
        out.append(f"молчащие каналы: {dead}")
    return out


def trim_silence(data: bytes, channels: int = 1,
                 threshold_dbfs: float = SILENCE_DBFS,
                 window_ms: int = 20, sample_rate: int = 48000) -> bytes:
    """Срезает тишину по краям. Нужно для измерения длительности РЕЧИ:
    синтез часто добавляет паузу, и без обрезки «слишком коротко» не
    отличить от «длинный буфер с коротким словом».

    Обрезка идёт по КАДРАМ, а не по отдельным отсчётам: кадр — это один
    отсчёт на каждый канал, и граница внутри кадра сдвинула бы каналы
    друг относительно друга, превратив стерео в шум со смещением. При
    работе с кадрами выравнивание получается по построению, а не
    отдельной поправкой, которую легко потерять при правке.
    """
    raw = decode_pcm16(data)
    if len(raw) == 0 or channels < 1:
        return b""
    usable = len(raw) - (len(raw) % channels)
    if usable == 0:
        return b""
    frames = raw[:usable].reshape(-1, channels)

    win = max(1, int(sample_rate * window_ms / 1000))
    limit = 10 ** (threshold_dbfs / 20.0) * FULL_SCALE

    def loud(block: np.ndarray) -> bool:
        if block.size == 0:
            return True
        f = block.astype(np.float64)
        return float(np.sqrt(np.mean(f * f))) > limit

    start, end = 0, len(frames)
    while start < end and not loud(frames[start:start + win]):
        start += win
    while end > start and not loud(frames[max(start, end - win):end]):
        end -= win
    if start >= end:
        return b""
    return frames[start:end].tobytes()


def summary(stats: AudioStats) -> str:
    """Одна строка для лога. Без текста реплики: в журнал уходит форма
    сигнала, а не что было сказано."""
    return (f"n={stats.samples} ch={stats.channels} "
            f"dur={stats.duration_s:.2f}s "
            f"rms={stats.rms_dbfs:.1f}dBFS peak={stats.peak_dbfs:.1f}dBFS "
            f"clip={stats.clipped}")
