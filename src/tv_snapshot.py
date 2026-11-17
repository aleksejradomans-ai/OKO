r"""tv_snapshot.py — срез настроек телевизора и сравнение с шаблоном.

Диагностический инструмент того же класса, что `tv_probe.py`: ничем не
импортируется, в манифест не входит, тестов нет сознательно — он читает
состояние и сравнивает JSON.

Зачем. Телевизор на запись отвечает 200 независимо от результата: так
было со `screenstate`, с `OFF` в стиле подсветки, с `CINEMA` вместо
NATURAL и с цветом вне режима lounge. Проверять каждую правку глазами не
получится, поэтому нужен срез: снять профиль до изменения, снять после и
сравнить. Расхождение видно построчно, а не на ощупь.

Срез = «маркер» состояния. Один из срезов можно объявить шаблоном
(`--save baseline`) и потом сверять с ним: так видно, что система ушла от
эталона, даже если никто не помнит, что именно менял.

ВАЖНО про шумные пути. `ambilight/measured`, `processed` и `cached` — это
покадровые цвета светодиодов, они меняются непрерывно и в сравнении дают
только мусор. Они исключены из среза по умолчанию (`NOISY`), для них есть
отдельный режим `--audio-ab`, который измеряет их ОСМЫСЛЕННО: с музыкой и
в тишине, с подсчётом разброса в обоих случаях. Без такой пары чисел
разброс сам по себе ничего не доказывает: сравнивать его не с чем.

Режимы:
  python tv_snapshot.py                 — срез на экран
  python tv_snapshot.py --save baseline — сохранить как data\tv_snapshot\baseline.json
  python tv_snapshot.py --diff baseline — сравнить текущее состояние с шаблоном
  python tv_snapshot.py --diff a b      — сравнить два сохранённых среза
  python tv_snapshot.py --list          — что сохранено
  python tv_snapshot.py --audio-ab      — реагирует ли подсветка на звук
  python tv_snapshot.py --volume-swing  — проверка реальности регулировки звука
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import urllib3
from requests.auth import HTTPDigestAuth

import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OUT_DIR = config.BASE_DIR / "tv_snapshot"
CREDS = config.BASE_DIR / "tv_creds.json"

# Пути, которые входят в срез: настройки и состояние, меняющиеся только
# по чьему-то решению. Порядок фиксирован, чтобы diff двух срезов не
# зависел от порядка обхода.
PATHS = [
    "system",
    "audio/volume",
    "activities/current",
    "activities/tv",
    "ambilight/power",
    "ambilight/mode",
    "ambilight/topology",
    "ambilight/currentconfiguration",
    "ambilight/supportedstyles",
    "ambilight/lounge",
    "channeldb/tv",
    "channeldb/tv/channelLists/all",
    "menuitems/settings/structure",
    "context",
    "input/textentry",
    "network/devices",
]

# Непрерывно меняющиеся пути — в срез не идут (см. docstring).
NOISY = ["ambilight/measured", "ambilight/processed", "ambilight/cached"]


def _client() -> tuple[str, int, Any, requests.Session]:
    if not CREDS.exists():
        raise SystemExit(f"FATAL: нет {CREDS} — сначала пайринг")
    c = json.loads(CREDS.read_text(encoding="utf-8"))
    return (c.get("host", ""), int(c.get("port", 1926)),
            HTTPDigestAuth(c["username"], c["password"]), requests.Session())


def take(host: str, port: int, auth: Any, s: requests.Session) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "_meta": {
            "taken_at": datetime.now().isoformat(timespec="seconds"),
            "host": host,
            "port": port,
        }
    }
    for p in PATHS:
        try:
            r = s.get(f"https://{host}:{port}/6/{p}", auth=auth, timeout=5, verify=False)
            if r.status_code != 200:
                snap[p] = {"_status": r.status_code}
                continue
            try:
                snap[p] = r.json()
            except ValueError:
                snap[p] = {"_text": r.text[:400]}
        except Exception as e:
            snap[p] = {"_error": type(e).__name__}
    return snap


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """JSON -> плоские пары `путь.ключ = значение`.

    Сравнивать вложенные структуры «как есть» бесполезно: отчёт получается
    либо «всё изменилось», либо нечитаемый. Плоские пути дают построчный
    diff, в котором видно именно то поле, которое поехало.
    """
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def diff(a: dict[str, Any], b: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Возвращает список (путь, было, стало). `_meta` исключён: время
    снятия различается всегда и забивает отчёт."""
    fa = {k: v for k, v in flatten(a).items() if not k.startswith("_meta")}
    fb = {k: v for k, v in flatten(b).items() if not k.startswith("_meta")}
    rows = []
    for k in sorted(set(fa) | set(fb)):
        va, vb = fa.get(k, "<нет>"), fb.get(k, "<нет>")
        if va != vb:
            rows.append((k, va, vb))
    return rows


