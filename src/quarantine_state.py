"""quarantine_state.py -- обезличенное состояние карантина файлов: суточный слепок -> недельный слепок -> зум.

Три уровня (спецификация -- docs/QUARANTINE_STATE.md):
  1. snapshot(dir): агрегат каталога карантина БЕЗ содержимого и имён файлов -- число файлов, категории причины
     (из manifest.tsv), размеры по категориям, расширения, min/max времени переноса, отпечаток набора sha256.
     Канонический JSON -> aggregate_sha256; daily_uuid = uuid5(NAMESPACE, sha256(канонический JSON {day, dir,
     aggregate_sha256})) -- одинаковый агрегат в разные сутки даёт разные суточные слепки. Запись в отдельную
     SQLite-базу (таблица quarantine_state_snapshots); повтор в те же сутки с тем же агрегатом -- та же строка.
  2. weekly(): до 7 последних суток (по одному слепку на сутки -- последний) -> weekly_uuid = uuid5(NAMESPACE,
     sha256(канонический JSON {daily: [...], aggregate: {...}})); partial = меньше 7 суток. Точки спада --
     сутки, где метрика ниже медианы окна больше чем на k * MAD (MAD -- медиана абсолютных отклонений;
     при MAD = 0 масштаб = max(1, 5 % медианы)); k -- параметр (по умолчанию 3).
  3. zoom(weekly_uuid): для каждой точки спада -- суточный слепок и метрики/категории, давшие отклонение.
run_metrics (B15): числовые метрики прогонов блоков (задача, блок, начало/конец, шаги, откаты, UAC, HITL, ошибки
разбора и сети, повторы нестабильных тестов FLAKY, сигнал) -- record_run(); weekly() добавляет по суткам ряды run.* в тот же поиск точек спада (k * MAD).
Без строк run_metrics недельный слепок такой же, как раньше.
cycle(root): для каждого каталога root/<glob>: snapshot (идемпотентно в пределах суток), purge (dry-run -- только
отчёт), в выбранный день недели -- weekly; одна строка-итог на каталог и итог цикла, по строке в лог-файл.
purge(dir): каталог удаляется ТОЛЬКО если есть слепок с тем же агрегатом (пересчёт перед удалением) И возраст
карантина (последний перенос по manifest.tsv, иначе mtime файлов) не меньше ttl_h. Иначе -- причина, 0 удалений.
Только стандартная библиотека; модуль не встроен в службы и ничего не делает при импорте."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import statistics
import uuid
from pathlib import Path
from typing import Optional

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "oko:quarantine_state")
DEFAULT_TTL_H = 24.0
DEFAULT_K = 3.0
WEEK_DAYS = 7
MANIFEST = "manifest.tsv"
SCHEMA_VERSION = 1
# категория причины переноса -- по ключевым словам столбца reason (первое совпадение)
CATEGORY_RULES = (
    ("duplicate", re.compile(r"(?i)дубл|лишн\w* копи|duplicate|same sha")),
    ("merge_source", re.compile(r"(?i)склеен|merge")),
    ("old_version", re.compile(r"(?i)стар\w* верси|old version|заменен")),
    ("test_junk", re.compile(r"(?i)сирот|без записи|тест|test|orphan")),
)
METRICS = ("files", "bytes")
RUN_COUNTS = ("steps", "rollbacks", "uac", "hitl", "parse_errors", "net_errors", "flaky")
RUN_SIGNALS = (1, 2, 3, 5)
_TASK_RE = re.compile(r"^B\d{2}[a-z]?$")
_BLOCK_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def default_db() -> Path:
    env = os.getenv("OKO_QUARANTINE_STATE_DB", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "state" / "quarantine_state.db"


def canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def category(reason: str) -> str:
    for name, rx in CATEGORY_RULES:
        if rx.search(reason or ""):
            return name
    return "other"


def _file_sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _manifest(qdir: Path) -> dict:
    m = {}
    p = qdir / MANIFEST
    if not p.is_file():
        return m
    with p.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            key = (row.get("quarantine_path") or "").replace("\\", "/")
            if key:
                m[key] = row
    return m


def _parse_time(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(s.strip())
    except (ValueError, AttributeError):
        return None


def aggregate(qdir: Path) -> dict:
    """Обезличенный агрегат каталога: без имён файлов и содержимого."""
    qdir = Path(qdir)
    man = _manifest(qdir)
    cats: dict = {}
    exts: dict = {}
    shas = []
    times = []
    n = total = unlisted = 0
    for p in sorted(qdir.rglob("*")):
        if not p.is_file() or (p.parent == qdir and p.name == MANIFEST):
            continue
        rel = p.relative_to(qdir).as_posix()
        size = p.stat().st_size
        row = man.get(rel)
        if row is None:
            unlisted += 1
            cat = "unlisted"
            t = dt.datetime.fromtimestamp(p.stat().st_mtime)
        else:
            cat = category(row.get("reason", ""))
            t = _parse_time(row.get("moved_at", "")) or dt.datetime.fromtimestamp(p.stat().st_mtime)
        c = cats.setdefault(cat, {"files": 0, "bytes": 0})
        c["files"] += 1
        c["bytes"] += size
        ext = p.suffix.lower() or "(none)"
        exts[ext] = exts.get(ext, 0) + 1
        shas.append(_file_sha(p))
        times.append(t)
        n += 1
        total += size
    return {
        "schema": SCHEMA_VERSION,
        "files": n,
        "bytes": total,
        "categories": cats,
        "extensions": exts,
        "unlisted": unlisted,
        "manifest_rows": len(man),
        "time_min": min(times).isoformat(timespec="seconds") if times else None,
        "time_max": max(times).isoformat(timespec="seconds") if times else None,
        "content_digest": sha256_hex("\n".join(sorted(shas)).encode()),
    }


def daily_uuid(agg: dict, day: str, dir_key: str) -> tuple[str, str]:
    h = sha256_hex(canonical(agg))
    return str(uuid.uuid5(NAMESPACE, sha256_hex(canonical({"day": day, "dir": dir_key, "aggregate_sha256": h})))), h


def connect(db: Optional[Path] = None) -> sqlite3.Connection:
    db = Path(db or default_db())
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE IF NOT EXISTS quarantine_state_snapshots (
        daily_uuid TEXT PRIMARY KEY, day TEXT NOT NULL, dir TEXT NOT NULL, created_at TEXT NOT NULL,
        aggregate_json TEXT NOT NULL, aggregate_sha256 TEXT NOT NULL, ttl_h REAL NOT NULL, purged_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS quarantine_state_weekly (
        weekly_uuid TEXT PRIMARY KEY, end_day TEXT NOT NULL, n_days INTEGER NOT NULL, partial INTEGER NOT NULL,
        daily_uuids_json TEXT NOT NULL, aggregate_json TEXT NOT NULL, dips_json TEXT NOT NULL, k REAL NOT NULL,
        created_at TEXT NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS run_metrics (
        run_id TEXT PRIMARY KEY, task TEXT NOT NULL, block TEXT NOT NULL, started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL, duration_s REAL NOT NULL, steps INTEGER NOT NULL, rollbacks INTEGER NOT NULL,
        uac INTEGER NOT NULL, hitl INTEGER NOT NULL, parse_errors INTEGER NOT NULL, net_errors INTEGER NOT NULL,
        flaky INTEGER NOT NULL, signal INTEGER NOT NULL, recorded_at TEXT NOT NULL)""")
    return con


