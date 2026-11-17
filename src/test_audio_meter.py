"""test_audio_meter.py — измеритель PCM, без звуковых устройств.

Сигналы генерируются в тесте, поэтому ожидаемые значения известны
аналитически, а не списаны с прогона.

Отдельно проверяется то, из-за чего типовая схема «RMS > 0.95 = клиппинг»
не работает: у синуса полной шкалы RMS = 0.707, и порог не достигается
ничем, кроме меандра. Клиппинг обязан считаться по пикам.
"""
from __future__ import annotations

import math

import numpy as np
import pytest


@pytest.fixture
def am():
    import audio_meter as mod
    return mod


SR = 48000


def pcm(samples: np.ndarray) -> bytes:
    return np.asarray(samples, dtype="<i2").tobytes()


def sine(amp: float, freq: int = 440, seconds: float = 1.0,
         sr: int = SR, channels: int = 1) -> bytes:
    t = np.arange(int(sr * seconds)) / sr
    wave = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)
    if channels == 1:
        return wave.tobytes()
    frame = np.repeat(wave[:, None], channels, axis=1)
    return frame.astype("<i2").tobytes()


def square(amp: float, seconds: float = 1.0, sr: int = SR) -> bytes:
    n = int(sr * seconds)
    half = max(1, sr // 880)
    wave = np.where((np.arange(n) // half) % 2 == 0, amp, -amp) * 32767
    return wave.astype("<i2").tobytes()


# --------------------------------------------------------------------------
# dBFS
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("level", "expected"), [
    (1.0, 0.0), (0.5, -6.02), (0.1, -20.0), (0.001, -60.0),
])
def test_dbfs(am, level, expected):
    assert am.dbfs(level) == pytest.approx(expected, abs=0.02)


def test_dbfs_zero_is_minus_inf(am):
    """Тишина — состояние сигнала, не ошибка счёта."""
    assert am.dbfs(0.0) == -math.inf
    assert am.dbfs(-1.0) == -math.inf


# --------------------------------------------------------------------------
# Разбор буфера
# --------------------------------------------------------------------------

def test_decode_empty(am):
    assert len(am.decode_pcm16(b"")) == 0


def test_odd_tail_does_not_crash(am):
    """Буфер легко приходит обрезанным по границе пакета. Измеритель не
    должен быть причиной сбоя озвучки."""
    data = sine(0.5, seconds=0.01) + b"\x01"
    s = am.analyze(data, channels=1, sample_rate=SR)
    assert s.samples == len(sine(0.5, seconds=0.01)) // 2
    assert s.rms > 0


def test_analyze_empty_buffer(am):
    s = am.analyze(b"", channels=1, sample_rate=SR)
    assert s.samples == 0 and s.rms == 0.0 and s.duration_s == 0.0
    assert s.rms_dbfs == -math.inf
    assert am.problems(s) == ["пустой буфер"]


@pytest.mark.parametrize("sr", [0, -48000])
def test_bad_sample_rate(am, sr):
    with pytest.raises(ValueError):
        am.analyze(sine(0.5, seconds=0.01), sample_rate=sr)


@pytest.mark.parametrize("ch", [0, -2])
def test_bad_channels(am, ch):
    with pytest.raises(ValueError):
        am.analyze(sine(0.5, seconds=0.01), channels=ch)


# --------------------------------------------------------------------------
# RMS и пик: известные формы сигнала
# --------------------------------------------------------------------------

def test_sine_full_scale_rms_is_0_707(am):
    """КЛЮЧЕВОЙ ТЕСТ. Синус полной шкалы даёт RMS 0.707, а не 1.0 —
    поэтому порог «RMS > 0.95 = клиппинг» недостижим для речи и музыки:
    проверка выглядела бы рабочей и не срабатывала никогда."""
    s = am.analyze(sine(1.0), sample_rate=SR)
    assert s.rms == pytest.approx(0.707, abs=0.01)
    assert s.peak == pytest.approx(1.0, abs=0.001)
    assert s.rms < 0.95, "на этом и ломается RMS-порог клиппинга"


def test_square_full_scale_rms_near_one(am):
    """Единственная форма, дотягивающая RMS до ~1.0 — меандр. Речью он
    не бывает."""
    s = am.analyze(square(1.0), sample_rate=SR)
    assert s.rms == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize(("amp", "expected_rms"), [
    (1.0, 0.707), (0.5, 0.354), (0.25, 0.177), (0.1, 0.0707),
])
def test_rms_scales_with_amplitude(am, amp, expected_rms):
    s = am.analyze(sine(amp), sample_rate=SR)
    assert s.rms == pytest.approx(expected_rms, abs=0.005)


def test_duration_from_sample_rate(am):
    s = am.analyze(sine(0.5, seconds=0.5), sample_rate=SR)
    assert s.duration_s == pytest.approx(0.5, abs=0.001)
    s2 = am.analyze(sine(0.5, seconds=0.5, sr=16000), sample_rate=16000)
    assert s2.duration_s == pytest.approx(0.5, abs=0.001)


def test_squares_do_not_overflow_int16(am):
    """Квадрат int16 не влезает в int16. Счёт обязан идти во float,
    иначе громкий сигнал даёт отрицательную сумму и RMS-мусор."""
    s = am.analyze(pcm([32767] * 1000), sample_rate=SR)
    assert s.rms == pytest.approx(0.99997, abs=0.001)
    assert s.rms > 0


# --------------------------------------------------------------------------
# Тишина
# --------------------------------------------------------------------------

def test_digital_silence_is_silent(am):
    s = am.analyze(pcm([0] * SR), sample_rate=SR)
    assert s.silent is True
    assert "тишина" in " ".join(am.problems(s))


def test_very_quiet_below_threshold_is_silent(am):
    s = am.analyze(sine(0.0005), sample_rate=SR)
    assert s.rms_dbfs < am.SILENCE_DBFS
    assert s.silent is True


def test_quiet_but_audible_is_not_silent(am):
    """-40 dBFS — тихая, но реальная речь. Ложная тревога здесь означала
    бы, что система ругается на нормальный шёпот."""
    s = am.analyze(sine(0.014), sample_rate=SR)
    assert -45 < s.rms_dbfs < -30
    assert s.silent is False
    assert am.problems(s) == []


# --------------------------------------------------------------------------
# Клиппинг — по пикам, а не по RMS
# --------------------------------------------------------------------------

def test_clipping_counted_by_peaks(am):
    data = pcm([32767] * 50 + [0] * 950)
    s = am.analyze(data, sample_rate=SR)
    assert s.clipped == 50
    assert s.clip_ratio == pytest.approx(0.05, abs=0.001)
    assert s.clipping is True


def test_negative_rail_also_counts(am):
    """Ограничение бывает и снизу: -32768 — такой же упор в шкалу."""
    s = am.analyze(pcm([-32768] * 40 + [0] * 960), sample_rate=SR)
    assert s.clipped == 40


def test_single_spike_is_not_clipping(am):
    """Один упёршийся отсчёт на согласной — не хрип. Порог по доле."""
    s = am.analyze(pcm([32767] + [1000] * 99999), sample_rate=SR)
    assert s.clipped == 1
    assert s.clipping is False


def test_loud_sine_is_not_clipping(am):
    """Синус полной шкалы громкий, но не ограниченный: пик равен шкале
    лишь в вершинах, доля ничтожна."""
    s = am.analyze(sine(0.99), sample_rate=SR)
    assert s.clipping is False
    assert am.problems(s) == []


# --------------------------------------------------------------------------
# Каналы
# --------------------------------------------------------------------------

def test_stereo_both_channels_alive(am):
    s = am.analyze(sine(0.5, channels=2), channels=2, sample_rate=SR)
    assert s.channels == 2
    assert len(s.per_channel_rms) == 2
    assert s.per_channel_rms[0] == pytest.approx(s.per_channel_rms[1], abs=0.01)
    assert s.dead_channels == []


def test_stereo_one_dead_channel_is_detected(am):
    """Главное, что теряется при счёте RMS по смешанному массиву: на слух
    это «звук только слева», а в общей цифре — просто потише."""
    t = np.arange(SR) / SR
    left = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767).astype(np.int16)
    right = np.zeros(SR, dtype=np.int16)
    frame = np.stack([left, right], axis=1).astype("<i2").tobytes()
    s = am.analyze(frame, channels=2, sample_rate=SR)
    assert s.dead_channels == [1]
    assert s.silent is False
    assert "молчащие каналы" in " ".join(am.problems(s))


def test_full_silence_reports_silence_not_dead_channels(am):
    """Если молчит всё — это тишина, другой диагноз. Иначе в логе была бы
    пара взаимоисключающих жалоб сразу."""
    s = am.analyze(pcm([0] * (SR * 2)), channels=2, sample_rate=SR)
    assert s.silent is True
    assert s.dead_channels == []


def test_duration_is_per_channel(am):
    """Стерео-секунда — это секунда, а не две."""
    s = am.analyze(sine(0.5, seconds=1.0, channels=2), channels=2, sample_rate=SR)
    assert s.duration_s == pytest.approx(1.0, abs=0.001)


# --------------------------------------------------------------------------
# Претензии к реплике
# --------------------------------------------------------------------------

def test_good_utterance_has_no_problems(am):
    s = am.analyze(sine(0.3, seconds=0.8), sample_rate=SR)
    assert am.problems(s) == []


def test_too_short_is_reported(am):
    s = am.analyze(sine(0.3, seconds=0.05), sample_rate=SR)
    probs = am.problems(s)
    assert any("коротко" in p for p in probs)


def test_several_problems_at_once(am):
    """Список, а не bool: коротко И с клиппингом — две разные причины,
    и в логе нужны обе."""
    s = am.analyze(pcm([32767] * 300), sample_rate=SR)
    probs = am.problems(s)
    assert len(probs) >= 2
    assert any("коротко" in p for p in probs)
    assert any("клиппинг" in p for p in probs)


def test_min_duration_is_configurable(am):
    s = am.analyze(sine(0.3, seconds=0.2), sample_rate=SR)
    assert am.problems(s, min_duration_s=0.1) == []
    assert any("коротко" in p for p in am.problems(s, min_duration_s=0.5))


# --------------------------------------------------------------------------
# Обрезка тишины
# --------------------------------------------------------------------------

def test_trim_removes_leading_and_trailing_silence(am):
    body = sine(0.5, seconds=0.3)
    pad = pcm([0] * int(SR * 0.5))
    trimmed = am.trim_silence(pad + body + pad, sample_rate=SR)
    s = am.analyze(trimmed, sample_rate=SR)
    assert 0.25 < s.duration_s < 0.36, f"осталось {s.duration_s:.3f} с"


def test_trim_keeps_internal_pause(am):
    """Пауза между словами внутри реплики обрезаться не должна."""
    word = sine(0.5, seconds=0.2)
    gap = pcm([0] * int(SR * 0.2))
    trimmed = am.trim_silence(word + gap + word, sample_rate=SR)
    s = am.analyze(trimmed, sample_rate=SR)
    assert s.duration_s == pytest.approx(0.6, abs=0.05)


def test_trim_all_silence_gives_empty(am):
    assert am.trim_silence(pcm([0] * SR), sample_rate=SR) == b""


def test_trim_empty_input(am):
    assert am.trim_silence(b"", sample_rate=SR) == b""


def test_trim_stereo_keeps_channel_identity(am):
    """После обрезки каналы должны остаться своими: левый громкий,
    правый тихий. Если срез сдвинет кадры, громкость «переедет» в другой
    канал, и перепутанное стерео будет не слышно в общей цифре.

    Проверяется именно сохранение порядка, а не длина в байтах: при
    корректных входных данных и окне в целых кадрах выравнивание
    выполняется по построению, и утверждение про кратность 4 байтам
    нельзя сломать — то есть оно ничего не проверяет."""
    n = int(SR * 0.3)
    t = np.arange(n) / SR
    left = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767).astype(np.int16)
    right = (np.sin(2 * np.pi * 440 * t) * 0.05 * 32767).astype(np.int16)
    body = np.stack([left, right], axis=1).astype("<i2").tobytes()
    pad = pcm([0] * int(SR * 0.4) * 2)

    trimmed = am.trim_silence(pad + body + pad, channels=2, sample_rate=SR)
    st = am.analyze(trimmed, channels=2, sample_rate=SR)
    assert st.per_channel_rms[0] > st.per_channel_rms[1] * 5, (
        f"каналы перепутаны или смешаны: {st.per_channel_rms}")
    assert st.dead_channels == []