def audio_ab(host: str, port: int, auth: Any, s: requests.Session,
             samples: int = 15, delay: float = 0.35) -> None:
    """Реагирует ли подсветка на звук: замер с музыкой против замера в
    тишине (mute на телевизоре).

    Один замер сам по себе ничего не доказывает — у разброса нет точки
    отсчёта. Даже на неподвижной картинке цвета слегка дышат, поэтому
    6% колебаний можно принять за реакцию на музыку, не имея сравнения.
    Здесь два замера подряд в одинаковых условиях, различающихся только
    наличием звука; вывод делается по отношению разбросов.
    """
    base = f"https://{host}:{port}/6/ambilight"

    def measure(label: str) -> float:
        sums = []
        for _ in range(samples):
            try:
                r = s.get(base + "/measured", auth=auth, timeout=5, verify=False)
                px = r.json().get("layer1", {})
                vals = [v["r"] + v["g"] + v["b"]
                        for side in px.values() for v in side.values()]
                sums.append(sum(vals))
            except Exception:
                pass
            time.sleep(delay)
        if len(sums) < 3:
            print(f"{label}: мало данных")
            return 0.0
        sd = statistics.pstdev(sums)
        print(f"{label}: n={len(sums)} mean={statistics.mean(sums):.0f} "
              f"stdev={sd:.1f} spread={max(sums) - min(sums)}")
        return sd

    s.post(base + "/power", json={"power": "On"}, auth=auth, timeout=5, verify=False)
    s.post(base + "/currentconfiguration",
           json={"styleName": "FOLLOW_AUDIO", "isExpert": False,
                 "menuSetting": "VU_METER"},
           auth=auth, timeout=5, verify=False)
    time.sleep(1.0)

    vol_url = f"https://{host}:{port}/6/audio/volume"
    cur = s.get(vol_url, auth=auth, timeout=5, verify=False).json()

    print("--- со звуком (играйте музыку)")
    loud = measure("sound")

    print("--- тишина (mute на телевизоре)")
    s.post(vol_url, json={"muted": True, "current": cur.get("current", 0)},
           auth=auth, timeout=5, verify=False)
    time.sleep(1.5)
    quiet = measure("silence")

    s.post(vol_url, json={"muted": bool(cur.get("muted", False)),
                          "current": cur.get("current", 0)},
           auth=auth, timeout=5, verify=False)

    print("--- вывод")
    if quiet <= 0:
        print("нет данных для сравнения")
    elif loud > quiet * 2.5:
        print(f"РЕАГИРУЕТ: разброс со звуком в {loud / quiet:.1f} раза больше")
    else:
        print(f"НЕ ДОКАЗАНО: отношение {loud / max(quiet, 0.1):.1f} — "
              f"колебания те же, что в тишине. Либо подсветка не видит звук, "
              f"либо mute на телевизоре не влияет на оптический выход "
              f"(тогда тишины в замере не было).")


def volume_swing(host: str, port: int, auth: Any, s: requests.Session) -> None:
    """Перепад громкости на 50% диапазона: проверка, что регулировка
    реальна, а не только меняет число в API.

    Нужна потому, что звук выведен по оптике на внешние колонки: запись
    `audio/volume` проходит с кодом 200 и прочитанное значение меняется,
    но слышимого эффекта может не быть вообще. Отличить одно от другого
    можно только ухом — поэтому скрипт делает громкий и тихий уровень с
    паузой и прямо спрашивает, была ли разница.
    """
    url = f"https://{host}:{port}/6/audio/volume"
    before = s.get(url, auth=auth, timeout=5, verify=False).json()
    lo, hi = int(before.get("min", 0)), int(before.get("max", 60))
    half = (lo + hi) // 2
    print(f"исходно: {before}")

    for label, level in (("ВЕРХ", hi // 2 + hi // 4), ("НИЗ", half // 3)):
        s.post(url, json={"muted": False, "current": level},
               auth=auth, timeout=5, verify=False)
        time.sleep(0.6)
        got = s.get(url, auth=auth, timeout=5, verify=False).json()
        print(f"{label}: просили {level}, в API {got.get('current')}, "
              f"muted={got.get('muted')}  — слушайте 4 с")
        time.sleep(4)

    s.post(url, json={"muted": bool(before.get("muted", False)),
                      "current": int(before.get("current", 0))},
           auth=auth, timeout=5, verify=False)
    print(f"вернул как было: {s.get(url, auth=auth, timeout=5, verify=False).json()}")
    print("Если на слух разницы между ВЕРХ и НИЗ не было — регулировка по API "
          "не управляет оптическим выходом, и команды громкости в боте нужно "
          "отключить, а не считать рабочими.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", default=None, metavar="ИМЯ")
    ap.add_argument("--diff", nargs="+", default=None, metavar="ИМЯ")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--audio-ab", action="store_true")
    ap.add_argument("--volume-swing", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.list:
        for f in sorted(OUT_DIR.glob("*.json")):
            print(f"{f.stem:<20} {f.stat().st_size:>7} б  {f.name}")
        return 0

    host, port, auth, s = _client()

    if args.audio_ab:
        audio_ab(host, port, auth, s)
        return 0

    if args.volume_swing:
        volume_swing(host, port, auth, s)
        return 0

    if args.diff:
        if len(args.diff) == 1:
            old_path = OUT_DIR / f"{args.diff[0]}.json"
            if not old_path.exists():
                print(f"нет среза {old_path}")
                return 2
            old = json.loads(old_path.read_text(encoding="utf-8"))
            new = take(host, port, auth, s)
            names = (args.diff[0], "сейчас")
        else:
            paths = [OUT_DIR / f"{n}.json" for n in args.diff[:2]]
            missing = [p for p in paths if not p.exists()]
            if missing:
                print("нет срезов: " + ", ".join(str(p) for p in missing))
                return 2
            old, new = (json.loads(p.read_text(encoding="utf-8")) for p in paths)
            names = tuple(args.diff[:2])

        rows = diff(old, new)
        print(f"=== diff {names[0]} -> {names[1]}: расхождений {len(rows)}")
        for k, a, b in rows:
            print(f"{k}\n    было : {a}\n    стало: {b}")
        return 0

    snap = take(host, port, auth, s)
    if args.save:
        p = OUT_DIR / f"{args.save}.json"
        p.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"срез сохранён: {p} ({p.stat().st_size} б)")
        return 0

    for k, v in snap.items():
        if k == "_meta":
            print(f"{k}: {v}")
            continue
        text = json.dumps(v, ensure_ascii=False)
        print(f"{k:<34} {text[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
