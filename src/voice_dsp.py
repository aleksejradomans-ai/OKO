"""voice_dsp.py -- офлайн-обработка речевых WAV для голоса бота: чтение/запись, громкость, паузы, нормализация.

Только numpy + scipy (уже в окружении бота); сети нет, модели нет. Используется tools/voice_cut.py и
tools/voice_check.py; при импорте ничего не делает.

Целевой формат: WAV PCM 16 бит, 24 000 Гц, моно.
Громкость:
  * integrated_lufs -- ITU-R BS.1770-4: K-фильтр (полка + ФВЧ), блоки 400 мс с шагом 100 мс, абсолютный порог
    -70 LUFS, относительный -10 LU; коэффициенты пересчитываются под частоту дискретизации (билинейное
    преобразование), как в эталонной реализации;
  * true_peak_db -- пик после 4-кратной передискретизации (полифазный фильтр), dBTP;
  * active_rms_db -- RMS по кадрам 10 мс выше порога тишины (активное речевое окно), dBFS.
Нормализация А (пачка): -18 LUFS, потолок -1,5 dBTP. Нормализация Б (клип < 1 с): -20 dBFS RMS активного окна,
потолок -1,5 dBTP. Если потолок пика не даёт достичь цели, усиление уменьшается (громкость ниже цели -- в отчёт).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

TARGET_SR = 24000
FRAME_S = 0.010
SILENCE_DBFS = -50.0
BATCH_LUFS = -18.0
CLIP_RMS_DBFS = -20.0
TP_CEIL_DBTP = -1.5
SHORT_CLIP_S = 1.0
HEAD_S = 0.040
TAIL_S = 0.120
TAIL_JOIN_S = 0.040


def db(x: float) -> float:
    return -math.inf if x <= 0 else 20.0 * math.log10(x)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """-> (моно float64 в [-1, 1], частота). PCM 8/16/24/32 бит и float WAV; стерео сводится в моно."""
    sr, x = wavfile.read(str(path))
    if x.dtype == np.uint8:
        y = (x.astype(np.float64) - 128.0) / 128.0
    elif x.dtype == np.int16:
        y = x.astype(np.float64) / 32768.0
    elif x.dtype == np.int32:
        y = x.astype(np.float64) / 2147483648.0
    elif x.dtype in (np.float32, np.float64):
        y = x.astype(np.float64)
    else:
        raise ValueError(f"формат WAV не поддерживается: {x.dtype}")
    if y.ndim == 2:
        y = y.mean(axis=1)
    return y, int(sr)


def to_target_sr(x: np.ndarray, sr: int, target: int = TARGET_SR) -> np.ndarray:
    if sr == target:
        return x
    g = math.gcd(int(sr), int(target))
    return signal.resample_poly(x, target // g, sr // g)


def write_wav(path: Path, x: np.ndarray, sr: int = TARGET_SR) -> None:
    """PCM 16 бит моно; значения за [-1, 1] обрезаются (после нормализации их нет -- потолок пика)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    y = np.clip(np.round(np.asarray(x, dtype=np.float64) * 32767.0), -32768, 32767).astype(np.int16)
    tmp = path.with_name(path.name + ".tmp")
    wavfile.write(str(tmp), sr, y)
    tmp.replace(path)


def wav_info(path: Path) -> dict:
    """Формат файла без преобразования: частота, каналы, тип отсчётов, длительность, число отсчётов на краю шкалы."""
    sr, x = wavfile.read(str(path))
    ch = 1 if x.ndim == 1 else x.shape[1]
    n = x.shape[0]
    clipped = 0
    if x.dtype == np.int16:
        clipped = int(np.count_nonzero((x == 32767) | (x == -32768)))
    return {"sr": int(sr), "channels": ch, "dtype": str(x.dtype), "seconds": n / float(sr) if sr else 0.0,
            "clipped": clipped}


