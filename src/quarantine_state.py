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
Схема run_metrics v2 (B29): счётчики допускают NULL (значение не выведено из машинных строк -- не догадка), столбцы
source (block -- запись самим блоком при закрытии; backfill* -- дозапись), report_sha16, outcome (GREEN / RED / ASK /
ROLLBACK / STOP / MANUAL / NOT_STARTED; у incident -- FAIL / CAUGHT / WARN / WAIT / FOUND), kind (block_run |
incident), boundary (стык), day, time_text (время как в источнике), fail_stage и fail_code (NULL у GREEN), attempt (номер попытки задача+блок за сутки -- по
чтению таблицы). Каждый запуск -- своя строка: run_id блока = uuid5(task, block, started_at, соль запуска); строки
не перезаписываются (INSERT OR IGNORE; та же run_id с другими полями -- расхождение при чтении назад). Старая база (v1) переводится
только явно: migrate_run_metrics() -- копия базы, затем перенос в одной транзакции, старые строки -> source
backfill_text_b15. read_run() -- чтение строки назад новым соединением (закрытие блока, src/block_finish.py).
weekly() читает и серию искажений distortions_YYYY-MM-DD.json (каталог dist_dir, по умолчанию -- каталог базы):
ряды dist.<стык> (by_boundary) и dist_kind.<вид> (series).
Поправка B29 (видеть паттерны сбоев): точка ряда None = «нет данных»
(суток нет в источнике ряда или счётчик дня NULL) -- не участвует в медиане / MAD; меньше 3 известных точек -- ряд
insufficient, точки не ищутся. Направление ряда -- таблица DIRECTIONS (одно место): «плохо = рост» -> всплески вверх
(x > median + k * scale), «плохо = падение» -> провалы вниз (x < median - k * scale); ряд без направления в поиск не
идёт и попадает в undirected (тест требует, чтобы таких не было).
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
RUN_SCHEMA = 2
RUN_SOURCES = ("block", "backfill", "backfill_text_b15")
RETRO = "retro_audit"
OUTCOMES = {"GREEN": 1, "RED": 2, "ROLLBACK": 2, "STOP": 2, "NOT_STARTED": 2, "ASK": 3, "MANUAL": 5}
# NOT_STARTED у строки блока: обёртка запустилась, но UAC истёк / отменён (сигнал 2); у ретро-строки -- сигнала нет
WAITS = ("launch_delay_s", "uac_wait_s", "hitl_wait_s")      # секунды; NULL = не измерялось, 0 = ожидания не было
INCIDENT_OUTCOMES = ("FAIL", "CAUGHT", "WARN", "WAIT", "FOUND")
_TASKCODE_RE = re.compile(r"^[A-Za-z0-9_?]{1,16}$")                    # incident: источник (метка) или код с «?»
_BOUNDARY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_EXACT_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
BLOCK_ALIASES = {"promote": "push_promote", "stage": "push_stage"}
SIGNAL_OUTCOME = {1: "GREEN", 2: "RED", 3: "ASK", 5: "MANUAL"}
_STAGE_RE = re.compile(r"^[A-Za-z0-9_./-]{1,24}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_SALT_RE = re.compile(r"^[0-9a-f]{8,64}$")
_SHA16_RE = re.compile(r"^[0-9a-f]{16}$")
_DIST_RE = re.compile(r"^distortions_(\d{4}-\d{2}-\d{2})\.json$")
_DIST_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RUN_DDL = """CREATE TABLE IF NOT EXISTS run_metrics (
        run_id TEXT PRIMARY KEY, task TEXT NOT NULL, block TEXT NOT NULL, started_at TEXT,
        ended_at TEXT, duration_s REAL, steps INTEGER, rollbacks INTEGER,
        uac INTEGER, hitl INTEGER, parse_errors INTEGER, net_errors INTEGER,
        flaky INTEGER, signal INTEGER, recorded_at TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'block', report_sha16 TEXT, outcome TEXT NOT NULL,
        fail_stage TEXT, fail_code TEXT, attempt INTEGER,
        kind TEXT NOT NULL DEFAULT 'block_run', boundary TEXT, day TEXT NOT NULL, time_text TEXT,
        launch_ts TEXT, launch_delay_s REAL, uac_wait_s REAL, hitl_wait_s REAL, retro_conflict INTEGER)"""
# started_at / ended_at / duration_s / signal / attempt = NULL только у строк ретро-разбора (время из источника
# неточное, сигнала нет); строки блока (block) и дозаписи (backfill) -- всегда со временем и сигналом
RUN_COLS = ("run_id", "task", "block", "started_at", "ended_at", "duration_s") + RUN_COUNTS + (
    "signal", "recorded_at", "source", "report_sha16", "outcome", "fail_stage", "fail_code", "attempt",
    "kind", "boundary", "day", "time_text", "launch_ts") + WAITS + ("retro_conflict",)


class SchemaError(RuntimeError):
    """run_metrics старой схемы (v1): запись новых строк -- только после migrate_run_metrics()."""
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
    con.execute(RUN_DDL)                      # новая база -- сразу v2; существующая таблица не меняется (только migrate)
    return con


def run_schema(con: sqlite3.Connection) -> int:
    """Версия схемы run_metrics: 0 -- таблицы нет, 1 -- B15 (без source), 2 -- B29."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(run_metrics)")]
    if not cols:
        return 0
    return RUN_SCHEMA if {"source", "report_sha16", "outcome", "attempt", "kind", "boundary", "day", "launch_ts",
                          "uac_wait_s", "hitl_wait_s", "retro_conflict"} <= set(cols) else 1


def migrate_run_metrics(db: Optional[Path] = None, backup_dir: Optional[Path] = None,
                        now: Optional[dt.datetime] = None) -> dict:
    """v1 -> v2. Сначала копия всей базы в backup_dir (обязателен для v1; копия сверяется по числу строк),
    затем в одной транзакции: новая таблица, перенос строк (source = backfill_text_b15, report_sha16 = NULL),
    замена старой. Повтор на v2 -- ничего не делает (migrated=False). Числа строк до/после -- в результате."""
    now = now or dt.datetime.now()
    db = Path(db or default_db())
    con = connect(db)
    try:
        ver = run_schema(con)
        n0 = con.execute("SELECT count(*) FROM run_metrics").fetchone()[0]
        if ver == RUN_SCHEMA:
            return {"migrated": False, "schema": ver, "rows": n0, "backup": None}
        if backup_dir is None:
            raise ValueError("backup_dir: обязателен при переводе схемы v1")
        bdir = Path(backup_dir)
        bdir.mkdir(parents=True, exist_ok=True)
        bpath = bdir / f"{db.stem}_v1_{now.strftime('%Y%m%d_%H%M%S')}.db"
        if bpath.exists():
            raise FileExistsError(f"копия уже есть: {bpath.name}")
        bcon = sqlite3.connect(str(bpath))
        try:
            con.backup(bcon)
            nb = bcon.execute("SELECT count(*) FROM run_metrics").fetchone()[0]
        finally:
            bcon.close()
        if nb != n0:
            raise RuntimeError(f"копия базы: строк {nb}, в базе {n0}")
        old = ", ".join(("run_id", "task", "block", "started_at", "ended_at", "duration_s") + RUN_COUNTS
                        + ("signal", "recorded_at"))
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(RUN_DDL.replace("IF NOT EXISTS run_metrics", "run_metrics_v2"))
            con.execute(f"INSERT INTO run_metrics_v2 ({old}, source, report_sha16, outcome, fail_stage, fail_code, attempt, "
                        f"kind, boundary, day, time_text, launch_ts, launch_delay_s, uac_wait_s, hitl_wait_s, retro_conflict) "
                        f"SELECT {old}, 'backfill_text_b15', NULL, CASE signal WHEN 1 THEN 'GREEN' WHEN 3 THEN 'ASK' "
                        f"WHEN 5 THEN 'MANUAL' ELSE 'RED' END, NULL, NULL, (SELECT count(*) FROM run_metrics o WHERE "
                        f"o.task = r.task AND o.block = r.block AND substr(o.started_at, 1, 10) = substr(r.started_at, 1, 10) "
                        f"AND o.started_at < r.started_at) + 1, 'block_run', NULL, substr(r.started_at, 1, 10), NULL, NULL, NULL, NULL, NULL, NULL "
                        f"FROM run_metrics r")
            n1 = con.execute("SELECT count(*) FROM run_metrics_v2").fetchone()[0]
            if n1 != n0:
                raise RuntimeError(f"перенос: строк {n1}, было {n0}")
            con.execute("DROP TABLE run_metrics")
            con.execute("ALTER TABLE run_metrics_v2 RENAME TO run_metrics")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return {"migrated": True, "schema": run_schema(con), "rows": n0, "backup": bpath.name, "backup_rows": nb}
    finally:
        con.close()


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


# направление «плохо» по ряду -- ЕДИНСТВЕННОЕ место; порядок не важен (шаблоны не пересекаются)
DIRECTIONS = (
    (re.compile(r"^(files|bytes)$|^cat\.[a-z_]+\.(files|bytes)$"), "down"),          # спад карантина (B12)
    (re.compile(r"^run\.(count|green|green_pct|clean_pct)$"), "down"),
    (re.compile(r"^run\.(duration_s|steps|rollbacks|uac|hitl|parse_errors|net_errors|flaky|red|rollback|stop|ask"
                r"|manual|not_started|incident|launch_delay_s|uac_wait_s|hitl_wait_s)$"), "up"),
    (re.compile(r"^(dist|dist_kind|run_bnd)\.[a-z][a-z0-9_]{0,31}$"), "up"),          # искажения и стыки
)


def direction(key: str) -> Optional[str]:
    """'up' (плохо = рост), 'down' (плохо = падение) или None (ряд без направления)."""
    for rx, d in DIRECTIONS:
        if rx.match(key):
            return d
    return None


def anomalies(series: list, dirn: str, k: float = DEFAULT_K) -> tuple:
    """(индексы точек, число известных). None пропускается; < 3 известных -- ([], n). Масштаб -- как в dips()."""
    known = [x for x in series if x is not None]
    if len(known) < 3 or dirn not in ("up", "down"):
        return [], len(known)
    med = statistics.median(known)
    mad = statistics.median(abs(x - med) for x in known)
    scale = mad if mad > 0 else max(1.0, 0.05 * abs(med))
    if dirn == "down":
        return [i for i, x in enumerate(series) if x is not None and x < med - k * scale], len(known)
    return [i for i, x in enumerate(series) if x is not None and x > med + k * scale], len(known)


def run_id(task: str, block: str, started_at: str, salt: Optional[str] = None) -> str:
    """Без соли -- как в B15 (одна запись на задачу+блок+время старта); с солью -- своя строка на каждый запуск."""
    if salt is None:
        return str(uuid.uuid5(NAMESPACE, sha256_hex(canonical({"run": [task, block, started_at]}))))
    return str(uuid.uuid5(NAMESPACE, sha256_hex(canonical({"run": [task, block, started_at], "salt": salt}))))


def validate_outcome(outcome, signal: int, fail_stage, fail_code) -> tuple:
    """outcome согласован с signal; у GREEN fail_stage / fail_code = None; коды -- машинные, без текста."""
    outcome = SIGNAL_OUTCOME[signal] if outcome is None else str(outcome)
    if outcome not in OUTCOMES or OUTCOMES[outcome] != signal:
        raise ValueError(f"outcome: {outcome} не согласован с signal {signal}")
    if outcome == "GREEN" and (fail_stage is not None or fail_code is not None):
        raise ValueError("outcome GREEN: fail_stage / fail_code -- NULL")
    if fail_stage is not None and not _STAGE_RE.match(str(fail_stage)):
        raise ValueError("fail_stage: [A-Za-z0-9_./-] до 24 знаков")
    if fail_code is not None and not _CODE_RE.match(str(fail_code)):
        raise ValueError("fail_code: код [a-z][a-z0-9_] до 32 знаков")
    return outcome, fail_stage, fail_code


def _norm_time(v, name: str) -> str:
    try:
        t = dt.datetime.fromisoformat(str(v))
    except ValueError as e:
        raise ValueError(f"{name}: время ISO 8601") from e
    return t.replace(tzinfo=None, microsecond=0).isoformat()


def validate_run(rec: dict, allow_null: bool = False) -> tuple:
    """Проверка записи прогона -> (task, block, started_at, ended_at, duration_s, {счётчики}, signal); иначе ValueError.
    allow_null (только дозапись): счётчик None = не выведен из машинных строк; иначе каждый счётчик -- целое >= 0."""
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
        if v is None and allow_null:
            vals[k] = None
            continue
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{k}: целое >= 0")
        vals[k] = v
    sig = rec.get("signal")
    if isinstance(sig, bool) or sig not in RUN_SIGNALS:
        raise ValueError(f"signal: одно из {RUN_SIGNALS}")
    return task, block, st, en, dur, vals, sig


def record_run(rec: dict, db: Optional[Path] = None, now: Optional[dt.datetime] = None, source: str = "block",
               report_sha16: Optional[str] = None, outcome: Optional[str] = None, fail_stage: Optional[str] = None,
               fail_code: Optional[str] = None, salt: Optional[str] = None, launch_ts: Optional[str] = None,
               waits: Optional[dict] = None) -> dict:
    """Одна строка run_metrics (только числа + коды задачи/блока). run_id = uuid5(task, block, started_at):
    повтор той же записи -- та же строка (new=False). Неверное поле -> ValueError, ничего не пишется.
    source: block (счётчик None -- только при исходе не GREEN: упал до сбора) или backfill* (None допустим).
    outcome по умолчанию -- из signal; attempt = 1 + число строк той же задачи+блока за те же сутки, начатых раньше.
    salt (соль запуска) -> своя run_id на каждый запуск. База v1 -> SchemaError."""
    now = now or dt.datetime.now()
    if source not in RUN_SOURCES:
        raise ValueError(f"source: одно из {RUN_SOURCES}")
    if report_sha16 is not None and not _SHA16_RE.match(str(report_sha16)):
        raise ValueError("report_sha16: 16 строчных шестнадцатеричных знаков")
    if salt is not None and not _SALT_RE.match(str(salt)):
        raise ValueError("salt: 8..64 строчных шестнадцатеричных знаков")
    task, block, st, en, dur, vals, sig = validate_run(rec, allow_null=True)
    outcome, fail_stage, fail_code = validate_outcome(outcome, sig, fail_stage, fail_code)
    if source == "block" and outcome == "GREEN" and any(vals[k] is None for k in RUN_COUNTS):
        raise ValueError("outcome GREEN: все счётчики -- целые (NULL только у незавершённого запуска)")
    wv = {}
    for k in WAITS:
        v = (waits or {}).get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v != v):
            raise ValueError(f"{k}: число >= 0 или NULL")
        wv[k] = None if v is None else round(float(v), 3)
    lt = None if launch_ts is None else _norm_time(launch_ts, "launch_ts")
    rid = run_id(task, block, st, salt)
    con = connect(db)
    try:
        if run_schema(con) != RUN_SCHEMA:
            raise SchemaError("run_metrics: схема v1 -- сначала migrate")
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute("SELECT attempt FROM run_metrics WHERE run_id = ?", (rid,)).fetchone()
            if row is not None:
                att, new = row[0], False                     # строка этого запуска уже есть -- не перезаписывается
            else:
                att = 1 + con.execute("SELECT count(*) FROM run_metrics WHERE task = ? AND block = ? AND "
                                      "day = ? AND started_at <= ? AND signal IS NOT NULL",   # <=: два запуска в одну секунду
                                      (task, block, st[:10], st)).fetchone()[0]
                con.execute("INSERT INTO run_metrics (" + ", ".join(RUN_COLS) + ") VALUES ("
                            + ", ".join("?" * len(RUN_COLS)) + ")",
                            (rid, task, block, st, en, dur, *[vals[k] for k in RUN_COUNTS], sig,
                             now.isoformat(timespec="seconds"), source, report_sha16, outcome, fail_stage, fail_code,
                             att, "block_run", None, st[:10], None, lt, *[wv[k] for k in WAITS], None))
                new = True
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return {"run_id": rid, "new": new, "task": task, "block": block, "duration_s": dur, "signal": sig,
            "outcome": outcome, "attempt": att}


def read_run(rid: str, db: Optional[Path] = None) -> Optional[dict]:
    """Строка run_metrics по run_id -- новым соединением (без создания таблиц и без записи); None -- нет строки."""
    db = Path(db or default_db())
    if not db.is_file():
        return None
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if run_schema(con) != RUN_SCHEMA:
            return None
        row = con.execute("SELECT " + ", ".join(RUN_COLS) + " FROM run_metrics WHERE run_id = ?", (rid,)).fetchone()
    finally:
        con.close()
    return dict(zip(RUN_COLS, row)) if row else None


def _run_series(con: sqlite3.Connection, start: dt.date, end: dt.date) -> dict:
    """{day: {run.<метрика>: число | None}} по прогонам, начатым в окне. run.green_pct -- доля прогонов с сигналом 1,
    run.clean_pct -- доля прогонов без откатов, FLAKY, ошибок разбора и сети (0..100); run.green / red / rollback /
    stop / ask / manual -- число прогонов по исходу. Счётчик суток = None, если хоть у одной строки суток он NULL
    (частичная сумма -- не число суток); clean_pct -- по строкам с известными четырьмя полями, нет таких -- None."""
    oc = "outcome" if run_schema(con) == RUN_SCHEMA else (
        "CASE signal WHEN 1 THEN 'GREEN' WHEN 3 THEN 'ASK' WHEN 5 THEN 'MANUAL' ELSE 'RED' END")
    v2 = run_schema(con) == RUN_SCHEMA
    dcol = "day" if v2 else "substr(started_at, 1, 10)"
    flt = " AND kind = 'block_run' AND signal IS NOT NULL" if v2 else ""
    wsel = ", ".join(WAITS) if v2 else ", ".join("NULL" for _ in WAITS)
    rows = con.execute(f"SELECT {dcol}, duration_s, " + ", ".join(RUN_COUNTS) + f", {wsel}, signal, {oc} "
                       f"FROM run_metrics WHERE {dcol} >= ? AND {dcol} <= ?{flt}",
                       (start.isoformat(), end.isoformat())).fetchall()
    by = {}
    extra = {}
    measured = set()                                     # ряды ожиданий -- только если в окне есть хоть одно измерение
    if v2:   # не запущенные блоки, инциденты и стыки (ряды стыков -- по всем строкам)
        for day, kind, outc, bnd in con.execute("SELECT day, kind, outcome, boundary FROM run_metrics WHERE day >= ? "
                                                "AND day <= ?", (start.isoformat(), end.isoformat())):
            e = extra.setdefault(day, {})                            # только встреченные ключи: без таких строк
            if kind == "block_run" and outc == "NOT_STARTED":            # недельный слепок прежний
                e["run.not_started"] = e.get("run.not_started", 0.0) + 1
            if kind == "incident":
                e["run.incident"] = e.get("run.incident", 0.0) + 1
            if bnd:
                e[f"run_bnd.{bnd}"] = e.get(f"run_bnd.{bnd}", 0.0) + 1
    for r in rows:
        m = by.setdefault(r[0], {"run.count": 0.0, "run.duration_s": 0.0, **{f"run.{k}": 0.0 for k in RUN_COUNTS},
                                 **{f"run.{o}": 0.0 for o in ("green", "red", "rollback", "stop", "ask", "manual")},
                                 "_g": 0.0, "_c": 0.0, "_ck": 0.0})
        m["run.count"] += 1
        if m["run.duration_s"] is None or r[1] is None:          # ретро-строка без времени -> длительность суток
            m["run.duration_s"] = None                             # неизвестна (нет данных), не 0
        else:
            m["run.duration_s"] += float(r[1])
        d = dict(zip(RUN_COUNTS, r[2:2 + len(RUN_COUNTS)]))
        wd = dict(zip(WAITS, r[2 + len(RUN_COUNTS):2 + len(RUN_COUNTS) + len(WAITS)]))
        if v2:
            for k in WAITS:                                     # сумма ожиданий суток; NULL у строки -> нет данных
                key = f"run.{k}"
                if key not in m:
                    m[key] = 0.0
                m[key] = None if (m[key] is None or wd[k] is None) else m[key] + float(wd[k])
                if wd[k] is not None:
                    measured.add(key)
        for k in RUN_COUNTS:
            if m[f"run.{k}"] is None or d[k] is None:
                m[f"run.{k}"] = None
            else:
                m[f"run.{k}"] += float(d[k])
        m["_g"] += 1.0 if r[-2] == 1 else 0.0
        m[f"run.{str(r[-1]).lower()}"] = m.get(f"run.{str(r[-1]).lower()}", 0.0) + 1.0
        four = [d[k] for k in ("rollbacks", "flaky", "parse_errors", "net_errors")]
        if all(v is not None for v in four):
            m["_ck"] += 1.0
            m["_c"] += 1.0 if not any(four) else 0.0
    for m in by.values():
        for key in [f"run.{k}" for k in WAITS]:
            if key not in measured:
                m.pop(key, None)                         # не измерялось нигде в окне -- ряда нет (слепок прежний)
        g, c, ck = m.pop("_g"), m.pop("_c"), m.pop("_ck")
        m["run.green_pct"] = round(100.0 * g / m["run.count"], 3)
        m["run.clean_pct"] = round(100.0 * c / ck, 3) if ck else None
    runkeys = ["run.count", "run.duration_s", "run.green_pct", "run.clean_pct"] + [f"run.{k}" for k in RUN_COUNTS + WAITS] + [
        f"run.{o}" for o in ("green", "red", "rollback", "stop", "ask", "manual")]
    for day, e in extra.items():
        if e:
            by.setdefault(day, {k: None for k in runkeys if not k.endswith("_s") or k == "run.duration_s"
                                or k in measured}).update(e)   # сутки без запусков: ряды блоков -- нет данных
    return by


def _dist_series(dist_dir: Optional[Path], start: dt.date, end: dt.date) -> tuple:
    """({day: {dist.<стык>: число}}, пропущено файлов) из distortions_YYYY-MM-DD.json окна (поле by_boundary)."""
    by, bad = {}, 0
    if dist_dir is None or not Path(dist_dir).is_dir():
        return by, bad
    for p in sorted(Path(dist_dir).iterdir()):
        m = _DIST_RE.match(p.name)
        if not m or not p.is_file():
            continue
        try:
            day = dt.date.fromisoformat(m.group(1))
        except ValueError:
            bad += 1
            continue
        if not start <= day <= end:
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            bb = obj.get("by_boundary") if isinstance(obj, dict) else None
            if obj.get("day") != day.isoformat() or not isinstance(bb, dict):
                raise ValueError("day / by_boundary")
            sr = obj.get("series", {})
            if not isinstance(sr, dict):
                raise ValueError("series")
            row = {}
            for pre, src in (("dist", bb), ("dist_kind", sr)):
                for key, v in src.items():
                    if not _DIST_KEY_RE.match(str(key)) or isinstance(v, bool) or not isinstance(v, int) or v < 0:
                        raise ValueError("by_boundary / series: код [a-z0-9_] -> целое >= 0")
                    row[f"{pre}.{key}"] = float(v)
        except (ValueError, OSError, AttributeError):
            bad += 1
            continue
        by[day.isoformat()] = row
    return by, bad


def weekly(db: Optional[Path] = None, end_day: Optional[str] = None, k: float = DEFAULT_K,
           now: Optional[dt.datetime] = None, dist_dir: Optional[Path] = None) -> dict:
    now = now or dt.datetime.now()
    end = dt.date.fromisoformat(end_day) if end_day else now.date()
    start = end - dt.timedelta(days=WEEK_DAYS - 1)
    dist_dir = Path(dist_dir) if dist_dir is not None else Path(db or default_db()).parent
    con = connect(db)
    try:
        rows = con.execute("SELECT daily_uuid, day, aggregate_json, created_at FROM quarantine_state_snapshots "
                           "WHERE day >= ? AND day <= ? ORDER BY day, created_at", (start.isoformat(), end.isoformat())).fetchall()
        last = {}
        for du, day, aj, ca in rows:
            last[day] = (du, json.loads(aj))          # последний слепок суток
        runs = _run_series(con, start, end)
        dist, dist_bad = _dist_series(dist_dir, start, end)
        days = sorted(set(last) | set(runs) | set(dist))
        daily = [last[d][0] if d in last else None for d in days]
        quar = [_metrics(last[d][1]) if d in last else None for d in days]
        rser = [runs.get(d) for d in days]
        dser = [dist.get(d) for d in days]
        keys = sorted({k_ for grp in (quar, rser, dser) for m in grp if m for k_ in m})

        def val(key: str, i: int):
            # источник ряда: сутки есть в источнике -> отсутствующий ключ = 0 (категории нет / стык не встречен);
            # суток нет в источнике -> None (нет данных)
            grp = rser if key.startswith(("run.", "run_bnd.")) else dser if key.startswith("dist") else quar
            m = grp[i]
            return None if m is None else m.get(key, 0.0)

        series = {key: [val(key, i) for i in range(len(days))] for key in keys}
        dip, known, insufficient, undirected = {}, {}, [], []
        for key, s in series.items():
            dn = direction(key)
            if dn is None:
                undirected.append(key)
                known[key] = sum(1 for x in s if x is not None)
                continue
            idx, known[key] = anomalies(s, dn, k)
            if known[key] < 3:
                insufficient.append(key)
            for i in idx:
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
    return {"weekly_uuid": wu, "n_days": len(days), "partial": partial, "daily_uuids": daily, "dips": dip,
            "dist_days": len(dist), "dist_skipped": dist_bad, "known": known, "insufficient": insufficient,
            "undirected": undirected}


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
                med = statistics.median([x for x in s if x is not None])
                nodes.append({"metric": key, "value": s[i], "median": med, "delta": s[i] - med,
                              "direction": direction(key)})
            points.append({"day": day, "daily_uuid": du, "nodes": nodes,
                           "aggregate": json.loads(row[0]) if row else None})
        return {"weekly_uuid": weekly_uuid, "found": True, "k": k, "points": points}
    finally:
        con.close()


def _retro_row(ev: dict, i: int) -> dict:
    """Событие ретро-разбора -> поля строки; ValueError с причиной. Ничего не выдумывается: неточное время ->
    started_at NULL (время -- как есть в time_text); нет attempt -> NULL; текст note / source_ref не хранится
    (в report_sha16 -- sha16 канонического события, след к исходному файлу)."""
    if not isinstance(ev, dict):
        raise ValueError("событие -- объект")
    if ev.get("source") != RETRO:
        raise ValueError("source: retro_audit")
    day = dt.date.fromisoformat(str(ev.get("day"))).isoformat()
    tt = str(ev.get("time") or "").strip()
    if not tt or len(tt) > 16 or "\n" in tt:
        raise ValueError("time: строка до 16 знаков")
    task = str(ev.get("block") or "")                       # в событии block = код задачи / источник (B18a, X_SRC)
    blk = str(ev.get("task") or "")                         # task = имя скрипта / действия (run, elevated, write)
    blk = BLOCK_ALIASES.get(blk, blk)
    kind = "block_run" if _TASK_RE.match(task) else "incident"
    if kind == "incident" and not _TASKCODE_RE.match(task):
        raise ValueError("block: код источника [A-Za-z0-9_?] до 16")
    if not _BLOCK_RE.match(blk):
        raise ValueError("task: [a-z][a-z0-9_] до 24")
    oc = str(ev.get("outcome") or "")
    allowed = set(OUTCOMES) if kind == "block_run" else set(INCIDENT_OUTCOMES) | {"NOT_STARTED"}
    if oc not in allowed:
        raise ValueError(f"outcome {oc} не для {kind}")
    if oc == "GREEN":
        raise ValueError("outcome GREEN в ретро-разборе сбоев не ожидается")
    fc, bnd, att = ev.get("fail_code"), ev.get("boundary"), ev.get("attempt")
    if fc is not None and not _CODE_RE.match(str(fc)):
        raise ValueError("fail_code: код [a-z][a-z0-9_]")
    if bnd is not None and not _BOUNDARY_RE.match(str(bnd)):
        raise ValueError("boundary: код [a-z][a-z0-9_]")
    if att is not None and (isinstance(att, bool) or not isinstance(att, int) or att < 1):
        raise ValueError("attempt: целое >= 1 или null")
    m = _EXACT_TIME_RE.match(tt)
    st = f"{day}T{m.group(1)}:{m.group(2)}:00" if m else None
    sig = OUTCOMES.get(oc) if kind == "block_run" and oc != "NOT_STARTED" else None
    canon = {k: ev.get(k) for k in ("day", "time", "block", "task", "outcome", "fail_code", "boundary", "attempt",
                                    "source_ref")}
    h = sha256_hex(canonical(canon))
    return {"run_id": str(uuid.uuid5(NAMESPACE, sha256_hex(canonical({"retro": h})))), "task": task, "block": blk,
            "kind": kind, "day": day, "time_text": tt, "started_at": st, "outcome": oc, "signal": sig,
            "fail_code": fc, "boundary": bnd, "attempt": att, "report_sha16": h[:16], "index": i}


def _time_fits(tt: str, st: Optional[str], en: Optional[str]) -> bool:
    """Точное HH:MM -- внутри [start - 1 мин, end + 1 мин] запуска; HH:Mx -- тот же час и десяток минут;
    ~HH:xx -- тот же час; прочее («утро») -- время не сверяется."""
    if not st:
        return False
    m = _EXACT_TIME_RE.match(tt)
    t0 = dt.datetime.fromisoformat(st)
    if m:
        x = t0.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
        t1 = dt.datetime.fromisoformat(en) if en else t0
        return t0 - dt.timedelta(minutes=1) <= x <= t1 + dt.timedelta(minutes=1)
    m = re.match(r"^~?([01][0-9]|2[0-3]):([0-5])x$", tt)
    if m:
        return t0.hour == int(m.group(1)) and t0.minute // 10 == int(m.group(2))
    m = re.match(r"^~?([01][0-9]|2[0-3]):xx$", tt)
    if m:
        return t0.hour == int(m.group(1))
    return True


def import_retro(events: list, db: Optional[Path] = None, now: Optional[dt.datetime] = None) -> dict:
    """Ретро-разбор -> run_metrics. Событие запуска блока (kind block_run), совпавшее с уже записанной строкой
    дозаписи (тот же день + код задачи + блок + attempt, время запуска подходит, строка не GREEN), не дублируется:
    у неё дополняются fail_code / fail_stage / boundary, где NULL, source -> backfill+retro_audit; исход строки не
    меняется (машинная строка отчёта первична; расхождение -> conflicts и retro_conflict = 1 у строки). Иначе -- новая строка (source retro_audit).
    Повтор импорта ничего не дублирует. -> {inserted, matched, rejected: [{index, reason}], conflicts, total}."""
    now = now or dt.datetime.now()
    rows, rejected = [], []
    for i, ev in enumerate(events):
        try:
            rows.append(_retro_row(ev, i))
        except (ValueError, TypeError) as e:
            rejected.append({"index": i, "reason": str(e)})
    con = connect(db)
    ins = mat = new = 0
    conflicts = []
    try:
        if run_schema(con) != RUN_SCHEMA:
            raise SchemaError("run_metrics: схема v1 -- сначала migrate")
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            for r in rows:
                if con.execute("SELECT 1 FROM run_metrics WHERE run_id = ?", (r["run_id"],)).fetchone():
                    ins += 1                                        # уже вставлено прошлым импортом
                    continue
                hit = None
                if r["kind"] == "block_run" and r["attempt"] is not None:
                    for row in con.execute("SELECT run_id, started_at, ended_at, outcome, source, fail_code, fail_stage, "
                                           "boundary FROM run_metrics WHERE kind = 'block_run' AND day = ? AND task = ? "
                                           "AND block = ? AND attempt = ? AND source LIKE 'backfill%' AND signal IS NOT NULL",
                                           (r["day"], r["task"], r["block"], r["attempt"])):
                        if row[3] != "GREEN" and _time_fits(r["time_text"], row[1], row[2]):
                            hit = row
                            break
                if hit:
                    con.execute("UPDATE run_metrics SET fail_code = COALESCE(fail_code, ?), fail_stage = COALESCE(fail_stage, ?), "
                                "boundary = COALESCE(boundary, ?), time_text = COALESCE(time_text, ?), "
                                "source = 'backfill+retro_audit' WHERE run_id = ?",
                                (r["fail_code"], None, r["boundary"], r["time_text"], hit[0]))
                    mat += 1
                    if hit[3] != r["outcome"]:          # исход отчёта блока первичен; расхождение видно в данных
                        con.execute("UPDATE run_metrics SET retro_conflict = 1 WHERE run_id = ?", (hit[0],))
                        conflicts.append({"index": r["index"], "row_outcome": hit[3], "retro_outcome": r["outcome"]})
                    continue
                con.execute("INSERT INTO run_metrics (" + ", ".join(RUN_COLS) + ") VALUES (" + ", ".join("?" * len(RUN_COLS))
                            + ")", (r["run_id"], r["task"], r["block"], r["started_at"], None, None,
                                    *[None] * len(RUN_COUNTS), r["signal"], now.isoformat(timespec="seconds"), RETRO,
                                    r["report_sha16"], r["outcome"], None, r["fail_code"], r["attempt"], r["kind"],
                                    r["boundary"], r["day"], r["time_text"], None, None, None, None, None))
                ins += 1
                new += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return {"total": len(events), "inserted": ins, "new_rows": new, "matched": mat, "rejected": rejected,
            "conflicts": conflicts}


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