def _dir_key(qdir: Path) -> str:
    return str(Path(qdir).resolve())


def snapshot(qdir: Path, db: Optional[Path] = None, ttl_h: float = DEFAULT_TTL_H,
             now: Optional[dt.datetime] = None) -> dict:
    now = now or dt.datetime.now()
    agg = aggregate(qdir)
    du, h = daily_uuid(agg, now.date().isoformat(), _dir_key(qdir))
    con = connect(db)
    try:
        cur = con.execute("INSERT OR IGNORE INTO quarantine_state_snapshots (daily_uuid, day, dir, created_at, "
                          "aggregate_json, aggregate_sha256, ttl_h) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (du, now.date().isoformat(), _dir_key(qdir), now.isoformat(timespec="seconds"),
                           canonical(agg).decode(), h, float(ttl_h)))
        con.commit()
        new = cur.rowcount == 1
    finally:
        con.close()
    return {"daily_uuid": du, "aggregate_sha256": h, "new": new, "aggregate": agg}


def purge(qdir: Path, db: Optional[Path] = None, dry_run: bool = False,
          now: Optional[dt.datetime] = None) -> dict:
    """-> {deleted: 0|1, reason}. Удаление только при слепке с тем же агрегатом И возрасте >= ttl_h."""
    now = now or dt.datetime.now()
    qdir = Path(qdir)
    if not qdir.is_dir():
        return {"deleted": 0, "reason": "no_directory"}
    rp = qdir.resolve()
    if rp.parent == rp or rp == Path.home().resolve() or Path(__file__).resolve().is_relative_to(rp):
        return {"deleted": 0, "reason": "refused_path"}
    agg = aggregate(qdir)
    h = sha256_hex(canonical(agg))
    con = connect(db)
    try:
        row = con.execute("SELECT daily_uuid, ttl_h, purged_at FROM quarantine_state_snapshots WHERE dir = ? AND "
                          "aggregate_sha256 = ? ORDER BY created_at DESC LIMIT 1", (_dir_key(qdir), h)).fetchone()
        if row is None:
            anyrow = con.execute("SELECT 1 FROM quarantine_state_snapshots WHERE dir = ?", (_dir_key(qdir),)).fetchone()
            return {"deleted": 0, "reason": "directory_changed_since_snapshot" if anyrow else "no_snapshot",
                    "aggregate_sha256": h}
        ttl = float(row[1])
        tmax = _parse_time(agg["time_max"] or "")
        if tmax is None:
            return {"deleted": 0, "reason": "no_time", "daily_uuid": row[0]}
        age_h = (now - tmax).total_seconds() / 3600.0
        if age_h < ttl:
            return {"deleted": 0, "reason": "age_below_ttl", "age_h": round(age_h, 2), "ttl_h": ttl,
                    "daily_uuid": row[0]}
        if dry_run:
            return {"deleted": 0, "reason": "dry_run_would_delete", "age_h": round(age_h, 2), "ttl_h": ttl,
                    "daily_uuid": row[0]}
        shutil.rmtree(qdir)
        con.execute("UPDATE quarantine_state_snapshots SET purged_at = ? WHERE daily_uuid = ?",
                    (now.isoformat(timespec="seconds"), row[0]))
        con.commit()
        return {"deleted": 1, "reason": "verified_snapshot_and_ttl", "age_h": round(age_h, 2), "ttl_h": ttl,
                "daily_uuid": row[0]}
    finally:
        con.close()


