r"""audio_features.py — признаки звука, по которым можно судить о комнате.

Расширяет `audio_meter` (там только уровень) до набора величин, из которых
строится «матрица пространства»: полосы спектра, спектральный центр,
соотношение прямого и отражённого звука, и сходство микрофонного сигнала с
тем, что мы сами проиграли.

ЧТО ЭТИМИ ПРИЗНАКАМИ МОЖНО, А ЧТО НЕЛЬЗЯ — это важнее самих формул.

Можно (одним микрофоном):
  - громкость и её ход во времени;
  - распределение энергии по полосам (тембр, «шипит» или «гудит»);
  - спектральный центр: с расстоянием высокие частоты теряются сильнее,
    поэтому центр смещается вниз, когда источник дальше. Это НЕ линейка,
    а признак: «ближе/дальше» различимо, метры — нет;
  - соотношение прямого и отражённого (DRR): у близкого источника
    преобладает прямой звук, у далёкого — отражения от стен. Это
    физически и есть главный признак расстояния для одного микрофона;
  - сходство с известным сигналом (`coherence`): раз мы сами знаем, что
    играет на колонках, совпадающую часть можно опознать и отделить от
    остального. Это и есть «две точки данных» — микрофон и опорный
    сигнал.

Нельзя (на этом железе):
  - НАПРАВЛЕНИЕ и положение источника. Для них нужны два разнесённых
    микрофона и разность времени прихода. «Набор микрофонов (Realtek)»
    отдаёт два ОДИНАКОВЫХ канала (проверено 27.10: L и R совпадали до
    второго знака во всём прогоне), то есть драйвер сводит решётку в
    моно. Никакая обработка не восстановит разность хода из двух копий
    одного сигнала;
  - расстояние в метрах. Все признаки выше относительные и зависят от
    комнаты; без калибровки в этой комнате они дают порядок, не величину;
  - опознание говорящего. Это задача модели (эмбеддинг голоса), а не
    арифметики; здесь её нет и выдавать за неё спектральные признаки
    нельзя.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

import audio_meter

# Границы семи полос, по числу верхних светодиодов. Логарифмическая сетка
# от низа голоса до «воздуха»: слух и тембр работают в логарифмическом
# масштабе, и равномерные полосы отдали бы шесть седьмых шкалы верхам,
# где почти нет энергии.
BANDS_HZ: tuple[tuple[float, float], ...] = (
    (40, 100), (100, 250), (250, 600), (600, 1400),
    (1400, 3000), (3000, 6000), (6000, 12000),
)

# Полоса, по которой оцениваем «вокальную» энергию: основной тон речи и
# первые форманты.
VOICE_BAND_HZ = (120.0, 3400.0)

# Полосы для спектрального наклона. Низкая — тело голоса, высокая —
# согласные и «воздух». Отношение между ними НЕ зависит от общего
# усиления: автоматическая регулировка усиления микрофона Windows меняет
# громкость целиком, а соотношение полос оставляет. Именно поэтому наклон
# пригоден как признак расстояния там, где уровень и динамика уже
# испорчены АРУ.
TILT_LOW_HZ = (200.0, 800.0)
TILT_HIGH_HZ = (2000.0, 6000.0)


def _mono(pcm: bytes, channels: int) -> np.ndarray:
    """Суммирует каналы в моно float64 в диапазоне [-1, 1).

    Для спектра каналы складываются, а не берутся по отдельности: на этом
    микрофоне они всё равно одинаковы, а при двух разных источниках
    спектр суммы честнее отражает то, что слышно в комнате.
    """
    raw = audio_meter.decode_pcm16(pcm)
    if raw.size == 0:
        return np.empty(0, dtype=np.float64)
    chans = audio_meter.split_channels(raw, channels)
    stacked = np.vstack([c[: len(chans[0])].astype(np.float64) for c in chans])
    return stacked.mean(axis=0) / audio_meter.FULL_SCALE


def _spectrum(mono: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Односторонний спектр мощности и сетка частот.

    Окно Ханна обязательно: без него прямоугольное окно размазывает
    чистый тон по всему спектру (растекание), и полосовые уровни врут тем
    сильнее, чем короче блок.
    """
    n = mono.size
    if n < 8:
        return np.empty(0), np.empty(0)
    window = np.hanning(n)
    spec = np.fft.rfft(mono * window)
    power = (np.abs(spec) ** 2) / (np.sum(window ** 2) + 1e-30)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    return power, freqs


