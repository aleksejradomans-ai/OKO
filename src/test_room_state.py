"""test_room_state.py — решение «что в комнате» без звуковых устройств.

Признаки подставляются готовыми, поэтому проверяется именно логика
слияния, а не DSP (он покрыт в test_audio_features).

Отдельно закреплены два обещания, которые модуль НЕ должен давать:
класс расстояния без снятого профиля и `playback` без опорного сигнала.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import pytest


@pytest.fixture
def rs():
    import room_state as mod
    return mod


@dataclass(frozen=True)
class F:
    """Подставные признаки — ровно те поля, которые читает classify."""

    rms_dbfs: float = -30.0
    tilt_db: float = -5.0
    drr_db: float = 12.0
    voice_share: float = 0.6
    centroid_hz: float = 900.0
    bands_dbfs: list[float] = field(default_factory=lambda: [-40.0] * 7)


CALIBRATED = dict(noise_floor_dbfs=-80.0, tilt_near_db=0.0, tilt_far_db=-12.0,
                  measured_near=True, measured_far=True)


# --------------------------------------------------------------------------
# Профиль
# --------------------------------------------------------------------------

def test_default_profile_is_not_calibrated(rs):
    """На дефолтах класс расстояния выдавать нельзя: пороги не из этой
    комнаты."""
    assert rs.Profile().calibrated is False


def test_calibrated_requires_both_measurements(rs):
    """Одного «рядом» мало: без «издалека» нет второй точки, и середину
    от дали не отличить. И наоборот."""
    assert rs.Profile(**CALIBRATED).calibrated is True
    only_near = {**CALIBRATED, "measured_far": False}
    only_far = {**CALIBRATED, "measured_near": False}
    assert rs.Profile(**only_near).calibrated is False
    assert rs.Profile(**only_far).calibrated is False


def test_calibrated_requires_separated_thresholds(rs):
    """Пороги должны быть разведены: near выше far. Иначе замеры
    перепутаны местами, и класс был бы обратным."""
    swapped = {**CALIBRATED, "tilt_near_db": -20.0, "tilt_far_db": -4.0}
    assert rs.Profile(**swapped).calibrated is False


def test_defaults_with_flags_are_not_calibrated(rs):
    """Дефолтные пороги тоже «разведены», поэтому вывести снятость из
    самих чисел нельзя — только по флагам процедуры калибровки."""
    d = rs.Profile()
    assert d.tilt_near_db > d.tilt_far_db
    assert d.calibrated is False


def test_profile_roundtrip(rs, tmp_path):
    p = rs.Profile(**CALIBRATED)
    path = p.save(tmp_path / "sub" / "room_profile.json")
    assert path.exists()
    back = rs.Profile.load(path)
    assert back == p


def test_profile_load_missing_gives_defaults(rs, tmp_path):
    assert rs.Profile.load(tmp_path / "absent.json") == rs.Profile()


def test_profile_load_broken_gives_defaults(rs, tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    assert rs.Profile.load(bad) == rs.Profile()


def test_profile_ignores_unknown_keys(rs, tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({**CALIBRATED, "whatever": 1}), encoding="utf-8")
    p = rs.Profile.load(path)
    assert p.tilt_near_db == 0.0
    assert p.tilt_far_db == -12.0
    assert not hasattr(p, "whatever")


# --------------------------------------------------------------------------
# Расстояние
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("tilt", "expected"), [
    (5.0, "near"), (0.0, "near"), (-6.0, "mid"),
    (-12.0, "far"), (-30.0, "far"),
])
def test_distance_classes_by_tilt(rs, tilt, expected):
    """Решение по НАКЛОНУ спектра: DRR на этом железе переворачивался
    из-за автоматической регулировки усиления микрофона."""
    assert rs.distance_class(tilt, rs.Profile(**CALIBRATED)) == expected


def test_distance_uses_tilt_not_drr(rs):
    """DRR остался в признаках справочно. Значения выбраны так, что по
    наклону выходит «рядом», а по DRR — «далеко»: если класс начнут
    считать по DRR, тест упадёт. Это и есть защита от возврата к
    признаку, который на этом железе переворачивался."""
    p = rs.Profile(**CALIBRATED)
    st = rs.classify(F(tilt_db=3.0, drr_db=-40.0), profile=p)
    assert st.distance == "near"

    # И наоборот: наклон «далеко», DRR «рядом».
    st2 = rs.classify(F(tilt_db=-25.0, drr_db=40.0), profile=p)
    assert st2.distance == "far"


def test_inverted_measurements_are_reported(rs):
    """Именно это случилось 27.10: «рядом» дал НИЖЕ, чем «издалека»."""
    bad = rs.Profile(**{**CALIBRATED, "tilt_near_db": -12.0, "tilt_far_db": -4.0})
    assert bad.calibrated is False
    warn = rs.spread_warning(bad)
    assert warn and "перепутаны" in warn


def test_narrow_spread_is_reported(rs):
    """Первый профиль дал полосу mid шириной 2.8 дБ — классы прыгали бы
    от шума."""
    narrow = rs.Profile(**{**CALIBRATED, "tilt_near_db": 0.0, "tilt_far_db": -2.8})
    assert narrow.calibrated is False
    warn = rs.spread_warning(narrow)
    assert warn and "разнос" in warn


def test_good_spread_passes(rs):
    ok = rs.Profile(**CALIBRATED)
    assert ok.calibrated is True
    assert rs.spread_warning(ok) is None


def test_spread_warning_needs_both_measurements(rs):
    half = rs.Profile(**{**CALIBRATED, "measured_far": False})
    assert rs.spread_warning(half) is None


def test_distance_unknown_without_profile(rs):
    """Без калибровки в ЭТОЙ комнате числа DRR не имеют абсолютного
    смысла. Выдавать класс — значит выдумывать."""
    assert rs.distance_class(25.0, rs.Profile()) == "unknown"


@pytest.mark.parametrize("tilt", [math.inf, -math.inf, float("nan")])
def test_distance_unknown_for_degenerate_tilt(rs, tilt):
    assert rs.distance_class(tilt, rs.Profile(**CALIBRATED)) == "unknown"


# --------------------------------------------------------------------------
# Классификация
# --------------------------------------------------------------------------

def test_silence_below_floor(rs):
    p = rs.Profile(**CALIBRATED)
    st = rs.classify(F(rms_dbfs=-75.0), coherence=None, profile=p)
    assert st.kind == "silence"
    assert st.distance == "unknown", "в тишине расстояние не оценивается"
    assert st.confidence == 1.0


def test_silence_for_minus_inf(rs):
    st = rs.classify(F(rms_dbfs=-math.inf), profile=rs.Profile(**CALIBRATED))
    assert st.kind == "silence"


def test_speech_when_voice_band_dominates(rs):
    p = rs.Profile(**CALIBRATED)
    st = rs.classify(F(rms_dbfs=-30.0, voice_share=0.8, tilt_db=4.0),
                     coherence=None, profile=p)
    assert st.kind == "speech"
    assert st.distance == "near"
    assert st.confidence > 0.5


def test_noise_when_voice_band_weak(rs):
    p = rs.Profile(**CALIBRATED)
    st = rs.classify(F(rms_dbfs=-25.0, voice_share=0.1), coherence=None, profile=p)
    assert st.kind == "noise"


def test_playback_recognised_by_coherence_not_loudness(rs):
    """Громкость о происхождении звука не говорит. Опознание «это мы
    сами» возможно только по сходству с опорным сигналом."""
    p = rs.Profile(**CALIBRATED)
    loud_music = F(rms_dbfs=-15.0, voice_share=0.9)
    assert rs.classify(loud_music, coherence=0.9, profile=p).kind == "playback"
    assert rs.classify(loud_music, coherence=0.1, profile=p).kind == "speech"


def test_no_playback_without_reference(rs):
    """Без опорного сигнала `playback` не выносится вовсе: отличить нашу
    музыку от чужой нечем, и догадку выдавать за факт нельзя."""
    p = rs.Profile(**CALIBRATED)
    for share in (0.05, 0.5, 0.95):
        st = rs.classify(F(rms_dbfs=-20.0, voice_share=share),
                         coherence=None, profile=p)
        assert st.kind != "playback"


def test_coherence_threshold_boundary(rs):
    p = rs.Profile(**CALIBRATED, coherence_min=0.55)
    assert rs.classify(F(), coherence=0.55, profile=p).kind == "playback"
    assert rs.classify(F(), coherence=0.549, profile=p).kind == "speech"


def test_voice_share_threshold_boundary(rs):
    p = rs.Profile(**CALIBRATED, voice_share_min=0.45)
    assert rs.classify(F(voice_share=0.45), coherence=None, profile=p).kind == "speech"
    assert rs.classify(F(voice_share=0.449), coherence=None, profile=p).kind == "noise"


def test_confidence_grows_with_evidence(rs):
    p = rs.Profile(**CALIBRATED)
    weak = rs.classify(F(voice_share=0.5), coherence=None, profile=p).confidence
    strong = rs.classify(F(voice_share=0.95), coherence=None, profile=p).confidence
    assert strong > weak
    assert 0.0 <= weak <= 1.0 and 0.0 <= strong <= 1.0


def test_reasons_are_filled(rs):
    p = rs.Profile(**CALIBRATED)
    for coh in (None, 0.9):
        st = rs.classify(F(), coherence=coh, profile=p)
        assert st.reasons, "решение без причины нельзя отладить по логу"


def test_classify_without_profile_uses_defaults(rs):
    st = rs.classify(F(rms_dbfs=-30.0, voice_share=0.8))
    assert st.kind in ("speech", "noise")
    assert st.distance == "unknown"


def test_describe_has_no_speech_text(rs):
    """В журнал идёт состояние, а не содержание разговора."""
    st = rs.classify(F(), coherence=None, profile=rs.Profile(**CALIBRATED))
    line = rs.describe(st)
    assert st.kind in line
    assert "\\n" not in line


# --------------------------------------------------------------------------
# Устойчивость решения
# --------------------------------------------------------------------------

def test_tracker_needs_consecutive_frames(rs):
    """Решение по одному блоку 60 мс дёргается на каждом слоге."""
    t = rs.StateTracker(hold=3, initial="silence")
    assert t.push("speech") == "silence"
    assert t.push("speech") == "silence"
    assert t.push("speech") == "speech"


def test_tracker_resets_on_interruption(rs):
    t = rs.StateTracker(hold=3, initial="silence")
    t.push("speech")
    t.push("speech")
    t.push("noise")          # серия прервана
    assert t.state == "silence"
    assert t.push("speech") == "silence"


def test_tracker_stays_on_same_state(rs):
    t = rs.StateTracker(hold=2, initial="speech")
    for _ in range(10):
        assert t.push("speech") == "speech"


def test_tracker_flapping_input_does_not_switch(rs):
    """Чередование кадров — это и есть речь с паузами. Состояние должно
    остаться прежним, а не мигать."""
    t = rs.StateTracker(hold=4, initial="silence")
    for kind in ["speech", "noise"] * 10:
        t.push(kind)
    assert t.state == "silence"


def test_tracker_hold_one_switches_immediately(rs):
    t = rs.StateTracker(hold=1, initial="silence")
    assert t.push("speech") == "speech"


@pytest.mark.parametrize("hold", [0, -3])
def test_tracker_bad_hold(rs, hold):
    with pytest.raises(ValueError):
        rs.StateTracker(hold=hold)


def test_tracker_sequence_realistic(rs):
    """Короткая реплика в тишине: переход в speech и возврат."""
    t = rs.StateTracker(hold=3, initial="silence")
    seq = ["silence"] * 3 + ["speech"] * 8 + ["silence"] * 8
    states = [t.push(k) for k in seq]
    assert "speech" in states
    assert states[-1] == "silence"


# --------------------------------------------------------------------------
# Правдоподобие калибровки
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("values", "q", "expected"), [
    ([1.0], 10.0, 1.0),
    ([0.0, 10.0], 0.0, 0.0),
    ([0.0, 10.0], 100.0, 10.0),
    ([0.0, 10.0], 50.0, 5.0),
    ([0.0, 1.0, 2.0, 3.0, 4.0], 50.0, 2.0),
])
def test_percentile(rs, values, q, expected):
    assert rs.percentile(values, q) == pytest.approx(expected)


def test_percentile_ignores_degenerate(rs):
    assert rs.percentile([math.inf, -math.inf, float("nan"), -70.0], 50.0) == -70.0
    assert math.isnan(rs.percentile([math.inf, float("nan")], 50.0))
    assert math.isnan(rs.percentile([], 10.0))


def test_percentile_survives_talking_during_silence(rs):
    """Главный смысл процентиля: если во время замера тишины прозвучал
    голос, медиана уезжает вверх на десятки децибел и порог речи
    оказывается ВЫШЕ самой речи. Замерено 27.10: медиана дала -39.3 там,
    где полка комнаты -80, после чего разговор на -31 попадал в silence.
    """
    quiet = [-80.0] * 14
    talk = [-35.0] * 18          # говорил большую часть замера
    values = quiet + talk
    assert rs._median(values) > -60.0, "медиана как раз и ломается"
    assert rs.percentile(values, 10.0) < -70.0, "процентиль держит полку"


@pytest.mark.parametrize("floor", [-39.3, -50.0, -20.0, 0.0])
def test_suspicious_silence_floor_is_reported(rs, floor):
    warn = rs.calibration_warning("silence", floor)
    assert warn and "тишины" in warn


@pytest.mark.parametrize("floor", [-56.0, -70.0, -81.0])
def test_plausible_silence_floor_passes(rs, floor):
    assert rs.calibration_warning("silence", floor) is None


@pytest.mark.parametrize("tilt", [math.inf, -math.inf])
def test_degenerate_tilt_measurement_is_reported(rs, tilt):
    assert rs.calibration_warning("near", tilt)
    assert rs.calibration_warning("far", tilt)


def test_good_tilt_passes(rs):
    assert rs.calibration_warning("near", 0.0) is None
    assert rs.calibration_warning("far", -12.0) is None


def test_nan_measurement_is_reported(rs):
    for kind in ("silence", "near", "far"):
        assert rs.calibration_warning(kind, float("nan"))


def test_bad_profile_makes_speech_undetectable(rs):
    """Что именно происходило 27.10: порог из плохого профиля выше, чем
    уровень реального разговора, и всё попадает в silence."""
    bad = rs.Profile(noise_floor_dbfs=-39.3)
    st = rs.classify(F(rms_dbfs=-31.0, voice_share=0.9), profile=bad)
    assert st.kind == "silence"

    good = rs.Profile(noise_floor_dbfs=-80.0)
    st2 = rs.classify(F(rms_dbfs=-31.0, voice_share=0.9), profile=good)
    assert st2.kind == "speech"


# --------------------------------------------------------------------------
# Расчёт калибровки (бывшая внутренность main(); там жил NameError)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CF:
    """Кадр для калибровки."""

    rms_dbfs: float = -70.0
    tilt_db: float = -5.0
    drr_db: float = 10.0


def test_calibration_far_branch_computes_tilt(rs):
    """РЕГРЕССИЯ 22:04: в ветке `far` использовалась переменная, которая
    считалась только в ветке `near` -> NameError на живом запуске. Тесты
    молчали, потому что расчёт жил внутри main(). Теперь он вынесен и
    проверяется обеими ветками."""
    prof = rs.Profile()
    frames = [CF(tilt_db=-14.0, drr_db=6.0)] * 5
    patch, msgs, ok = rs.calibration_patch("far", frames, prof)
    assert ok is True
    assert patch["tilt_far_db"] == -14.0
    assert patch["measured_far"] is True
    assert "measured_near" not in patch
    assert any("наклон far" in m for m in msgs)


def test_calibration_near_branch(rs):
    prof = rs.Profile()
    frames = [CF(tilt_db=2.0, drr_db=9.0)] * 5
    patch, msgs, ok = rs.calibration_patch("near", frames, prof)
    assert ok is True
    assert patch["tilt_near_db"] == 2.0
    assert patch["measured_near"] is True
    assert "measured_far" not in patch


def test_calibration_silence_branch(rs):
    frames = [CF(rms_dbfs=-80.0)] * 10 + [CF(rms_dbfs=-35.0)] * 12
    patch, msgs, ok = rs.calibration_patch("silence", frames, rs.Profile())
    assert ok is True, msgs
    assert patch["noise_floor_dbfs"] < -70.0, "процентиль держит полку"


def test_calibration_silence_suspicious_returns_patch_for_force(rs):
    """Сомнительный замер всё равно возвращает изменения: решение
    «сохранять или нет» принимает вызывающий код (--force). Иначе
    `--force` было бы нечего сохранять."""
    frames = [CF(rms_dbfs=-30.0)] * 10
    patch, msgs, ok = rs.calibration_patch("silence", frames, rs.Profile())
    assert ok is False
    assert patch and "noise_floor_dbfs" in patch
    assert any("СОМНИТЕЛЬНО" in m for m in msgs)


def test_calibration_reports_inverted_pair(rs):
    """Пара замеров перепутана: near ниже far. Отдельный замер при этом
    правдоподобен, поэтому это предупреждение, а не отказ."""
    prof = rs.Profile(tilt_far_db=-2.0, measured_far=True)
    frames = [CF(tilt_db=-18.0)] * 5
    patch, msgs, ok = rs.calibration_patch("near", frames, prof)
    assert ok is True
    assert any("перепутаны" in m for m in msgs)
    merged = rs.Profile(**{**prof.to_dict(), **patch})
    assert merged.calibrated is False


def test_calibration_empty_frames(rs):
    patch, msgs, ok = rs.calibration_patch("near", [], rs.Profile())
    assert ok is False and patch == {}
    assert msgs


def test_calibration_unknown_kind(rs):
    patch, msgs, ok = rs.calibration_patch("sideways", [CF()], rs.Profile())
    assert ok is False and patch == {}
