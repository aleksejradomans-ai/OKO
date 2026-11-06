r"""audio_probe.py — платформенная часть аудиоконтура: что есть на машине.

Диагностика того же класса, что `tv_probe.py` и `tv_snapshot.py`: ничем не
импортируется, в манифест не входит, тестов нет. Всё, что здесь делается,
зависит от Windows, звуковых устройств и установленных пакетов — это
проверяется на машине, а не в тестах. Измеримая арифметика вынесена в
`audio_meter.py`, она покрыта тестами отдельно.

Что проверяется и почему именно так:

1. `--devices` — список устройств вывода и ввода. Нужен первым: пока не
   известно, какое устройство по умолчанию, разговор про громкость и
   петлю беспредметен.

2. `--sessions` — сессии микшера с PID. Управление громкостью процесса
   идёт через `ISimpleAudioVolume`, и сессия существует ТОЛЬКО пока
   процесс что-то играет. Поэтому «установить громкость заранее» не
   работает: сессии ещё нет, вызов молча ничего не делает.

3. `--set-volume 0.5` — громкость ЭТОГО процесса, по его собственному PID.
   Отбор по подстроке «python» в имени процесса (как в типовых примерах)
   здесь недопустим: в системе одновременно живут OkoBot, run_system.py
   (pid был 5776) и HUD — все python.exe. Такой фильтр менял бы громкость
   всем сразу, включая постороннее.

4. `--loopback` — попытка захвата системного вывода через WASAPI
   loopback. Поддержка зависит от версии `sounddevice`/PortAudio, поэтому
   здесь это ПРОВЕРКА ВОЗМОЖНОСТИ, а не рабочий механизм: скрипт пробует
   и честно сообщает, получилось ли.

   Важно про смысл петли: она слышит ЛЮБОЙ звук на устройстве. Если в это
   время играет музыка или звук из браузера, петля покажет сигнал даже при
   полностью провалившемся синтезе. Поэтому петля сама по себе НЕ
   доказывает, что произнесено наше; доказательством может быть только
   совпадение по времени и длительности с конкретной репликой. Скрипт
   измеряет уровень и говорит, что слышит, не делая вывода за человека.

5. `--tone` — проигрывание короткого тона, чтобы замкнуть контур целиком:
   сгенерировали, измерили (`audio_meter`), отдали устройству, услышали
   в петле.
"""
from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio_meter


def _need(module: str):
    try:
        return __import__(module)
    except ImportError:
        print(f"нет пакета {module}: pip install {module}")
        return None


def devices() -> int:
    sd = _need("sounddevice")
    if sd is None:
        return 2
    try:
        default_out = sd.default.device[1]
        default_in = sd.default.device[0]
    except Exception:
        default_out = default_in = None
    print(f"{'#':>3} {'in':>3} {'out':>3} {'sr':>7}  host  name")
    for i, d in enumerate(sd.query_devices()):
        host = sd.query_hostapis(d["hostapi"])["name"]
        mark = ""
        if i == default_out:
            mark += " <- вывод по умолчанию"
        if i == default_in:
            mark += " <- ввод по умолчанию"
        print(f"{i:>3} {d['max_input_channels']:>3} {d['max_output_channels']:>3} "
              f"{int(d['default_samplerate']):>7}  {host[:12]:<12} {d['name'][:40]}{mark}")
    return 0


def sessions() -> int:
    pycaw = _need("pycaw")
    if pycaw is None:
        return 2
    from pycaw.pycaw import AudioUtilities

    me = os.getpid()
    rows = AudioUtilities.GetAllSessions()
    print(f"сессий микшера: {len(rows)}; этот процесс pid={me}")
    mine = 0
    for s in rows:
        p = s.Process
        if p is None:
            print(f"  pid=   -  (системный звук)")
            continue
        try:
            vol = s.SimpleAudioVolume.GetMasterVolume()
            muted = s.SimpleAudioVolume.GetMute()
        except Exception as e:
            vol, muted = "?", f"({type(e).__name__})"
        tag = "  <- ЭТОТ процесс" if p.pid == me else ""
        if p.pid == me:
            mine += 1
        print(f"  pid={p.pid:<7} vol={vol} mute={muted} {p.name()}{tag}")
    if mine == 0:
        print("У этого процесса сессии НЕТ: она появляется только когда процесс "
              "реально играет звук. Управлять громкостью до первого "
              "воспроизведения нечем — это свойство Windows, не ошибка.")
    return 0