def band_energies(pcm: bytes, channels: int = 1, sample_rate: int = 48000,
                  bands: tuple[tuple[float, float], ...] = BANDS_HZ) -> list[float]:
    """Энергия в каждой полосе, в dBFS-подобной шкале (-inf..0).

    Возвращает именно энергию полосы, а не нормированную долю: доля
    остаётся одинаковой и в тишине, и при громком звуке, то есть по ней
    нельзя отличить «нет звука» от «есть звук с таким тембром».
    """
    mono = _mono(pcm, channels)
    power, freqs = _spectrum(mono, sample_rate)
    if power.size == 0:
        return [-math.inf] * len(bands)
    out: list[float] = []
    for low, high in bands:
        mask = (freqs >= low) & (freqs < high)
        if not mask.any():
            out.append(-math.inf)
            continue
        # Энергия полосы -> эквивалентная амплитуда -> dBFS.
        amp = math.sqrt(float(power[mask].sum()) * 2.0) / max(1, mono.size)
        out.append(audio_meter.dbfs(amp))
    return out


def spectral_centroid(pcm: bytes, channels: int = 1,
                      sample_rate: int = 48000) -> float:
    """Центр тяжести спектра в герцах; 0.0, если звука нет.

    Смещение центра вниз — признак удаления источника: высокие частоты
    гасятся воздухом и препятствиями сильнее низких. Признак, не линейка.
    """
    mono = _mono(pcm, channels)
    power, freqs = _spectrum(mono, sample_rate)
    total = float(power.sum()) if power.size else 0.0
    if total <= 0.0:
        return 0.0
    return float((power * freqs).sum() / total)


def spectral_tilt(pcm: bytes, channels: int = 1,
                  sample_rate: int = 48000) -> float:
    """Наклон спектра: верх минус низ, в дБ.

    Физика: высокие частоты теряются с расстоянием и при отражениях
    сильнее низких, поэтому у близкого голоса наклон выше, у далёкого
    ниже. В отличие от уровня и от динамики, наклон НЕ сдвигается при
    автоматической регулировке усиления — она умножает весь сигнал на
    общий множитель, а разность логарифмов от этого не меняется.

    Возвращает -inf, если в низкой полосе нет энергии (нечего сравнивать).
    """
    mono = _mono(pcm, channels)
    power, freqs = _spectrum(mono, sample_rate)
    if power.size == 0:
        return -math.inf

    def band(low: float, high: float) -> float:
        mask = (freqs >= low) & (freqs < high)
        return float(power[mask].sum()) if mask.any() else 0.0

    low = band(*TILT_LOW_HZ)
    high = band(*TILT_HIGH_HZ)
    if low <= 0.0:
        return -math.inf
    if high <= 0.0:
        return -120.0
    return 10.0 * math.log10(high / low)


def direct_to_reverb(pcm: bytes, channels: int = 1, sample_rate: int = 48000,
                     window_ms: float = 25.0) -> float:
    """Грубая оценка соотношения прямого и отражённого звука, в дБ.

    Считается по огибающей: берём самый громкий короткий участок (прямой
    звук) и медиану остальных (отражения и шум), возвращаем их отношение.
    Чем меньше величина, тем дальше источник — у далёкого голоса прямой
    звук почти не выделяется на фоне отражений.

    Это ОЦЕНКА, а не измерение импульсного отклика: настоящий DRR считают
    по отклику комнаты, которого у нас нет. Для различения «рядом» и «в
    другом углу» этого хватает, для метров — нет.
    """
    mono = _mono(pcm, channels)
    if mono.size == 0:
        return -math.inf
    win = max(8, int(sample_rate * window_ms / 1000.0))
    usable = mono.size - (mono.size % win)
    if usable < win * 3:
        return -math.inf
    frames = mono[:usable].reshape(-1, win)
    energy = np.sqrt((frames * frames).mean(axis=1))
    peak = float(energy.max())
    if peak <= 0.0:
        return -math.inf
    rest = np.sort(energy)[:-1]
    floor = float(np.median(rest)) if rest.size else 0.0
    if floor <= 0.0:
        return math.inf
    return 20.0 * math.log10(peak / floor)