# ---------------------------------------------------------------- громкость (BS.1770-4)
def _k_filters(sr: int):
    """Коэффициенты K-фильтра для частоты sr (как в pyloudnorm / BS.1770 по Mansbridge)."""
    # полка высоких частот
    G, Q, fc = 3.999843853973347, 0.7071752369554196, 1681.974450955533
    K = math.tan(math.pi * fc / sr)
    Vh = 10 ** (G / 20.0)
    Vb = Vh ** 0.4996667741545416
    a0 = 1.0 + K / Q + K * K
    b1 = [(Vh + Vb * K / Q + K * K) / a0, 2.0 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0]
    a1 = [1.0, 2.0 * (K * K - 1.0) / a0, (1.0 - K / Q + K * K) / a0]
    # ФВЧ (RLB)
    Q2, fc2 = 0.5003270373238773, 38.13547087602444
    K2 = math.tan(math.pi * fc2 / sr)
    a02 = 1.0 + K2 / Q2 + K2 * K2
    b2 = [1.0, -2.0, 1.0]
    a2 = [1.0, 2.0 * (K2 * K2 - 1.0) / a02, (1.0 - K2 / Q2 + K2 * K2) / a02]
    return (b1, a1), (b2, a2)


def integrated_lufs(x: np.ndarray, sr: int) -> float:
    """Integrated loudness моно-сигнала, LUFS; -inf для тишины или сигнала короче одного блока 400 мс."""
    x = np.asarray(x, dtype=np.float64)
    blk = int(round(0.400 * sr))
    hop = int(round(0.100 * sr))
    if x.size < blk:
        return -math.inf
    (b1, a1), (b2, a2) = _k_filters(sr)
    y = signal.lfilter(b2, a2, signal.lfilter(b1, a1, x))
    n = 1 + (y.size - blk) // hop
    z = np.array([np.mean(y[i * hop:i * hop + blk] ** 2) for i in range(n)])
    with np.errstate(divide="ignore"):
        l = -0.691 + 10.0 * np.log10(z)
    g = z[l > -70.0]
    if g.size == 0:
        return -math.inf
    rel = -0.691 + 10.0 * math.log10(float(np.mean(g))) - 10.0
    g2 = z[(l > -70.0) & (l > rel)]
    if g2.size == 0:
        return -math.inf
    return -0.691 + 10.0 * math.log10(float(np.mean(g2)))


def true_peak_db(x: np.ndarray, sr: int = TARGET_SR) -> float:
    """Истинный пик (4-кратная передискретизация), dBTP."""
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return -math.inf
    up = signal.resample_poly(x, 4, 1)
    return db(float(max(np.max(np.abs(up)), np.max(np.abs(x)))))


def frame_db(x: np.ndarray, sr: int, frame_s: float = FRAME_S) -> np.ndarray:
    """RMS по кадрам (dBFS) -- для порога тишины и активного окна."""
    f = max(1, int(round(frame_s * sr)))
    n = x.size // f
    if n == 0:
        return np.zeros(0)
    fr = x[:n * f].reshape(n, f)
    rms = np.sqrt(np.mean(fr ** 2, axis=1))
    with np.errstate(divide="ignore"):
        return 20.0 * np.log10(np.maximum(rms, 1e-12))


def active_rms_db(x: np.ndarray, sr: int, thr_db: float = SILENCE_DBFS) -> float:
    d = frame_db(x, sr)
    act = d[d > thr_db]
    if act.size == 0:
        return -math.inf
    return 10.0 * math.log10(float(np.mean(10.0 ** (act / 10.0))))


def _gain_with_ceiling(x: np.ndarray, sr: int, gain_db: float, ceil_db: float) -> tuple[np.ndarray, float, bool]:
    y = x * 10 ** (gain_db / 20.0)
    tp = true_peak_db(y, sr)
    limited = False
    if tp > ceil_db:
        red = tp - ceil_db
        y = y * 10 ** (-red / 20.0)
        gain_db -= red
        limited = True
    return y, gain_db, limited


def normalize_batch(x: np.ndarray, sr: int, target_lufs: float = BATCH_LUFS, ceil_db: float = TP_CEIL_DBTP) -> tuple[np.ndarray, dict]:
    """Этап А: пачка целиком к target_lufs, потолок истинного пика ceil_db."""
    l0 = integrated_lufs(x, sr)
    if not math.isfinite(l0):
        raise ValueError("пачка без речи (громкость не измеряется)")
    y, g, lim = _gain_with_ceiling(x, sr, target_lufs - l0, ceil_db)
    return y, {"lufs_before": round(l0, 2), "gain_db": round(g, 2), "peak_limited": lim,
               "lufs_after": round(integrated_lufs(y, sr), 2), "tp_after": round(true_peak_db(y, sr), 2)}