def _metrics(agg: dict) -> dict:
    m = {k: float(agg.get(k) or 0) for k in METRICS}
    for cat, v in (agg.get("categories") or {}).items():
        m[f"cat.{cat}.files"] = float(v.get("files", 0))
        m[f"cat.{cat}.bytes"] = float(v.get("bytes", 0))
    return m


def dips(series: list, k: float = DEFAULT_K) -> list:
    """Индексы точек спада: x < median − k·scale, scale = MAD или max(1, 5 % медианы) при MAD = 0."""
    if len(series) < 3:
        return []
    med = statistics.median(series)
    mad = statistics.median(abs(x - med) for x in series)
    scale = mad if mad > 0 else max(1.0, 0.05 * abs(med))
    return [i for i, x in enumerate(series) if x < med - k * scale]


def run_id(task: str, block: str, started_at: str) -> str:
    return str(uuid.uuid5(NAMESPACE, sha256_hex(canonical({"run": [task, block, started_at]}))))


def _norm_time(v, name: str) -> str:
    try:
        t = dt.datetime.fromisoformat(str(v))
    except ValueError as e:
        raise ValueError(f"{name}: время ISO 8601") from e
    return t.replace(tzinfo=None, microsecond=0).isoformat()


def validate_run(rec: dict) -> tuple:
    """Проверка записи прогона -> (task, block, started_at, ended_at, duration_s, {счётчики}, signal); иначе ValueError."""
    task, block = str(rec.get("task", "")), str(rec.get("block", ""))
    if not _TASK_RE.match(task):
        raise ValueError("task: код задачи вида B15")
    if not _BLOCK_RE.match(block):
        raise ValueError("block: латиница/цифры/_ до 24 знаков")
    st, en = _norm_time(rec.get("started_at"), "started_at"), _norm_time(rec.get("ended_at"), "ended_at")
    dur = (dt.datetime.fromisoformat(en) - dt.datetime.fromisoformat(st)).total_seconds()
    if dur < 0:
        raise ValueError("ended_at раньше started_at")
    vals = {}
    for k in RUN_COUNTS:
        v = rec.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{k}: целое >= 0")
        vals[k] = v
    sig = rec.get("signal")
    if isinstance(sig, bool) or sig not in RUN_SIGNALS:
        raise ValueError(f"signal: одно из {RUN_SIGNALS}")
    return task, block, st, en, dur, vals, sig