# --------------------------------------------------------------------------
# Лог
# --------------------------------------------------------------------------

def test_summary_has_numbers_and_no_text(am):
    s = am.analyze(sine(0.3, seconds=0.5), sample_rate=SR)
    line = am.summary(s)
    for token in ("n=", "ch=", "dur=", "rms=", "peak=", "clip="):
        assert token in line
    assert "\\n" not in line


# --------------------------------------------------------------------------
# float -> PCM16 (источники, отдающие float: WASAPI loopback через soundcard)
# --------------------------------------------------------------------------

def test_float_full_scale(am):
    data = am.pcm16_from_float([1.0, -1.0, 0.0])
    assert np.frombuffer(data, dtype="<i2").tolist() == [32767, -32767, 0]


def test_float_out_of_range_is_clipped_not_wrapped(am):
    """Без обрезки 1.5*32767 переполняет int16 и становится ОТРИЦАТЕЛЬНЫМ:
    громкий участок дал бы противофазный треск вместо ограничения."""
    data = am.pcm16_from_float([1.5, -2.0, 1.0001])
    vals = np.frombuffer(data, dtype="<i2").tolist()
    assert vals == [32767, -32767, 32767]
    assert all(v > 0 or v == -32767 for v in vals)


def test_float_half_scale(am):
    data = am.pcm16_from_float([0.5, -0.5])
    assert np.frombuffer(data, dtype="<i2").tolist() == [16384, -16384]


