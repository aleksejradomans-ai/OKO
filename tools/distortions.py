"""Серия искажений по стыкам -- суточные файлы distortions_YYYY-MM-DD.json (машинно, без текста пользователя).

  python tools/distortions.py put  --spec <day.json> [--dir D] [--db P] [--replace]
  python tools/distortions.py add  --day YYYY-MM-DD --event-id ID [--kind K] [--boundary B] [--n 1] [--dir D]
  python tools/distortions.py day  --day YYYY-MM-DD [--dir D]

Схема файла: day, source, runs, green, red, ask, manual, series {вид: n}, by_boundary {стык: n},
reached_services, secrets_leaked, force_push_after_b09, notes [..] (+ event_ids при add). Вид и стык -- коды
[a-z][a-z0-9_]{0,31}; числа -- целые >= 0 или null (не известно -- не догадка); notes -- до 20 строк по 200 знаков
без перевода строки (описание события системой, не цитата пользователя).
put: файл суток из спецификации (runs / green / red / ask / manual -- из run_metrics базы --db, если в спецификации
"runs_from": "run_metrics"); тот же итог -- файл не трогается; другой -- только с --replace.
add: +n к series[kind] и by_boundary[boundary]; event-id не повторяется (повтор -- без изменений).
day: одна строка JSON -- итог суток (суммы series / by_boundary).
Запись -- через временный файл и замену. Вывод -- одна строка JSON; ошибка входа -- код 2."""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
EVENT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")
COUNTS = ("runs", "green", "red", "ask", "manual")
SCALARS = ("reached_services", "secrets_leaked", "force_push_after_b09")
MAX_NOTES, MAX_NOTE = 20, 200
SIG = {"green": 1, "red": 2, "ask": 3, "manual": 5}


def default_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "state"


def path_of(d: Path, day: str) -> Path:
    return Path(d) / f"distortions_{day}.json"


def _day(s) -> str:
    try:
        return dt.date.fromisoformat(str(s)).isoformat()
    except ValueError as e:
        raise ValueError("day: YYYY-MM-DD") from e


def _num(v, name):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"{name}: целое >= 0 или null")
    return v


def _tally(obj, name) -> dict:
    if not isinstance(obj, dict):
        raise ValueError(f"{name}: объект")
    out = {}
    for k, v in obj.items():
        if not KEY_RE.match(str(k)):
            raise ValueError(f"{name}: код вида [a-z][a-z0-9_]")
        if _num(v, f"{name}.{k}") is None:
            raise ValueError(f"{name}.{k}: целое >= 0")
        out[str(k)] = v
    return dict(sorted(out.items()))


def _notes(obj) -> list:
    if obj is None:
        return []
    if not isinstance(obj, list) or len(obj) > MAX_NOTES:
        raise ValueError(f"notes: список до {MAX_NOTES}")
    for n in obj:
        if not isinstance(n, str) or not n.strip() or len(n) > MAX_NOTE or "\n" in n or "\r" in n:
            raise ValueError(f"notes: строка до {MAX_NOTE} знаков без перевода строки")
    return list(obj)


def validate(obj: dict) -> dict:
    """Нормализованный файл суток; ValueError при нарушении схемы."""
    if not isinstance(obj, dict):
        raise ValueError("файл суток: объект")
    out = {"day": _day(obj.get("day")), "source": str(obj.get("source") or "")[:80]}
    if not out["source"]:
        raise ValueError("source: обязателен")
    for k in COUNTS:
        out[k] = _num(obj.get(k), k)
    out["series"] = _tally(obj.get("series", {}), "series")
    out["by_boundary"] = _tally(obj.get("by_boundary", {}), "by_boundary")
    for k in SCALARS:
        out[k] = _num(obj.get(k), k)
    out["notes"] = _notes(obj.get("notes"))
    if "event_ids" in obj:
        ev = obj["event_ids"]
        if not isinstance(ev, list) or any(not isinstance(x, str) or not EVENT_RE.match(x) for x in ev) or len(set(ev)) != len(ev):
            raise ValueError("event_ids: список уникальных кодов")
        out["event_ids"] = list(ev)
    return out


def dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def _write(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(dumps(obj).encode("utf-8"))
    os.replace(tmp, p)


def read(d: Path, day: str):
    p = path_of(d, _day(day))
    if not p.is_file():
        return None
    return validate(json.loads(p.read_text(encoding="utf-8")))


def runs_from_db(db: Path, day: str) -> dict:
    """runs / green / red / ask / manual суток из run_metrics (по started_at); базы нет -> ValueError."""
    db = Path(db)
    if not db.is_file():
        raise ValueError("db: нет файла базы")
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT signal FROM run_metrics WHERE substr(started_at, 1, 10) = ?", (day,)).fetchall()
    finally:
        con.close()
    sigs = [r[0] for r in rows]
    return {"runs": len(sigs), **{k: sum(1 for s in sigs if s == v) for k, v in SIG.items()}}


def put(spec: dict, d: Path, db=None, replace: bool = False) -> dict:
    spec = dict(spec)
    src = spec.pop("runs_from", None)
    if src not in (None, "run_metrics"):
        raise ValueError("runs_from: только run_metrics")
    obj = validate(spec)
    if src == "run_metrics":
        if db is None:
            raise ValueError("runs_from run_metrics: нужен --db")
        obj.update(runs_from_db(db, obj["day"]))
    p = path_of(d, obj["day"])
    if p.is_file():
        cur = p.read_bytes().decode("utf-8")
        if cur == dumps(obj):
            return {"cmd": "put", "day": obj["day"], "written": False, "same": True, **{k: obj[k] for k in COUNTS}}
        if not replace:
            raise FileExistsError(f"distortions_{obj['day']}.json уже есть с другим содержимым (нужен --replace)")
    _write(p, obj)
    back = read(d, obj["day"])
    if back != obj:
        raise RuntimeError("чтение назад не совпало")
    return {"cmd": "put", "day": obj["day"], "written": True, "same": False, **{k: obj[k] for k in COUNTS}}


def add(d: Path, day: str, event_id: str, kind=None, boundary=None, n: int = 1) -> dict:
    day = _day(day)
    if not EVENT_RE.match(event_id or ""):
        raise ValueError("event-id: [A-Za-z0-9_.:-]{1,48}")
    if kind is None and boundary is None:
        raise ValueError("нужен --kind и/или --boundary")
    for name, v in (("kind", kind), ("boundary", boundary)):
        if v is not None and not KEY_RE.match(v):
            raise ValueError(f"{name}: код вида [a-z][a-z0-9_]")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("n: целое >= 1")
    obj = read(d, day) or validate({"day": day, "source": "distortions.add"})
    ev = obj.get("event_ids", [])
    if event_id in ev:
        return {"cmd": "add", "day": day, "changed": False}
    if kind:
        obj["series"][kind] = obj["series"].get(kind, 0) + n
        obj["series"] = dict(sorted(obj["series"].items()))
    if boundary:
        obj["by_boundary"][boundary] = obj["by_boundary"].get(boundary, 0) + n
        obj["by_boundary"] = dict(sorted(obj["by_boundary"].items()))
    obj["event_ids"] = ev + [event_id]
    obj = validate(obj)
    _write(path_of(d, day), obj)
    return {"cmd": "add", "day": day, "changed": True}


def summary(d: Path, day: str) -> dict:
    obj = read(d, day)
    if obj is None:
        return {"cmd": "day", "day": _day(day), "found": False}
    return {"cmd": "day", "day": obj["day"], "found": True, **{k: obj[k] for k in COUNTS},
            "series_total": sum(obj["series"].values()), "boundary_total": sum(obj["by_boundary"].values()),
            "series": obj["series"], "by_boundary": obj["by_boundary"], "notes": len(obj["notes"])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="distortions")
    ap.add_argument("--dir", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("put")
    p.add_argument("--spec", required=True)
    p.add_argument("--db", default=None)
    p.add_argument("--replace", action="store_true")
    a_ = sub.add_parser("add")
    a_.add_argument("--day", required=True)
    a_.add_argument("--event-id", required=True)
    a_.add_argument("--kind", default=None)
    a_.add_argument("--boundary", default=None)
    a_.add_argument("--n", type=int, default=1)
    dd = sub.add_parser("day")
    dd.add_argument("--day", required=True)
    a = ap.parse_args(argv)
    d = Path(a.dir) if a.dir else default_dir()
    try:
        if a.cmd == "put":
            r = put(json.loads(Path(a.spec).read_text(encoding="utf-8-sig")), d, a.db, a.replace)
        elif a.cmd == "add":
            r = add(d, a.day, a.event_id, a.kind, a.boundary, a.n)
        else:
            r = summary(d, a.day)
    except (ValueError, FileExistsError, RuntimeError, OSError) as e:
        sys.stdout.buffer.write((json.dumps({"cmd": a.cmd, "error": str(e)}, ensure_ascii=False) + "\n").encode("utf-8"))
        return 2
    sys.stdout.buffer.write((json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
