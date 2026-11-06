r"""tv_probe.py — снятие реального «белого списка» телевизора.

Диагностический инструмент, НЕ часть рабочего контура: ничем не
импортируется, в манифест не входит, тестов не имеет сознательно —
он только читает и печатает таблицу. Любое действие, меняющее
состояние телевизора, спрятано за явным флагом (см. ниже).

Зачем: описание API v6 в документе `tv_philips_api_v6` взято из внешних
источников и помечено PENDING. Проба 27.10 показала, что модель отвечает
иначе (`powerstate` 404, `applications` и `sources/current` 403). Чтобы
строить управление через бота, нужен список того, что РЕАЛЬНО работает на
этом устройстве, а не то, что написано в чужой документации.

Три контура опроса:
  1. HTTPS 1926 /6/<path> с digest-парой — основной API.
  2. HTTP 1925 /1/<path> без авторизации — то, что доступно без пайринга.
  3. HTTP 8008 DIAL — запуск приложений. Нужен потому, что `applications`
     на 1926 отдаёт 403: штатный путь к YouTube/Netflix на этой модели
     закрыт, а DIAL на 8008 открыт и работает без пары.

Режимы:
  python tv_probe.py                  — только GET, ничего не меняет
  python tv_probe.py --dump           — то же + полные тела всех живых
                                        путей в data\tv_probe\*.json
                                        (дерево настроек и список каналов
                                        в консоль не влезают)
  python tv_probe.py --keys-safe      — нажимает БЕЗОПАСНЫЕ кнопки с пау-
                                        зой: Info, стрелки, Back. Экран
                                        мигает плашками, состояние не
                                        меняется.
  python tv_probe.py --keys-all       — проходит ВЕСЬ список кнопок по
                                        группам, с паузой и пометкой, что
                                        каждая группа делает. Меняет то,
                                        что показано на экране. `Standby`
                                        исключён, добавляется отдельным
                                        флагом --include-standby.
  python tv_probe.py --key Online     — одна кнопка.
  python tv_probe.py --screen on      — POST screenstate. Кандидат на роль
                                        «включить телевизор»: `powerstate`
                                        на этой модели 404, а `screenstate`
                                        отвечает 200 и показывает Off/On.
  python tv_probe.py --amb-style OFF  — POST ambilight/currentconfiguration.
                                        Формат записи в документации не
                                        подтверждён, поэтому проверяется
                                        здесь, а не в рабочем модуле.
                                        FOLLOW_AUDIO:VU_METER — стиль с
                                        алгоритмом через двоеточие.
  python tv_probe.py --amb-lounge 110,38,255
                                      — POST ambilight/lounge (hue,sat,bri).
  python tv_probe.py --amb-mode manual
                                      — POST ambilight/mode. Для поштучного
                                        управления светодиодами режим должен
                                        быть не internal.
  python tv_probe.py --amb-pixel top:3:0,0,255
                                      — POST ambilight/cached: ОДИН светодиод
                                        по стороне и номеру. Проверка, что
                                        поштучная адресация вообще работает.
  python tv_probe.py --amb-modes      — перебирает возможные значения
                                        ambilight/mode и показывает, какое
                                        РЕАЛЬНО закрепилось: POST отдаёт 200
                                        на любое, а GET показывает своё.
  python tv_probe.py --amb-sweep      — бегущая точка по всем светодиодам:
                                        видно физическое расположение и
                                        порядок нумерации.
  python tv_probe.py --launch YouTube — POST DIAL на 8008. На «тв амби»
                                        бесполезно: DIAL там не отвечает
                                        (всё 404, включая device-desc.xml),
                                        порт открыт чем-то другим.

Вывод — таблица `путь | код | начало ответа`. Коды читать так: 200 путь
живой; 404 на модели отсутствует; 403 закрыт; 401 пара отвергнута.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any

import requests
import urllib3
from requests.auth import HTTPDigestAuth

import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CREDS = config.BASE_DIR / "tv_creds.json"

# GET-пути API v6. Состав — объединение таблиц из документа и путей,
# встречающихся в рабочих клиентах. Часть заведомо даст 404: это и есть
# результат, отрицательный ответ здесь такой же факт, как положительный.
V6_GET = [
    "system",
    "powerstate",
    "screenstate",
    "audio/volume",
    "activities/current",
    "activities/tv",
    "activities/browser",
    "applications",
    "applications/current",
    "sources",
    "sources/current",
    "channeldb/tv",
    "channeldb/tv/channelLists/all",
    "channeldb/tv/favoriteLists/1",
    "ambilight/power",
    "ambilight/mode",
    "ambilight/topology",
    "ambilight/measured",
    "ambilight/processed",
    "ambilight/cached",
    "ambilight/currentconfiguration",
    "ambilight/supportedstyles",
    "ambilight/lounge",
    "menuitems/settings/structure",
    "recordings/list",
    "context",
    "input/textentry",
    "network/devices",
    "userinterface/edgeplayer",
    "HomeMenu",
]

V1_GET = ["system", "audio/volume", "sources", "channels", "channels/current"]

# Кнопки, которые только показывают что-то на экране или двигают фокус.
# Standby, Source, WatchTV, Online, Record, цифры и PlayPause сюда НЕ
# входят: они переключают состояние, и «проба» ими была бы не пробой, а
# вмешательством в то, что человек в этот момент смотрит.
SAFE_KEYS = ["Info", "CursorUp", "CursorDown", "CursorLeft", "CursorRight", "Back"]

# Полный список кнопок по группам. Группировка не косметическая: она
# определяет порядок прохода и то, что человек увидит на экране. Внутри
# группы кнопки однородны по последствиям, поэтому прогон можно прервать
# на любой границе. `Standby` не входит ни в одну группу — он выключает
# телевизор, то есть обрывает сам прогон.
KEY_GROUPS: list[tuple[str, list[str]]] = [
    ("навигация (фокус, без выбора)", [
        "CursorUp", "CursorDown", "CursorLeft", "CursorRight", "Back", "Info",
        "Options", "Adjust",
    ]),
    ("громкость (вернётся на исходную)", ["VolumeUp", "VolumeDown", "Mute"]),
    ("цветные кнопки (контекстные)", [
        "RedColour", "GreenColour", "YellowColour", "BlueColour",
    ]),
    ("экраны и порталы (меняют картинку)", [
        "Home", "Find", "WatchTV", "Online", "Source", "Viewmode",
        "Teletext", "Subtitle",
    ]),
    ("каналы и цифры (переключают канал)", [
        "ChannelStepUp", "ChannelStepDown", "Digit0", "Digit1", "Digit2",
        "Digit3", "Digit4", "Digit5", "Digit6", "Digit7", "Digit8", "Digit9",
        "Dot",
    ]),
    ("воспроизведение", [
        "PlayPause", "Pause", "Stop", "FastForward", "Rewind", "Next",
        "Previous", "Record",
    ]),
    ("ambilight и подтверждение", ["AmbilightOnOff", "Confirm"]),
]

# Имена приложений в DIAL. Регистр важен, это идентификаторы протокола.
DIAL_APPS = ["YouTube", "Netflix", "Spotify", "AmazonInstantVideo", "Prime Video"]


def _creds() -> dict[str, Any]:
    if not CREDS.exists():
        sys.exit(f"FATAL: нет {CREDS} — сначала пайринг: python src\\tv_pair.py")
    return json.loads(CREDS.read_text(encoding="utf-8"))


def _short(text: str, n: int = 78) -> str:
    one = re.sub(r"\s+", " ", text or "").strip()
    return one[:n]


def probe_v6(host: str, port: int, auth: Any, session: requests.Session) -> list[tuple]:
    rows = []
    for p in V6_GET:
        url = f"https://{host}:{port}/6/{p}"
        try:
            r = session.get(url, auth=auth, timeout=5, verify=False)
            rows.append((f"1926 /6/{p}", r.status_code, _short(r.text)))
        except Exception as e:
            rows.append((f"1926 /6/{p}", "ERR", type(e).__name__))
    return rows


def probe_v1(host: str, session: requests.Session) -> list[tuple]:
    rows = []
    for p in V1_GET:
        url = f"http://{host}:1925/1/{p}"
        try:
            r = session.get(url, timeout=5)
            rows.append((f"1925 /1/{p}", r.status_code, _short(r.text)))
        except Exception as e:
            rows.append((f"1925 /1/{p}", "ERR", type(e).__name__))
    return rows


def probe_dial(host: str, session: requests.Session) -> list[tuple]:
    """DIAL на 8008. Это ЧУЖОЙ протокол, не JointSpace: отдельный порт,
    без пары, XML вместо JSON. Нужен ровно потому, что `applications`
    на 1926 закрыт — иначе запускать YouTube/Netflix нечем."""
    rows = []
    try:
        r = session.get(f"http://{host}:8008/ssdp/device-desc.xml", timeout=5)
        rows.append(("8008 device-desc.xml", r.status_code, _short(r.text)))
    except Exception as e:
        rows.append(("8008 device-desc.xml", "ERR", type(e).__name__))

    for app in DIAL_APPS:
        url = f"http://{host}:8008/apps/{app}"
        try:
            r = session.get(url, timeout=5)
            # 200 = приложение установлено (в XML будет state running/stopped),
            # 404 = такого приложения на телевизоре нет.
            state = ""
            m = re.search(r"<state>([^<]+)</state>", r.text or "")
            if m:
                state = f"state={m.group(1)} "
            rows.append((f"8008 /apps/{app}", r.status_code, state + _short(r.text, 48)))
        except Exception as e:
            rows.append((f"8008 /apps/{app}", "ERR", type(e).__name__))
    return rows


def dump_live(host: str, port: int, auth: Any, session: requests.Session) -> list[tuple]:
    """Сохраняет полные тела всех путей, ответивших 200. Дерево настроек и
    список каналов в консоли не читаемы, а для построения команд бота
    нужны именно они целиком."""
    out_dir = config.BASE_DIR / "tv_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in V6_GET:
        url = f"https://{host}:{port}/6/{p}"
        try:
            r = session.get(url, auth=auth, timeout=5, verify=False)
        except Exception as e:
            rows.append((p, "ERR", type(e).__name__))
            continue
        if r.status_code != 200:
            continue
        name = p.replace("/", "_") + ".json"
        try:
            body = json.dumps(r.json(), ensure_ascii=False, indent=2)
        except ValueError:
            body = r.text
        (out_dir / name).write_text(body, encoding="utf-8")
        rows.append((p, 200, f"-> {name} ({len(body)} б)"))
    return rows


def probe_keys_all(host: str, port: int, auth: Any, session: requests.Session,
                   include_standby: bool, delay: float = 1.5) -> list[tuple]:
    """Проход по всем группам. Печатает заголовок группы перед нажатиями,
    чтобы по логу было видно, в какой момент что появилось на экране."""
    rows: list[tuple] = []
    groups = list(KEY_GROUPS)
    if include_standby:
        groups.append(("ВЫКЛЮЧЕНИЕ", ["Standby"]))
    for title, keys in groups:
        rows.append((f"-- {title}", "", ""))
        for k in keys:
            url = f"https://{host}:{port}/6/input/key"
            try:
                r = session.post(url, json={"key": k}, auth=auth, timeout=5, verify=False)
                rows.append((f"key {k}", r.status_code, _short(r.text, 40)))
            except Exception as e:
                rows.append((f"key {k}", "ERR", type(e).__name__))
            time.sleep(delay)
        # Возврат фокуса в предсказуемое место между группами: иначе
        # следующая группа нажимается поверх случайно открытого меню.
        try:
            session.post(f"https://{host}:{port}/6/input/key", json={"key": "Back"},
                         auth=auth, timeout=5, verify=False)
        except Exception:
            pass
        time.sleep(delay)
    return rows


def set_screen(host: str, port: int, auth: Any, session: requests.Session,
               state: str) -> list[tuple]:
    url = f"https://{host}:{port}/6/screenstate"
    want = "On" if state.lower() == "on" else "Off"
    try:
        r = session.post(url, json={"screenstate": want}, auth=auth, timeout=5, verify=False)
        after = session.get(url, auth=auth, timeout=5, verify=False)
        return [
            (f"POST screenstate {want}", r.status_code, _short(r.text, 40) or "(empty)"),
            ("GET screenstate", after.status_code, _short(after.text, 40)),
        ]
    except Exception as e:
        return [(f"screenstate {want}", "ERR", type(e).__name__)]


def set_amb_style(host: str, port: int, auth: Any, session: requests.Session,
                  spec: str) -> list[tuple]:
    """Запись стиля подсветки. `spec` — `STYLE` или `STYLE:ALGORITHM`.

    Живой `currentconfiguration` вернул `{"styleName":"FOLLOW_VIDEO",
    "isExpert":false,"menuSetting":"STANDARD"}`, поэтому запись строится
    по той же форме: алгоритм кладётся в `menuSetting`. Для FOLLOW_AUDIO
    допустимые значения — из `supportedstyles.algorithms` (VU_METER,
    SPECTRUM_ANALYZER, PARTY, STROBE, KNIGHT_RIDER_*, RANDOM_PIXEL_FLASH,
    ENERGY_ADAPTIVE_*). После записи сразу читаем состояние: 200 на POST
    ничего не доказывает — ровно это уже случилось с `screenstate`.
    """
    style, _, algo = spec.partition(":")
    payload: dict[str, Any] = {"styleName": style.strip().upper(), "isExpert": False}
    if algo:
        payload["menuSetting"] = algo.strip().upper()
    url = f"https://{host}:{port}/6/ambilight/currentconfiguration"
    rows = []
    try:
        r = session.post(url, json=payload, auth=auth, timeout=5, verify=False)
        rows.append((f"POST {payload}", r.status_code, _short(r.text, 40) or "(empty)"))
        time.sleep(0.8)
        a = session.get(url, auth=auth, timeout=5, verify=False)
        rows.append(("GET currentconfiguration", a.status_code, _short(a.text, 60)))
        p2 = session.get(f"https://{host}:{port}/6/ambilight/power",
                         auth=auth, timeout=5, verify=False)
        rows.append(("GET ambilight/power", p2.status_code, _short(p2.text, 30)))
    except Exception as e:
        rows.append(("amb-style", "ERR", type(e).__name__))
    return rows


def set_amb_lounge(host: str, port: int, auth: Any, session: requests.Session,
                   spec: str) -> list[tuple]:
    """`hue,saturation,brightness`. Живой ответ: hue 110, sat 38, bri 255."""
    try:
        hue, sat, bri = (int(x) for x in spec.split(","))
    except ValueError:
        return [("amb-lounge", "ERR", "нужно три числа: hue,sat,bri")]
    url = f"https://{host}:{port}/6/ambilight/lounge"
    payload = {"color": {"hue": hue, "saturation": sat, "brightness": bri}}
    rows = []
    try:
        r = session.post(url, json=payload, auth=auth, timeout=5, verify=False)
        rows.append((f"POST lounge {hue},{sat},{bri}", r.status_code, _short(r.text, 40) or "(empty)"))
        time.sleep(0.8)
        a = session.get(url, auth=auth, timeout=5, verify=False)
        rows.append(("GET lounge", a.status_code, _short(a.text, 60)))
    except Exception as e:
        rows.append(("amb-lounge", "ERR", type(e).__name__))
    return rows


def amb_mode(host: str, port: int, auth: Any, session: requests.Session,
             mode: str) -> list[tuple]:
    """POST ambilight/mode. Живой GET вернул `{"current":"internal"}` —
    в этом режиме телевизор считает цвета сам, и поштучная запись, скорее
    всего, будет затираться. Для управления нужен `manual` (или `expert`,
    если прошивка его знает)."""
    url = f"https://{host}:{port}/6/ambilight/mode"
    rows = []
    try:
        r = session.post(url, json={"current": mode}, auth=auth, timeout=5, verify=False)
        rows.append((f"POST mode {mode}", r.status_code, _short(r.text, 40) or "(empty)"))
        time.sleep(0.6)
        a = session.get(url, auth=auth, timeout=5, verify=False)
        rows.append(("GET mode", a.status_code, _short(a.text, 40)))
    except Exception as e:
        rows.append(("amb-mode", "ERR", type(e).__name__))
    return rows


def _topology(host: str, port: int, auth: Any, session: requests.Session) -> dict:
    r = session.get(f"https://{host}:{port}/6/ambilight/topology",
                    auth=auth, timeout=5, verify=False)
    return r.json()


def amb_pixel(host: str, port: int, auth: Any, session: requests.Session,
              spec: str) -> list[tuple]:
    """POST ambilight/cached для ОДНОГО светодиода: `сторона:номер:r,g,b`.

    Форма тела — вложенные словари `{"layer1": {"<сторона>": {"<номер>":
    {"r":..,"g":..,"b":..}}}}`: именно так устроен ответ `measured`, и
    запись по той же схеме — первое, что стоит проверить. Если 200 придёт,
    а цвет не изменится, значит режим подсветки перебивает запись (см.
    `--amb-mode manual`) — это тот же случай, что `OFF` в стиле и
    `screenstate`: код ответа ничего не доказывает.
    """
    try:
        side, index, rgb = spec.split(":")
        r_, g_, b_ = (int(x) for x in rgb.split(","))
    except ValueError:
        return [("amb-pixel", "ERR", "формат: сторона:номер:r,g,b")]

    url = f"https://{host}:{port}/6/ambilight/cached"
    payload = {"layer1": {side: {str(index): {"r": r_, "g": g_, "b": b_}}}}
    rows = []
    try:
        top = _topology(host, port, auth, session)
        rows.append(("topology", 200, _short(json.dumps(top), 60)))
        if side in top and int(index) >= int(top[side]):
            rows.append(("WARN", "", f"у стороны {side} только {top[side]} "
                                     f"светодиодов, номер {index} вне диапазона"))
        r = session.post(url, json=payload, auth=auth, timeout=5, verify=False)
        rows.append((f"POST {side}:{index}", r.status_code, _short(r.text, 40) or "(empty)"))
        time.sleep(0.6)
        a = session.get(url, auth=auth, timeout=5, verify=False)
        got = a.json() if a.status_code == 200 else {}
        actual = (got.get("layer1", {}).get(side, {}) or {}).get(str(index))
        rows.append(("GET cached", a.status_code,
                     f"{side}:{index} = {actual}  (просили r={r_} g={g_} b={b_})"))
    except Exception as e:
        rows.append(("amb-pixel", "ERR", f"{type(e).__name__}: {e}"))
    return rows


def amb_modes(host: str, port: int, auth: Any, session: requests.Session,
              candidates: tuple[str, ...] = ("internal", "manual", "expert",
                                             "lounge", "follow_video")) -> list[tuple]:
    """Перебор значений `ambilight/mode` с чтением после каждого.

    Нужно потому, что `POST mode manual` вернул 200, а `GET` показал
    `lounge`: телевизор принял запрос и поставил своё. Пока неизвестно,
    какие значения он вообще признаёт, поштучная запись в `cached`
    бессмысленна — активный режим будет её перебивать.

    Между попытками подсветка принудительно включается: в выключенном
    состоянии режим может не меняться вообще.
    """
    mode_url = f"https://{host}:{port}/6/ambilight/mode"
    power_url = f"https://{host}:{port}/6/ambilight/power"
    rows: list[tuple] = []
    try:
        session.post(power_url, json={"power": "On"}, auth=auth, timeout=5, verify=False)
        before = session.get(mode_url, auth=auth, timeout=5, verify=False)
        rows.append(("исходный режим", before.status_code, _short(before.text, 40)))
    except Exception as e:
        return [("amb-modes", "ERR", type(e).__name__)]

    for cand in candidates:
        try:
            r = session.post(mode_url, json={"current": cand},
                             auth=auth, timeout=5, verify=False)
            time.sleep(0.5)
            a = session.get(mode_url, auth=auth, timeout=5, verify=False)
            got = (a.json() or {}).get("current") if a.status_code == 200 else None
            verdict = "ЗАКРЕПИЛСЯ" if got == cand else f"подменён на {got!r}"
            rows.append((f"mode={cand}", r.status_code, verdict))
        except Exception as e:
            rows.append((f"mode={cand}", "ERR", type(e).__name__))
    return rows


def amb_sweep(host: str, port: int, auth: Any, session: requests.Session,
              delay: float = 0.45) -> list[tuple]:
    """Бегущая точка по всем светодиодам по очереди.

    Смысл не в красоте, а в карте: по экрану видно, какой номер какой
    стороны где физически находится. Без этого любая пространственная
    картинка строится наугад — топология даёт только КОЛИЧЕСТВО
    светодиодов на сторону, а не порядок обхода и не направление.
    """
    url = f"https://{host}:{port}/6/ambilight/cached"
    rows: list[tuple] = []
    try:
        top = _topology(host, port, auth, session)
    except Exception as e:
        return [("topology", "ERR", type(e).__name__)]
    rows.append(("topology", 200, _short(json.dumps(top), 60)))

    for side in ("left", "top", "right", "bottom"):
        count = int(top.get(side, 0) or 0)
        if not count:
            rows.append((f"-- {side}", "", "нет светодиодов"))
            continue
        rows.append((f"-- {side}", "", f"{count} шт."))
        for i in range(count):
            dark = {str(j): {"r": 0, "g": 0, "b": 0} for j in range(count)}
            dark[str(i)] = {"r": 255, "g": 255, "b": 255}
            payload = {"layer1": {side: dark}}
            try:
                r = session.post(url, json=payload, auth=auth, timeout=5, verify=False)
                rows.append((f"{side}:{i}", r.status_code, ""))
            except Exception as e:
                rows.append((f"{side}:{i}", "ERR", type(e).__name__))
            time.sleep(delay)
    return rows


def probe_safe_keys(host: str, port: int, auth: Any, session: requests.Session,
                    delay: float = 1.2) -> list[tuple]:
    rows = []
    for k in SAFE_KEYS:
        url = f"https://{host}:{port}/6/input/key"
        try:
            r = session.post(url, json={"key": k}, auth=auth, timeout=5, verify=False)
            rows.append((f"key {k}", r.status_code, _short(r.text, 40)))
        except Exception as e:
            rows.append((f"key {k}", "ERR", type(e).__name__))
        time.sleep(delay)
    return rows


def launch_dial(host: str, app: str, session: requests.Session) -> list[tuple]:
    url = f"http://{host}:8008/apps/{app}"
    try:
        r = session.post(url, data="", timeout=10)
        return [(f"LAUNCH {app}", r.status_code, _short(r.text, 60) or "(empty)")]
    except Exception as e:
        return [(f"LAUNCH {app}", "ERR", type(e).__name__)]


def show(title: str, rows: list[tuple]) -> None:
    print(f"\n=== {title}")
    for path, code, body in rows:
        print(f"{str(path):<34} {str(code):<4} {body}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None, help="IP телевизора (по умолчанию из tv_creds.json)")
    ap.add_argument("--keys-safe", action="store_true", help="нажать безопасные кнопки")
    ap.add_argument("--keys-all", action="store_true", help="пройти весь список кнопок по группам")
    ap.add_argument("--include-standby", action="store_true", help="добавить Standby в конец прогона")
    ap.add_argument("--key", default=None, help="нажать одну кнопку")
    ap.add_argument("--screen", default=None, choices=["on", "off"], help="POST screenstate")
    ap.add_argument("--dump", action="store_true", help="сохранить полные тела живых путей")
    ap.add_argument("--amb-style", default=None, help="стиль подсветки: OFF | FOLLOW_AUDIO:VU_METER")
    ap.add_argument("--amb-lounge", default=None, help="цвет подсветки: hue,sat,bri")
    ap.add_argument("--amb-mode", default=None, help="режим подсветки: internal | manual | expert")
    ap.add_argument("--amb-pixel", default=None, help="один светодиод: сторона:номер:r,g,b")
    ap.add_argument("--amb-sweep", action="store_true", help="бегущая точка по всем светодиодам")
    ap.add_argument("--amb-modes", action="store_true", help="какие значения ambilight/mode телевизор признаёт")
    ap.add_argument("--launch", default=None, help="запустить приложение через DIAL (8008)")
    args = ap.parse_args()

    c = _creds()
    host = args.host or c.get("host") or ""
    port = int(c.get("port", 1926))
    if not host:
        return 2
    auth = HTTPDigestAuth(c["username"], c["password"])
    s = requests.Session()

    print(f"# target {host} (1926 digest / 1925 open / 8008 DIAL)")

    if args.launch:
        show("DIAL launch", launch_dial(host, args.launch, s))
        return 0

    if args.amb_style:
        show("ambilight style", set_amb_style(host, port, auth, s, args.amb_style))
        return 0

    if args.amb_mode:
        show("ambilight mode", amb_mode(host, port, auth, s, args.amb_mode))
        return 0

    if args.amb_pixel:
        show("ambilight pixel", amb_pixel(host, port, auth, s, args.amb_pixel))
        return 0

    if args.amb_modes:
        show("ambilight modes", amb_modes(host, port, auth, s))
        return 0

    if args.amb_sweep:
        show("ambilight sweep", amb_sweep(host, port, auth, s))
        return 0

    if args.amb_lounge:
        show("ambilight lounge", set_amb_lounge(host, port, auth, s, args.amb_lounge))
        return 0

    if args.screen:
        show("screenstate", set_screen(host, port, auth, s, args.screen))
        return 0

    if args.key:
        url = f"https://{host}:{port}/6/input/key"
        r = s.post(url, json={"key": args.key}, auth=auth, timeout=5, verify=False)
        show("key", [(f"key {args.key}", r.status_code, _short(r.text, 40) or "(empty)")])
        return 0 if r.status_code < 400 else 1

    if args.keys_all:
        show("ВСЕ кнопки по группам", probe_keys_all(host, port, auth, s, args.include_standby))
        return 0

    show("API v6 (1926, digest)", probe_v6(host, port, auth, s))
    show("API v1 (1925, без пары)", probe_v1(host, s))
    show("DIAL (8008)", probe_dial(host, s))

    if args.keys_safe:
        show("безопасные кнопки", probe_safe_keys(host, port, auth, s))

    if args.dump:
        show("полные тела -> data\\tv_probe", dump_live(host, port, auth, s))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