def test_float_empty(am):
    assert am.pcm16_from_float([]) == b""
    assert am.pcm16_from_float(np.empty((0, 2), dtype=np.float32)) == b""


def test_float_2d_keeps_interleaving(am):
    """(n, channels) должно разворачиваться кадр за кадром, иначе каналы
    склеятся блоками и split_channels вернёт мусор."""
    frames = np.array([[1.0, -1.0], [0.5, -0.5]], dtype=np.float32)
    vals = np.frombuffer(am.pcm16_from_float(frames), dtype="<i2").tolist()
    assert vals == [32767, -32767, 16384, -16384]


def test_float_roundtrip_preserves_rms(am):
    t = np.arange(SR) / SR
    wave = (np.sin(2 * np.pi * 440 * t) * 0.3).astype(np.float32)
    s = am.analyze(am.pcm16_from_float(wave), channels=1, sample_rate=SR)
    assert s.rms == pytest.approx(0.3 / math.sqrt(2), abs=0.005)
    assert s.clipping is False


def test_float_stereo_dead_channel_survives_conversion(am):
    n = 1000
    t = np.arange(n) / SR
    left = (np.sin(2 * np.pi * 440 * t) * 0.4).astype(np.float32)
    frames = np.stack([left, np.zeros(n, dtype=np.float32)], axis=1)
    s = am.analyze(am.pcm16_from_float(frames), channels=2, sample_rate=SR)
    assert s.dead_channels == [1]