def record_run(rec: dict, db: Optional[Path] = None, now: Optional[dt.datetime] = None) -> dict:
    """Одна строка run_metrics (только числа + коды задачи/блока). run_id = uuid5(task, block, started_at):
    повтор той же записи -- та же строка (new=False). Неверное поле -> ValueError, ничего не пишется."""
    now = now or dt.datetime.now()
    task, block, st, en, dur, vals, sig = validate_run(rec)
    rid = run_id(task, block, st)
    con = connect(db)
    try:
        cur = con.execute("INSERT OR IGNORE INTO run_metrics (run_id, task, block, started_at, ended_at, duration_s, "
                          + ", ".join(RUN_COUNTS) + ", signal, recorded_at) VALUES (" + ", ".join("?" * (8 + len(RUN_COUNTS))) + ")",
                          (rid, task, block, st, en, dur, *[vals[k] for k in RUN_COUNTS], sig,
                           now.isoformat(timespec="seconds")))
        con.commit()
        new = cur.rowcount == 1
    finally:
        con.close()
    return {"run_id": rid, "new": new, "task": task, "block": block, "duration_s": dur, "signal": sig}


def _run_series(con: sqlite3.Connection, start: dt.date, end: dt.date) -> dict:
    """{day: {run.<метрика>: число}} по прогонам, начатым в окне. Спад деградации виден в процентах:
    run.green_pct -- доля прогонов с сигналом 1, run.clean_pct -- доля прогонов без откатов, FLAKY, ошибок разбора
    и сети (0..100; в процентах, чтобы нижняя граница масштаба 1 правила k * MAD не гасила долю)."""
    rows = con.execute("SELECT substr(started_at, 1, 10), duration_s, " + ", ".join(RUN_COUNTS) + ", signal "
                       "FROM run_metrics WHERE substr(started_at, 1, 10) >= ? AND substr(started_at, 1, 10) <= ?",
                       (start.isoformat(), end.isoformat())).fetchall()
    by = {}
    for r in rows:
        m = by.setdefault(r[0], {"run.count": 0.0, "run.duration_s": 0.0, "run.green": 0.0, "run.clean": 0.0,
                                 **{f"run.{k}": 0.0 for k in RUN_COUNTS}})
        m["run.count"] += 1
        m["run.duration_s"] += float(r[1])
        for i, k in enumerate(RUN_COUNTS):
            m[f"run.{k}"] += float(r[2 + i])
        m["run.green"] += 1.0 if r[-1] == 1 else 0.0
        d = dict(zip(RUN_COUNTS, r[2:2 + len(RUN_COUNTS)]))
        m["run.clean"] += 1.0 if not (d["rollbacks"] or d["flaky"] or d["parse_errors"] or d["net_errors"]) else 0.0
    for m in by.values():
        m["run.green_pct"] = round(100.0 * m.pop("run.green") / m["run.count"], 3)
        m["run.clean_pct"] = round(100.0 * m.pop("run.clean") / m["run.count"], 3)
    return by


def weekly(db: Optional[Path] = None, end_day: Optional[str] = None, k: float = DEFAULT_K,
           now: Optional[dt.datetime] = None) -> dict:
    now = now or dt.datetime.now()
    end = dt.date.fromisoformat(end_day) if end_day else now.date()
    start = end - dt.timedelta(days=WEEK_DAYS - 1)
    con = connect(db)
    try:
        rows = con.execute("SELECT daily_uuid, day, aggregate_json, created_at FROM quarantine_state_snapshots "
                           "WHERE day >= ? AND day <= ? ORDER BY day, created_at", (start.isoformat(), end.isoformat())).fetchall()
        last = {}
        for du, day, aj, ca in rows:
            last[day] = (du, json.loads(aj))          # последний слепок суток
        runs = _run_series(con, start, end)
        days = sorted(set(last) | set(runs))
        daily = [last[d][0] if d in last else None for d in days]
        mets = [dict(_metrics(last[d][1]) if d in last else {}, **runs.get(d, {})) for d in days]
        keys = sorted({k_ for m in mets for k_ in m})
        series = {key: [m.get(key, 0.0) for m in mets] for key in keys}
        dip = {}
        for key, s in series.items():
            for i in dips(s, k):
                dip.setdefault(days[i], []).append(key)
        agg = {"days": days, "series": series, "k": k}
        wh = sha256_hex(canonical({"daily": daily, "aggregate": agg}))
        wu = str(uuid.uuid5(NAMESPACE, wh))
        partial = len(days) < WEEK_DAYS
        con.execute("INSERT OR IGNORE INTO quarantine_state_weekly (weekly_uuid, end_day, n_days, partial, "
                    "daily_uuids_json, aggregate_json, dips_json, k, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (wu, end.isoformat(), len(days), int(partial), json.dumps(daily), canonical(agg).decode(),
                     canonical(dip).decode(), float(k), now.isoformat(timespec="seconds")))
        con.commit()
    finally:
        con.close()
    return {"weekly_uuid": wu, "n_days": len(days), "partial": partial, "daily_uuids": daily, "dips": dip}