def normalize_short(x: np.ndarray, sr: int, target_db: float = CLIP_RMS_DBFS, ceil_db: float = TP_CEIL_DBTP) -> tuple[np.ndarray, dict]:
    """Этап Б (клип < 1 с): RMS активного окна к target_db, потолок истинного пика ceil_db."""
    r0 = active_rms_db(x, sr)
    if not math.isfinite(r0):
        return x, {"rms_before": None, "gain_db": 0.0, "peak_limited": False}
    y, g, lim = _gain_with_ceiling(x, sr, target_db - r0, ceil_db)
    return y, {"rms_before": round(r0, 2), "gain_db": round(g, 2), "peak_limited": lim,
               "rms_after": round(active_rms_db(y, sr), 2), "tp_after": round(true_peak_db(y, sr), 2)}


# ---------------------------------------------------------------- паузы и нарезка
@dataclass(frozen=True)
class Seg:
    start: int      # отсчёт начала речи (первый кадр выше порога)
    end: int        # отсчёт конца речи (последний кадр выше порога, не включая)


def speech_runs(x: np.ndarray, sr: int, thr_db: float = SILENCE_DBFS) -> list[Seg]:
    """Непрерывные участки кадров выше порога (без слияния)."""
    d = frame_db(x, sr)
    f = max(1, int(round(FRAME_S * sr)))
    act = d > thr_db
    runs, i, n = [], 0, act.size
    while i < n:
        if act[i]:
            j = i
            while j < n and act[j]:
                j += 1
            runs.append(Seg(i * f, j * f))
            i = j
        else:
            i += 1
    return runs


def split_by_largest_gaps(runs: list[Seg], count: int, sr: int, min_gap_s: float = 0.25) -> tuple[list[Seg], dict]:
    """Разбить речь на count фраз по count-1 самым длинным паузам между участками.
    Неоднозначность (самая длинная невзятая пауза не короче 80 % самой короткой взятой) -- флаг в отчёт."""
    if count < 1:
        raise ValueError("count >= 1")
    if len(runs) < count:
        raise ValueError(f"в записи {len(runs)} участков речи, ожидалось не меньше {count}")
    gaps = [(runs[i + 1].start - runs[i].end, i) for i in range(len(runs) - 1)]
    order = sorted(gaps, key=lambda g: (-g[0], g[1]))
    chosen = sorted(i for _, i in order[:count - 1])
    taken = [g for g in order[:count - 1]]
    rest = order[count - 1:]
    min_taken = min((g[0] for g in taken), default=None)
    max_rest = max((g[0] for g in rest), default=0)
    info = {"min_split_gap_s": round(min_taken / sr, 3) if min_taken is not None else None,
            "max_inner_gap_s": round(max_rest / sr, 3),
            "ambiguous": bool(min_taken is not None and max_rest >= 0.8 * min_taken),
            "too_short_gap": bool(min_taken is not None and min_taken < min_gap_s * sr)}
    segs, s0 = [], 0
    for i in chosen + [len(runs) - 1]:
        segs.append(Seg(runs[s0].start, runs[i].end))
        s0 = i + 1
    return segs, info


def after_first_long_gap(x: np.ndarray, sr: int, seg: Seg, thr_db: float = SILENCE_DBFS) -> Seg:
    """Носитель «Текущее значение: N.»: внутри фразы -- самая длинная пауза; число -- речь после неё."""
    runs = [Seg(r.start + seg.start, r.end + seg.start) for r in speech_runs(x[seg.start:seg.end], sr, thr_db)]
    if len(runs) < 2:
        raise ValueError("в фразе-носителе нет паузы между префиксом и числом")
    gi = max(range(len(runs) - 1), key=lambda i: (runs[i + 1].start - runs[i].end, -i))
    return Seg(runs[gi + 1].start, runs[-1].end)


def take_clip(x: np.ndarray, sr: int, seg: Seg, head_s: float = HEAD_S, tail_s: float = TAIL_S) -> np.ndarray:
    """Речь + буферы: берутся из записи (там тишина), за краями записи -- нули."""
    h, t = int(round(head_s * sr)), int(round(tail_s * sr))
    a, b = seg.start - h, seg.end + t
    pre = np.zeros(max(0, -a)); post = np.zeros(max(0, b - x.size))
    return np.concatenate([pre, x[max(0, a):min(x.size, b)], post])
