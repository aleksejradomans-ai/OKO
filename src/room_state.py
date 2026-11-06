r"""room_state.py — что сейчас происходит в комнате: голос, музыка, тишина.

Слияние признаков из `audio_features` в одно решение. Это тот минимум, с
которого начинается голосовой отклик: прежде чем распознавать слова, надо
знать, что СЕЙЧАС говорит человек, а не играет телевизор, и примерно
откуда — рядом или из глубины комнаты.

ЧТО РЕШАЕТСЯ ЗДЕСЬ:
  - `silence` — в комнате ничего;
  - `playback` — звучит то, что мы сами отдали на колонки (опознаётся по
    сходству с опорным сигналом, а не по громкости);
  - `speech` — человек говорит;
  - `noise` — есть звук, но он не похож ни на нашу запись, ни на речь.

ЧТО НЕ РЕШАЕТСЯ: кто именно говорит. Для «это оператор, а не гость»
нужен голосовой отпечаток (модель), и подменять его долей энергии в
голосовой полосе нельзя — в неё попадёт и голос из телевизора.

РАССТОЯНИЕ — КЛАССАМИ, А НЕ МЕТРАМИ, И ПО НАКЛОНУ СПЕКТРА.
Сначала опорной величиной был DRR (контраст огибающей: у близкого голоса
прямой звук выделяется резким всплеском). На машине Оператора этот
признак ПЕРЕВЕРНУЛСЯ: замер «рядом» дал 4.9 дБ против 7.3 дБ «издалека».
Объяснение: микрофонная решётка Realtek работает с автоматической
регулировкой усиления, и громкую близкую речь она поджимает — огибающая
становится ровной, контраст падает. То есть величина мерила не
расстояние, а то, насколько сигнал сжат.

Поэтому класс расстояния считается по СПЕКТРАЛЬНОМУ НАКЛОНУ (верх минус
низ, в дБ): высокие частоты теряются с расстоянием сильнее низких, а
разность логарифмов не меняется при общем усилении — АРУ её не портит.
DRR остаётся в признаках как справочная величина, но решения на нём не
строятся.

Абсолютного смысла у наклона тоже нет: он зависит от комнаты и
микрофона. Поэтому класс определяется сравнением с ПРОФИЛЕМ, снятым в
этой самой комнате (`--calibrate`). Без профиля — `unknown`, честнее,
чем выдумывать сантиметры.

УСТОЙЧИВОСТЬ ВАЖНЕЕ ЧУВСТВИТЕЛЬНОСТИ. Решение по одному блоку в 60 мс
дёргается на каждом слоге и на каждой паузе. Поэтому состояние меняется
только после нескольких подряд согласных кадров (`StateTracker`): для
отклика системы лучше опоздать на треть секунды, чем мигать десять раз
в секунду.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SILENCE = "silence"
PLAYBACK = "playback"
SPEECH = "speech"
NOISE = "noise"

NEAR = "near"
MID = "mid"
FAR = "far"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Profile:
    """Пороги, снятые в конкретной комнате.

    `noise_floor_dbfs` — уровень тишины ЭТОЙ комнаты. На машине Оператора
    замерено -81 dBFS; подставлять учебные -60 нельзя, иначе тишина будет
    считаться звуком.
    """

    noise_floor_dbfs: float = -70.0
    speech_margin_db: float = 12.0      # насколько громче тишины, чтобы считать звуком
    voice_share_min: float = 0.45       # доля энергии в голосовой полосе
    coherence_min: float = 0.55         # выше — это наша же запись
    # Наклон спектра: основной признак расстояния (устойчив к АРУ).
    tilt_near_db: float = 0.0
    tilt_far_db: float = -10.0
    # DRR: оставлен справочно. На решения не влияет — на этом железе
    # признак переворачивался из-за АРУ микрофона.
    drr_near_db: float = 18.0
    drr_far_db: float = 7.0
    # Снята ли калибровка РЕАЛЬНО, а не взяты дефолты. Вывести это из
    # самих чисел нельзя: дефолтные пороги тоже «разведены», и попытка
    # угадать по ним дала бы класс расстояния в комнате, где никто ничего
    # не мерил. Поэтому флаги ставятся только процедурой калибровки.
    measured_near: bool = False
    measured_far: bool = False

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        out: dict[str, Any] = {}
        for key, value in known.items():
            out[key] = bool(value) if key.startswith("measured_") else float(value)
        return cls(**out)

    @classmethod
    def load(cls, path: Path) -> "Profile":
        if not path.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return cls()

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    # Минимальный разнос порогов. При меньшем классы прыгают от шума:
    # на машине Оператора первый профиль дал полосу mid шириной 2.8 дБ.
    MIN_SPREAD_DB = 4.0

    @property
    def calibrated(self) -> bool:
        """Класс расстояния выдаётся, только если ОБА замера сняты в этой
        комнате и пороги НАКЛОНА разведены хотя бы на MIN_SPREAD_DB.

        Проверка разноса, а не просто порядка: при зазоре меньше нескольких
        дБ середина от дали не отличается, и класс меняется от дыхания.
        """
        return (self.measured_near and self.measured_far
                and self.tilt_near_db - self.tilt_far_db >= self.MIN_SPREAD_DB)


@dataclass(frozen=True)
class RoomState:
    kind: str
    distance: str = UNKNOWN
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)


def distance_class(tilt_db: float, profile: Profile) -> str:
    """Наклон спектра -> near/mid/far. Без снятого профиля — unknown."""
    if not profile.calibrated:
        return UNKNOWN
    if tilt_db in (math.inf, -math.inf) or tilt_db != tilt_db:   # inf или NaN
        return UNKNOWN
    if tilt_db >= profile.tilt_near_db:
        return NEAR
    if tilt_db <= profile.tilt_far_db:
        return FAR
    return MID


def classify(features: Any, coherence: float | None = None,
             profile: Profile | None = None) -> RoomState:
    """Признаки -> состояние комнаты.

    `coherence` — сходство с опорным сигналом (что мы сами играем). None
    означает «опорного сигнала нет», и тогда `playback` не выносится
    вовсе: без опоры отличить нашу музыку от чужой нечем, и выдавать
    догадку за факт нельзя.
    """
    p = profile or Profile()
    rms = getattr(features, "rms_dbfs", -math.inf)
    reasons: list[str] = []

    if rms == -math.inf or rms < p.noise_floor_dbfs + p.speech_margin_db:
        return RoomState(SILENCE, UNKNOWN, 1.0,
                         [f"rms {rms:.1f} ниже порога "
                          f"{p.noise_floor_dbfs + p.speech_margin_db:.1f}"])

    dist = distance_class(getattr(features, "tilt_db", -math.inf), p)

    if coherence is not None and coherence >= p.coherence_min:
        # Сходство с опорным сигналом — единственный надёжный признак
        # «это мы сами»: громкость тут ничего не говорит.
        conf = min(1.0, (coherence - p.coherence_min) / max(1e-6, 1.0 - p.coherence_min))
        reasons.append(f"сходство с опорным {coherence:.2f}")
        return RoomState(PLAYBACK, dist, round(0.5 + 0.5 * conf, 3), reasons)

    share = getattr(features, "voice_share", 0.0)
    if share >= p.voice_share_min:
        conf = min(1.0, (share - p.voice_share_min) / max(1e-6, 1.0 - p.voice_share_min))
        reasons.append(f"голосовая полоса {share:.2f}")
        if coherence is not None:
            reasons.append(f"не похоже на опорный ({coherence:.2f})")
        return RoomState(SPEECH, dist, round(0.5 + 0.5 * conf, 3), reasons)

    reasons.append(f"голосовая полоса {share:.2f} ниже {p.voice_share_min:.2f}")
    return RoomState(NOISE, dist, 0.5, reasons)


class StateTracker:
    """Сглаживание решений: переход только после `hold` согласных кадров.

    Без этого состояние дёргается на каждой паузе между словами: на 60 мс
    блоках речь выглядит как чередование `speech` и `noise` с частотой
    слогов. Для отклика системы опоздание на треть секунды безвредно, а
    мигание — нет.
    """

    def __init__(self, hold: int = 5, initial: str = SILENCE) -> None:
        if hold < 1:
            raise ValueError(f"hold должен быть >= 1, получено {hold}")
        self.hold = int(hold)
        self.state = initial
        self._candidate = initial
        self._streak = 0

    def push(self, kind: str) -> str:
        if kind == self.state:
            self._candidate = kind
            self._streak = 0
            return self.state
        if kind == self._candidate:
            self._streak += 1
        else:
            self._candidate = kind
            self._streak = 1
        if self._streak >= self.hold:
            self.state = kind
            self._streak = 0
        return self.state


def describe(state: RoomState) -> str:
    """Строка для лога. Текста сказанного здесь нет и быть не должно:
    в журнал идёт состояние, а не содержание разговора."""
    dist = "" if state.distance == UNKNOWN else f" / {state.distance}"
    why = "; ".join(state.reasons)
    return f"{state.kind}{dist} ({state.confidence:.2f}) — {why}"


# --------------------------------------------------------------------------
# Калибровка и живой просмотр. Ввод-вывод, тестами не покрывается.
# --------------------------------------------------------------------------

def _profile_path() -> Path:
    import config
    return config.BASE_DIR / "room_profile.json"


def _capture(device: int | None, seconds: float, sample_rate: int = 48000,
             block_ms: int = 250) -> list[Any]:
    """Набор признаков за `seconds`. Блок крупнее, чем у моста: DRR
    нужна огибающая длиной хотя бы в несколько окон по 25 мс."""
    import numpy as np
    import sounddevice as sd

    import audio_features

    info = (sd.query_devices(device, "input") if device is not None
            else sd.query_devices(kind="input"))
    sr = int(info["default_samplerate"])
    channels = min(2, int(info["max_input_channels"]))
    frames = max(1, int(sr * block_ms / 1000))
    out: list[Any] = []

    def cb(indata, n, t, status):
        pcm = bytes(memoryview(indata).cast("B"))
        out.append(audio_features.extract(pcm, channels=channels, sample_rate=sr))

    with sd.InputStream(device=device, samplerate=sr, channels=channels,
                        dtype="int16", blocksize=frames, callback=cb):
        import time
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.05)
    return out


def _clean(values: list[float]) -> list[float]:
    return sorted(v for v in values if v not in (math.inf, -math.inf) and v == v)


def _median(values: list[float]) -> float:
    vals = _clean(values)
    if not vals:
        return math.nan
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def percentile(values: list[float], q: float) -> float:
    """Нижний процентиль без numpy. Нужен вместо медианы для замера
    тишины: если во время калибровки прозвучал голос, кашель или щелчок
    клавиши, медиана уезжает вверх на десятки децибел, и порог речи
    оказывается ВЫШЕ самой речи — система глохнет полностью.

    Замерено 27.10: медиана «тишины» дала -39.3 dBFS там, где реальная
    полка комнаты -80, и после этого все кадры, включая разговор на -31,
    попадали в `silence`.
    """
    vals = _clean(values)
    if not vals:
        return math.nan
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * max(0.0, min(100.0, q)) / 100.0
    low = int(math.floor(pos))
    high = min(low + 1, len(vals) - 1)
    frac = pos - low
    return vals[low] * (1.0 - frac) + vals[high] * frac


# Полка тише этого порога для жилой комнаты неправдоподобна: значит во
# время замера в комнате был звук. Величина выбрана по измерениям на
# машине Оператора (реальная тишина -80, разговор -42..-23).
SUSPICIOUS_FLOOR_DBFS = -55.0


def calibration_warning(kind: str, value: float) -> str | None:
    """Проверка правдоподобия замера. None — замер похож на правду.

    Без этой проверки одна неудачная калибровка молча делает систему
    глухой, и внешне это выглядит как «не работает распознавание», а не
    как «профиль снят с шумом».
    """
    if value != value:                      # NaN
        return "замер не получен"
    if kind == "silence" and value > SUSPICIOUS_FLOOR_DBFS:
        return (f"полка тишины {value:.1f} dBFS слишком высокая "
                f"(ожидается ниже {SUSPICIOUS_FLOOR_DBFS:.0f}): похоже, во время "
                f"замера в комнате был звук. Речь после такого профиля не "
                f"распознается вовсе")
    if kind in ("near", "far") and value in (math.inf, -math.inf):
        return f"наклон {value}: в полосах нет энергии, замер бесполезен"
    return None


def calibration_patch(kind: str, frames: list[Any],
                      profile: Profile) -> tuple[dict[str, Any], list[str], bool]:
    """Замер -> (изменения профиля, сообщения, можно ли сохранять).

    Вынесено из `main()` отдельной функцией сознательно: пока этот расчёт
    жил внутри ветки CLI, он был непокрываем тестами, и там завелась
    опечатка — в ветке `far` использовалась переменная, посчитанная
    только в ветке `near` (NameError на живом запуске 22:04). Тесты
    модуля при этом были зелёными, потому что `main()` ими не
    затрагивается.
    """
    msgs: list[str] = []
    if not frames:
        return {}, ["не получено ни одного блока"], False

    if kind == "silence":
        values = [f.rms_dbfs for f in frames]
        floor = percentile(values, 10.0)
        msgs.append(f"полка тишины: {floor:.1f} dBFS "
                    f"(медиана {_median(values):.1f}, кадров {len(values)})")
        patch = {"noise_floor_dbfs": round(floor, 1)}
        warn = calibration_warning("silence", floor)
        if warn:
            msgs.append(f"СОМНИТЕЛЬНО: {warn}")
            # Изменения возвращаются даже у сомнительного замера: решение
            # «сохранять или нет» принимает вызывающий код (--force), а не
            # эта функция. Иначе `--force` было бы нечего сохранять.
            return patch, msgs, False
        return patch, msgs, True

    if kind not in ("near", "far"):
        return {}, [f"неизвестный замер {kind!r}"], False

    tilt = _median([f.tilt_db for f in frames])
    drr = _median([f.drr_db for f in frames])
    msgs.append(f"наклон {kind}: {tilt:.1f} дБ "
                f"(DRR справочно {drr:.1f} дБ, кадров {len(frames)})")
    ok = True
    warn = calibration_warning(kind, tilt)
    if warn:
        msgs.append(f"СОМНИТЕЛЬНО: {warn}")
        ok = False

    if kind == "near":
        patch = {"tilt_near_db": round(tilt, 1), "drr_near_db": round(drr, 1),
                 "measured_near": True}
    else:
        patch = {"tilt_far_db": round(tilt, 1), "drr_far_db": round(drr, 1),
                 "measured_far": True}

    merged = Profile(**{**profile.to_dict(), **patch})
    spread = spread_warning(merged)
    if spread:
        # Разнос — не повод отказать: отдельный замер сам по себе
        # правдоподобен, негодной становится ПАРА. Поэтому это
        # предупреждение, а профиль всё равно не будет считаться снятым,
        # пока разнос мал (см. Profile.calibrated).
        msgs.append(f"ВНИМАНИЕ: {spread}")
    return patch, msgs, ok


def spread_warning(profile: Profile) -> str | None:
    """Проверка разноса порогов наклона после калибровки."""
    if not (profile.measured_near and profile.measured_far):
        return None
    spread = profile.tilt_near_db - profile.tilt_far_db
    if spread < 0:
        return (f"замеры перепутаны: «рядом» {profile.tilt_near_db:.1f} дБ НИЖЕ "
                f"«издалека» {profile.tilt_far_db:.1f} дБ. Переснимите, "
                f"говоря вплотную к микрофону")
    if spread < Profile.MIN_SPREAD_DB:
        return (f"разнос {spread:.1f} дБ меньше {Profile.MIN_SPREAD_DB:.0f}: "
                f"классы расстояния будут прыгать от шума")
    return None


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--calibrate", choices=["silence", "near", "far"], default=None,
                    help="снять часть профиля: тишина / голос рядом / голос издалека")
    ap.add_argument("--show", action="store_true", help="живой просмотр состояния")
    ap.add_argument("--force", action="store_true",
                    help="сохранить замер, даже если он выглядит неправдоподобно")
    args = ap.parse_args()

    path = _profile_path()
    profile = Profile.load(path)

    if args.calibrate:
        if args.calibrate == "silence":
            print(f"молчите {args.seconds:.0f} с — замеряю тишину комнаты")
        else:
            where = "РЯДОМ с микрофоном" if args.calibrate == "near" else "ИЗДАЛЕКА"
            print(f"говорите {where} {args.seconds:.0f} с")
        frames = _capture(args.device, args.seconds)
        if not frames:
            print("не получено ни одного блока")
            return 1
        patch, msgs, ok = calibration_patch(args.calibrate, frames, profile)
        for line in msgs:
            print(line)
        if not patch:
            print("нечего сохранять")
            return 1
        if not ok and not args.force:
            print("ОТКАЗ: замер не сохранён")
            if args.calibrate == "silence":
                print("повторите в тишине, либо --force если уверены")
            return 1
        if not ok:
            print("сохраняю по --force, хотя замер сомнительный")
        profile = Profile(**{**profile.to_dict(), **patch})
        profile.save(path)
        print(f"профиль: {path}")
        print(f"снят полностью: {'да' if profile.calibrated else 'нет'}")
        return 0

    if args.show:
        import time

        import audio_features  # noqa: F401  (нужен внутри _capture)

        print(f"профиль: {path} (снят: {'да' if profile.calibrated else 'нет'})")
        tracker = StateTracker()
        frames = _capture(args.device, args.seconds)
        above = 0
        loudest = -math.inf
        threshold = profile.noise_floor_dbfs + profile.speech_margin_db
        for f in frames:
            st = classify(f, coherence=None, profile=profile)
            stable = tracker.push(st.kind)
            if f.rms_dbfs > threshold:
                above += 1
            loudest = max(loudest, f.rms_dbfs)
            print(f"{stable:<9} | {describe(st)}")
            time.sleep(0)
        if frames and above == 0:
            # Иначе это выглядит как «распознавание не работает», хотя
            # дело в профиле: порог оказался выше всего, что звучало.
            print(f"\nНИ ОДИН кадр не превысил порог {threshold:.1f} dBFS "
                  f"(самый громкий {loudest:.1f}). Профиль снят с шумом: "
                  f"переснимите тишину — python room_state.py "
                  f"--calibrate silence --device {args.device}")
        return 0

    print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
    print(f"профиль: {path} (снят: {'да' if profile.calibrated else 'нет'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
