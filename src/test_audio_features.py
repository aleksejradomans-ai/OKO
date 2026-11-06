"""test_audio_features.py — признаки звука на синтетических сигналах.

Сигналы генерируются здесь, поэтому ответы известны заранее: тон в
полосе должен подсвечивать именно её, центр спектра у чистого тона равен
его частоте, одинаковые сигналы дают сходство 1, независимые — около
нуля.

Отдельно закреплено то, что НЕЛЬЗЯ: модуль не должен делать вид, что
считает направление или расстояние в метрах.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

SR = 48000


@pytest.fixture
def af():
    import audio_features as mod
    return mod


def tone(freq: float, seconds: float = 0.25, amp: float = 0.3,
         sr: int = SR, channels: int = 1) -> bytes:
    t = np.arange(int(sr * seconds)) / sr
    wave = np.sin(2 * np.pi * freq * t) * amp
    if channels > 1:
        wave = np.repeat(wave[:, None], channels, axis=1)
    import audio_meter
    return audio_meter.pcm16_from_float(wave)


def noise(seconds: float = 0.25, amp: float = 0.2, seed: int = 1,
          sr: int = SR, channels: int = 1) -> bytes:
    rng = np.random.default_rng(seed)
    wave = rng.standard_normal(int(sr * seconds)) * amp
    if channels > 1:
        wave = np.repeat(wave[:, None], channels, axis=1)
    import audio_meter
    return audio_meter.pcm16_from_float(wave)


def silence(seconds: float = 0.25, sr: int = SR, channels: int = 1) -> bytes:
    import audio_meter
    return audio_meter.pcm16_from_float(np.zeros((int(sr * seconds), channels)))


# --------------------------------------------------------------------------
# Полосы
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("freq", "index"), [
    (60, 0), (180, 1), (400, 2), (1000, 3), (2000, 4), (4500, 5), (9000, 6),
])
def test_tone_lights_its_own_band(af, freq, index):
    """Тон должен давать максимум именно в своей полосе — иначе сетка
    полос или окно спектра посчитаны неверно."""
    bands = af.band_energies(tone(freq), channels=1, sample_rate=SR)
    best = max(range(len(bands)), key=lambda i: bands[i])
    assert best == index, f"{freq} Гц попал в полосу {best}, ожидалась {index}"


def test_bands_count_matches_leds(af):
    """Семь полос — по числу верхних светодиодов."""
    assert len(af.BANDS_HZ) == 7
    assert len(af.band_energies(tone(1000), sample_rate=SR)) == 7


def test_bands_are_ascending_and_contiguous(af):
    for (lo1, hi1), (lo2, hi2) in zip(af.BANDS_HZ, af.BANDS_HZ[1:]):
        assert lo1 < hi1
        assert hi1 == lo2, "полосы должны идти стык в стык, без дыр"


def test_silence_gives_minus_inf_bands(af):
    bands = af.band_energies(silence(), sample_rate=SR)
    assert all(b == -math.inf for b in bands)


def test_band_energy_grows_with_level(af):
    quiet = af.band_energies(tone(1000, amp=0.05), sample_rate=SR)[3]
    loud = af.band_energies(tone(1000, amp=0.5), sample_rate=SR)[3]
    assert loud > quiet + 15, "энергия полосы обязана зависеть от громкости"


def test_empty_input(af):
    assert af.band_energies(b"", sample_rate=SR) == [-math.inf] * 7
    assert af.spectral_centroid(b"", sample_rate=SR) == 0.0


# --------------------------------------------------------------------------
# Спектральный центр
# --------------------------------------------------------------------------

@pytest.mark.parametrize("freq", [200, 1000, 4000])
def test_centroid_of_pure_tone_is_its_frequency(af, freq):
    got = af.spectral_centroid(tone(freq), sample_rate=SR)
    assert got == pytest.approx(freq, rel=0.1), got


def test_centroid_rises_with_brighter_signal(af):
    low = af.spectral_centroid(tone(200), sample_rate=SR)
    high = af.spectral_centroid(tone(5000), sample_rate=SR)
    assert high > low * 5


def test_centroid_of_silence_is_zero(af):
    assert af.spectral_centroid(silence(), sample_rate=SR) == 0.0


def test_window_prevents_leakage_into_other_bands(af):
    """Без оконной функции (прямоугольное окно) тон, не попавший ровно в
    узел сетки FFT, размазывается по ВСЕМУ спектру: замер показал +35..44
    дБ лишней энергии в далёких полосах. Центр тяжести при этом почти не
    страдает — растекание симметрично, — поэтому проверять надо полосы,
    а не центр.

    Частота взята заведомо некратной шагу сетки: на кратной растекания
    нет вообще, и тест ничего бы не проверял.
    """
    bands = af.band_energies(tone(1033.0, seconds=0.03), sample_rate=SR)
    assert bands[3] > -60, "тон должен быть в своей полосе"
    for i in (0, 1, 2, 4, 5, 6):
        assert bands[i] < bands[3] - 70, (
            f"полоса {i} = {bands[i]:.1f} дБ при тоне в полосе 3 "
            f"({bands[3]:.1f} дБ) — похоже на растекание спектра")


# --------------------------------------------------------------------------
# Прямой/отражённый
# --------------------------------------------------------------------------

def test_drr_high_for_single_burst(af):
    """Короткий всплеск на тишине — предельный случай «прямого звука»."""
    import audio_meter
    n = int(SR * 0.5)
    wave = np.zeros(n)
    wave[: int(SR * 0.02)] = 0.5
    drr = af.direct_to_reverb(audio_meter.pcm16_from_float(wave), sample_rate=SR)
    assert drr > 20, drr


def test_drr_low_for_steady_noise(af):
    """Ровный шум — предельный случай «всё отражения»: выделенного
    прямого звука нет."""
    drr = af.direct_to_reverb(noise(seconds=0.5), sample_rate=SR)
    assert drr < 6, drr


def test_drr_burst_above_noise(af):
    quiet = af.direct_to_reverb(noise(seconds=0.5), sample_rate=SR)
    import audio_meter
    rng = np.random.default_rng(2)
    wave = rng.standard_normal(int(SR * 0.5)) * 0.02
    wave[int(SR * 0.1): int(SR * 0.12)] += 0.6
    loud = af.direct_to_reverb(audio_meter.pcm16_from_float(wave), sample_rate=SR)
    assert loud > quiet + 10


def test_drr_too_short_input(af):
    assert af.direct_to_reverb(tone(1000, seconds=0.01), sample_rate=SR) == -math.inf
    assert af.direct_to_reverb(b"", sample_rate=SR) == -math.inf


# --------------------------------------------------------------------------
# Сходство с опорным сигналом
# --------------------------------------------------------------------------

def test_identical_signals_fully_coherent(af):
    ref = noise(seconds=0.2, seed=5)
    assert af.coherence(ref, ref, sample_rate=SR) == pytest.approx(1.0, abs=0.01)


def test_delayed_copy_is_still_coherent(af):
    """Звук идёт от колонок до микрофона и проходит буферы драйвера —
    это десятки миллисекунд. Без поиска по задержке одинаковые сигналы
    дали бы корреляцию около нуля, и вывод был бы обратным истине."""
    import audio_meter
    rng = np.random.default_rng(7)
    base = rng.standard_normal(int(SR * 0.3)) * 0.2
    delay = int(SR * 0.02)          # 20 мс
    delayed = np.concatenate((np.zeros(delay), base[:-delay]))
    a = audio_meter.pcm16_from_float(delayed)
    b = audio_meter.pcm16_from_float(base)
    assert af.coherence(a, b, sample_rate=SR) > 0.8


def test_independent_noise_is_incoherent(af):
    a = noise(seconds=0.2, seed=11)
    b = noise(seconds=0.2, seed=12)
    assert af.coherence(a, b, sample_rate=SR) < 0.2


def test_different_tones_incoherent(af):
    assert af.coherence(tone(440), tone(1500), sample_rate=SR) < 0.3


def test_voice_over_music_lowers_coherence(af):
    """Голос поверх музыки: часть энергии не объясняется опорным
    сигналом, и сходство должно упасть. Это и есть способ отделить
    человека от того, что мы сами играем."""
    import audio_meter
    rng = np.random.default_rng(3)
    music = rng.standard_normal(int(SR * 0.3)) * 0.2
    t = np.arange(music.size) / SR
    voice = np.sin(2 * np.pi * 220 * t) * 0.25
    pure = af.coherence(audio_meter.pcm16_from_float(music),
                        audio_meter.pcm16_from_float(music), sample_rate=SR)
    mixed = af.coherence(audio_meter.pcm16_from_float(music + voice),
                         audio_meter.pcm16_from_float(music), sample_rate=SR)
    assert mixed < pure
    assert mixed < 0.95


def test_coherence_with_silence_is_zero(af):
    assert af.coherence(silence(), noise(), sample_rate=SR) == 0.0
    assert af.coherence(noise(), silence(), sample_rate=SR) == 0.0


def test_coherence_short_input(af):
    assert af.coherence(b"", b"", sample_rate=SR) == 0.0


@pytest.mark.parametrize("seed", [21, 22, 23])
def test_coherence_bounded(af, seed):
    a, b = noise(seed=seed), noise(seed=seed + 100)
    for x, y in ((a, b), (b, a), (a, a)):
        assert 0.0 <= af.coherence(x, y, sample_rate=SR) <= 1.0


# --------------------------------------------------------------------------
# Сводка признаков
# --------------------------------------------------------------------------

def test_extract_voice_share(af):
    """Тон внутри голосовой полосы даёт высокую долю, вне — низкую."""
    inside = af.extract(tone(800), sample_rate=SR).voice_share
    outside = af.extract(tone(9000), sample_rate=SR).voice_share
    assert inside > 0.8
    assert outside < 0.2


def test_extract_on_silence_is_safe(af):
    f = af.extract(silence(), sample_rate=SR)
    assert f.rms_dbfs == -math.inf
    assert f.centroid_hz == 0.0
    assert f.voice_share == 0.0
    assert af.summary(f)


def test_extract_stereo(af):
    f = af.extract(tone(1000, channels=2), channels=2, sample_rate=SR)
    assert f.centroid_hz == pytest.approx(1000, rel=0.1)


def test_summary_is_one_line(af):
    line = af.summary(af.extract(tone(1000), sample_rate=SR))
    assert "\\n" not in line
    for token in ("rms", "centroid", "drr", "voice", "bands"):
        assert token in line


# --------------------------------------------------------------------------
# Чего модуль НЕ делает
# --------------------------------------------------------------------------

def test_module_claims_no_direction_or_metres(af):
    """Направление и расстояние в метрах одним микрофоном не считаются.
    Если в модуле появится что-то с таким именем, это обещание, которого
    он не может выполнить — и тест должен об этом сказать."""
    forbidden = ("direction", "azimuth", "angle", "distance_m", "position",
                 "triangulate", "doa")
    names = [n.lower() for n in dir(af) if not n.startswith("_")]
    for bad in forbidden:
        assert not any(bad in n for n in names), (
            f"имя с «{bad}» обещает пространственную оценку, "
            f"которая на одном моно-микрофоне невозможна")


# --------------------------------------------------------------------------
# Спектральный наклон (признак расстояния, устойчивый к АРУ микрофона)
# --------------------------------------------------------------------------

def test_tilt_positive_for_bright_signal(af):
    """Энергия в верхней полосе -> наклон положительный."""
    assert af.spectral_tilt(tone(3500), sample_rate=SR) > 20


def test_tilt_negative_for_dull_signal(af):
    assert af.spectral_tilt(tone(400), sample_rate=SR) < -20


def test_tilt_is_gain_invariant(af):
    """ГЛАВНОЕ СВОЙСТВО: автоматическая регулировка усиления микрофона
    умножает сигнал на общий множитель. Уровень и контраст огибающей от
    этого уезжают, а наклон — нет, потому что это РАЗНОСТЬ логарифмов.
    """
    quiet = af.spectral_tilt(tone(3000, amp=0.02), sample_rate=SR)
    loud = af.spectral_tilt(tone(3000, amp=0.6), sample_rate=SR)
    assert abs(loud - quiet) < 1.0, (quiet, loud)


def test_tilt_separates_near_and_far_models(af):
    """Модель близкого и далёкого голоса: далёкий теряет верх. Разные
    уровни взяты намеренно — признак не должен зависеть от громкости."""
    import audio_meter
    t = np.arange(int(SR * 0.3)) / SR
    body = np.sin(2 * np.pi * 300 * t)
    air = np.sin(2 * np.pi * 3500 * t)

    near = audio_meter.pcm16_from_float((body * 0.3 + air * 0.3))
    far = audio_meter.pcm16_from_float((body * 0.05 + air * 0.003))

    assert af.spectral_tilt(near, sample_rate=SR) > af.spectral_tilt(far, sample_rate=SR) + 10


def test_tilt_silence_and_empty(af):
    assert af.spectral_tilt(silence(), sample_rate=SR) == -math.inf
    assert af.spectral_tilt(b"", sample_rate=SR) == -math.inf


def test_extract_has_tilt(af):
    f = af.extract(tone(3000), sample_rate=SR)
    assert f.tilt_db > 0
    assert "tilt" in af.summary(f)