def zoom(weekly_uuid: str, db: Optional[Path] = None) -> dict:
    con = connect(db)
    try:
        r = con.execute("SELECT daily_uuids_json, aggregate_json, dips_json, k FROM quarantine_state_weekly "
                        "WHERE weekly_uuid = ?", (weekly_uuid,)).fetchone()
        if r is None:
            return {"weekly_uuid": weekly_uuid, "found": False, "points": []}
        daily, agg, dip, k = json.loads(r[0]), json.loads(r[1]), json.loads(r[2]), r[3]
        days = agg["days"]
        points = []
        for day in sorted(dip):
            i = days.index(day)
            du = daily[i]
            row = con.execute("SELECT aggregate_json FROM quarantine_state_snapshots WHERE daily_uuid = ?",
                              (du,)).fetchone() if du else None
            nodes = []
            for key in dip[day]:
                s = agg["series"][key]
                med = statistics.median(s)
                nodes.append({"metric": key, "value": s[i], "median": med, "delta": s[i] - med})
            points.append({"day": day, "daily_uuid": du, "nodes": nodes,
                           "aggregate": json.loads(row[0]) if row else None})
        return {"weekly_uuid": weekly_uuid, "found": True, "k": k, "points": points}
    finally:
        con.close()


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def cycle(root: Path, pattern: str = "knowledge_*", weekly_on: Optional[str] = "Sun", db: Optional[Path] = None,
          dry_run: bool = False, now: Optional[dt.datetime] = None, log: Optional[Path] = None) -> dict:
    """Один проход расписания: snapshot -> purge -> (weekly в день weekly_on). Возвращает {dirs: [...], summary}."""
    now = now or dt.datetime.now()
    if weekly_on is not None and weekly_on not in WEEKDAYS:
        raise ValueError(f"weekly_on: одно из {','.join(WEEKDAYS)}")
    root = Path(root)
    dirs = sorted(p for p in root.glob(pattern) if p.is_dir()) if root.is_dir() else []
    res = []
    for d in dirs:
        s = snapshot(d, db, now=now)
        pr = purge(d, db, dry_run=dry_run, now=now)
        res.append({"dir_name_sha16": sha256_hex(d.name.encode())[:16], "daily_uuid": s["daily_uuid"],
                    "snapshot_new": s["new"], "files": s["aggregate"]["files"], "purge_deleted": pr["deleted"],
                    "purge_reason": pr["reason"], "age_h": pr.get("age_h")})
    w = None
    if weekly_on is not None and WEEKDAYS[now.weekday()] == weekly_on:
        w = weekly(db, now.date().isoformat(), now=now)
    summary = {"at": now.isoformat(timespec="seconds"), "dirs": len(res), "snapshots_new": sum(r["snapshot_new"] for r in res),
               "purged": sum(r["purge_deleted"] for r in res), "dry_run": dry_run,
               "weekly_uuid": w["weekly_uuid"] if w else None, "weekly_partial": w["partial"] if w else None,
               "weekly_dips": len(w["dips"]) if w else None}
    if log is not None:
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            for r in res:
                f.write(json.dumps(dict(r, at=summary["at"], kind="dir"), ensure_ascii=False, sort_keys=True) + "\n")
            f.write(json.dumps(dict(summary, kind="cycle"), ensure_ascii=False, sort_keys=True) + "\n")
    return {"dirs": res, "summary": summary}