def set_volume(level: float) -> int:
    """Громкость этого процесса по собственному PID."""
    pycaw = _need("pycaw")
    if pycaw is None:
        return 2
    if not 0.0 <= level <= 1.0:
        print(f"уровень должен быть 0.0..1.0, получено {level}")
        return 2
    from pycaw.pycaw import AudioUtilities

    me = os.getpid()
    for s in AudioUtilities.GetAllSessions():
        p = s.Process
        if p is not None and p.pid == me:
            s.SimpleAudioVolume.SetMasterVolume(level, None)
            got = s.SimpleAudioVolume.GetMasterVolume()
            print(f"pid={me}: просили {level:.2f}, прочитали {got:.2f}")
            return 0 if abs(got - level) < 0.01 else 1
    print(f"сессии для pid={me} нет — сначала нужно что-то проиграть "
          f"(--tone), иначе менять нечего")
    return 1


def make_tone(seconds: float = 1.0, freq: int = 440, amp: float = 0.3,
              sample_rate: int = 48000) -> bytes:
    n = int(sample_rate * seconds)
    out = bytearray()
    for i in range(n):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / sample_rate))
        out += struct.pack("<h", v)
    return bytes(out)


def tone(seconds: float, level: float | None) -> int:
    sd = _need("sounddevice")
    np = _need("numpy")
    if sd is None or np is None:
        return 2

    pcm = make_tone(seconds=seconds)
    stats = audio_meter.analyze(pcm, channels=1, sample_rate=48000)
    print("до отправки:", audio_meter.summary(stats))
    probs = audio_meter.problems(stats)
    print("претензии:", probs or "нет")
    if probs:
        print("не отправляю: буфер не прошёл проверку")
        return 1

    arr = np.frombuffer(pcm, dtype="<i2")
    sd.play(arr, 48000)
    if level is not None:
        time.sleep(0.15)   # дать Windows создать сессию микшера
        set_volume(level)
    sd.wait()
    print("проиграно")
    return 0


def loopback(seconds: float) -> int:
    sd = _need("sounddevice")
    np = _need("numpy")
    if sd is None or np is None:
        return 2

    try:
        settings = sd.WasapiSettings(loopback=True)
    except Exception as e:
        print(f"WASAPI loopback недоступен в этой версии sounddevice: "
              f"{type(e).__name__}: {e}")
        print("варианты: обновить sounddevice (pip install -U sounddevice) "
              "или взять библиотеку soundcard")
        return 2

    try:
        rec = sd.rec(int(48000 * seconds), samplerate=48000, channels=2,
                     dtype="int16", extra_settings=settings)
        sd.wait()
    except Exception as e:
        print(f"захват не удался: {type(e).__name__}: {e}")
        return 1

    pcm = np.asarray(rec, dtype="<i2").tobytes()
    stats = audio_meter.analyze(pcm, channels=2, sample_rate=48000)
    print("петля:", audio_meter.summary(stats))
    print("по каналам RMS:", [f"{r:.4f}" for r in stats.per_channel_rms])
    if stats.silent:
        print("ТИШИНА на устройстве вывода за время замера")
    else:
        print("звук на устройстве ЕСТЬ. Внимание: петля слышит любой звук "
              "системы — это не доказательство, что играло именно наше. "
              "Привязка возможна только по времени и длительности реплики.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", action="store_true")
    ap.add_argument("--sessions", action="store_true")
    ap.add_argument("--set-volume", type=float, default=None, metavar="0..1")
    ap.add_argument("--tone", type=float, default=None, metavar="СЕК")
    ap.add_argument("--loopback", type=float, default=None, metavar="СЕК")
    args = ap.parse_args()

    if args.devices:
        return devices()
    if args.sessions:
        return sessions()
    if args.tone is not None:
        return tone(args.tone, args.set_volume)
    if args.set_volume is not None:
        return set_volume(args.set_volume)
    if args.loopback is not None:
        return loopback(args.loopback)

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