def coherence(mic_pcm: bytes, ref_pcm: bytes, channels: int = 1,
              sample_rate: int = 48000, max_lag_ms: float = 60.0) -> float:
    """Сходство микрофонного сигнала с опорным, 0..1.

    Опорный сигнал — то, что мы сами отдали на колонки. Если микрофон
    слышит в основном его, сходство высокое, и значит в комнате играет
    наша музыка, а не говорит человек. Голос поверх музыки сходство
    снижает: часть энергии не объясняется опорным сигналом.

    Поиск по задержке обязателен: звук идёт от колонок до микрофона
    физически, плюс буферы драйвера — это десятки миллисекунд. Без
    сдвига одинаковые сигналы дали бы корреляцию около нуля, и вывод был
    бы обратным истине.
    """
    a = _mono(mic_pcm, channels)
    b = _mono(ref_pcm, channels)
    n = min(a.size, b.size)
    if n < 64:
        return 0.0
    a, b = a[:n], b[:n]
    a = a - a.mean()
    b = b - b.mean()
    na, nb = float(np.sqrt((a * a).sum())), float(np.sqrt((b * b).sum()))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    max_lag = min(n - 1, int(sample_rate * max_lag_ms / 1000.0))
    # Корреляция через FFT: прямой перебор задержек на 48 кГц и окне 60 мс
    # это миллионы умножений на блок, в реальном времени не проходит.
    size = 1 << (2 * n - 1).bit_length()
    fa = np.fft.rfft(a, size)
    fb = np.fft.rfft(b, size)
    corr = np.fft.irfft(fa * np.conj(fb), size)
    both = np.concatenate((corr[-max_lag:], corr[: max_lag + 1])) if max_lag else corr[:1]
    return float(np.clip(np.abs(both).max() / (na * nb), 0.0, 1.0))


@dataclass(frozen=True)
class Features:
    """Признаки одного блока. Пространственных величин здесь нет
    намеренно: одним микрофоном они не вычисляются (см. docstring модуля)."""

    rms_dbfs: float
    bands_dbfs: list[float]
    centroid_hz: float
    drr_db: float               # контраст огибающей; портится АРУ микрофона
    tilt_db: float              # верх минус низ; к АРУ устойчив
    voice_share: float          # доля энергии в голосовой полосе, 0..1


def extract(pcm: bytes, channels: int = 1, sample_rate: int = 48000) -> Features:
    stats = audio_meter.analyze(pcm, channels=channels, sample_rate=sample_rate)
    bands = band_energies(pcm, channels, sample_rate)

    mono = _mono(pcm, channels)
    power, freqs = _spectrum(mono, sample_rate)
    total = float(power.sum()) if power.size else 0.0
    if total > 0.0:
        low, high = VOICE_BAND_HZ
        mask = (freqs >= low) & (freqs < high)
        share = float(power[mask].sum() / total)
    else:
        share = 0.0

    return Features(
        rms_dbfs=stats.rms_dbfs,
        bands_dbfs=bands,
        centroid_hz=spectral_centroid(pcm, channels, sample_rate),
        drr_db=direct_to_reverb(pcm, channels, sample_rate),
        tilt_db=spectral_tilt(pcm, channels, sample_rate),
        voice_share=share,
    )


def summary(f: Features) -> str:
    bands = " ".join("--" if b == -math.inf else f"{b:.0f}" for b in f.bands_dbfs)
    drr = "--" if f.drr_db in (math.inf, -math.inf) else f"{f.drr_db:.1f}"
    tilt = "--" if f.tilt_db in (math.inf, -math.inf) else f"{f.tilt_db:.1f}"
    return (f"rms {f.rms_dbfs:6.1f}  centroid {f.centroid_hz:6.0f}Гц  "
            f"tilt {tilt:>6}дБ  drr {drr:>5}дБ  voice {f.voice_share:4.2f}  "
            f"bands [{bands}]")
