"""AMB_OFF_PROBE (03.11.2026): почему «подсветка выключи» отвечает «выключена (Off)», а лампы горят.

Живой случай 02:28: в режиме «один цвет» POST ambilight/power Off -> чтение Off, Оператор видит свет.
Гипотеза (не проверена): цвет «lounge» живёт отдельно от ambilight/power. Скрипт делает ОДИН шаг и печатает
прочитанное состояние; глазами смотрит Оператор (блок PowerShell спрашивает после каждого шага).

Шаги: state | off | on | video_off | lounge0 | restore
"""
import json
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent.parent / "src")]
import config  # noqa: E402,F401  -- .env
from tv_control import TVClient  # noqa: E402

PAUSE = 2.0


def state(tv) -> dict:
    out = {}
    for name, fn in (("power", tv.get_ambilight_power), ("config", tv.get_ambilight_config),
                     ("lounge", tv.get_lounge_color)):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = f"ERR {type(e).__name__}: {e}"
    try:
        out["mode"] = tv.get_ambilight_mode()
    except Exception as e:  # noqa: BLE001
        out["mode"] = f"ERR {type(e).__name__}: {e}"
    return out


def main(step: str) -> int:
    tv = TVClient.from_env()
    if step == "off":
        tv.set_ambilight_power(False)
    elif step == "on":
        tv.set_ambilight_power(True)
    elif step == "video_off":
        tv.set_ambilight_power(True)
        tv.set_ambilight_style("FOLLOW_VIDEO", "STANDARD")
        time.sleep(PAUSE)
        tv.set_ambilight_power(False)
    elif step == "lounge0":
        tv.set_ambilight_power(True)
        tv.set_ambilight_style("FOLLOW_COLOR", "MANUAL_HUE")
        tv.set_lounge_color(85, 255, 0)
    elif step == "restore":
        tv.set_ambilight_power(True)
        tv.set_ambilight_style("FOLLOW_COLOR", "MANUAL_HUE")
        tv.set_lounge_color(85, 255, 255)
    elif step != "state":
        print(f"unknown step {step}")
        return 2
    time.sleep(PAUSE)
    print(f"STEP={step}")
    print(json.dumps(state(tv), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "state"))
