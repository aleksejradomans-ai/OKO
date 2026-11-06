"""oko_selfcheck.py — общий тест контура Ока: только ЧТЕНИЕ, каждый пункт PASS / WARN / FAIL (30.10.2026).

Запуск: python C:\\oko\\tools\\oko_selfcheck.py   (python службы; из любой папки)
Ничего не пишет и не нажимает: ТВ — только GET/чтение настроек, SmartThings — /devices + /status,
Garmin и OAuth — чтение файлов/базы. Код возврата: 0 — нет FAIL, 1 — есть FAIL.
Личное не выводится: id, токены, серийные номера маскируются или не печатаются.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\oko")
sys.path.insert(0, str(ROOT / "src"))
LOG = ROOT / "logs" / "OkoBot.stderr.log"
RES: list[tuple[str, str, str]] = []


def rec(status: str, name: str, info: str = "") -> None:
    RES.append((status, name, info))
    print(f"{status:<4} {name}" + (f" — {info}" if info else ""), flush=True)


def check(name):
    def deco(fn):
        def run():
            try:
                fn()
            except Exception as e:                      # пункт изолирован: остальные идут дальше
                rec("FAIL", name, f"{type(e).__name__}: {str(e)[:160]}")
        run.__name__ = fn.__name__
        CHECKS.append(run)
        return run
    return deco


CHECKS: list = []


def _log_tail(n: int = 30000) -> list[str]:
    with LOG.open("r", encoding="utf-8", errors="replace") as f:
        return f.readlines()[-n:]


def _ts(line: str) -> dt.datetime | None:
    m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
    return dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None


def _last(lines, needle):
    for l in reversed(lines):
        if needle in l:
            return l
    return None


@check("git: HEAD и чистота дерева")
def c_git():
    head = subprocess.run(["git", "-C", str(ROOT), "log", "--oneline", "-1"], capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()
    dirty = [l for l in subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True,
                                       text=True, encoding="utf-8").stdout.splitlines() if l.strip()]
    if not head:
        rec("FAIL", "git", "git log не ответил"); return
    rec("PASS" if not dirty else "WARN", "git", f"{head[:60]} | изменённых/новых: {len(dirty)}"
        + (f" ({', '.join(d[3:] for d in dirty[:4])})" if dirty else ""))


@check("служба: процесс работает")
def c_service():
    # 30.10: тихий журнал — НЕ признак смерти (успешные опросы молчат: 10 мин без записей при живой службе).
    # Состояние берётся у диспетчера служб; возраст журнала — только справка.
    out = subprocess.run(["sc", "query", "OkoBot"], capture_output=True).stdout.decode("ascii", "replace")
    running = "RUNNING" in out
    age = (time.time() - LOG.stat().st_mtime) / 60
    rec("PASS" if running else "FAIL", "служба OkoBot",
        ("работает" if running else "НЕ работает") + f", последняя запись в журнале {age:.0f} мин назад")


@check("служба: задачи запущены после последнего старта")
def c_jobs():
    lines = _log_tail()
    start = _last(lines, "[NET] наблюдение за сменой сети включено")
    if not start:
        rec("FAIL", "задача net_context", "строки запуска нет в журнале"); return
    t0 = _ts(start)
    rec("PASS", "задача net_context", f"включена {t0:%d.%m %H:%M}")
    for needle, name in (("[DEVICES] наблюдение за техникой включено", "задача device_watch"),
                         ("[GARMIN] сводки включены", "сводки Garmin 08:00/21:00")):
        l = _last(lines, needle)
        ok = l and _ts(l) and _ts(l) >= t0 - dt.timedelta(minutes=2)
        rec("PASS" if ok else "WARN", name, "в этом запуске" if ok else "не найдено после старта")
    # Строка совместимости пишется при импорте stt — ДО настройки журнала, без отметки времени (30.10).
    # Поэтому — по положению: после строки запуска ПРЕДЫДУЩЕГО процесса.
    starts = [i for i, l in enumerate(lines) if "[NET] наблюдение за сменой сети включено" in l]
    prev = starts[-2] if len(starts) > 1 else -1
    ok = any("включена совместимость для faster-whisper" in l for l in lines[prev + 1:])
    rec("PASS" if ok else "WARN", "голос: совместимость PyAV",
        "включена в этом запуске" if ok else "строки нет в этом запуске")
    errs = [l for l in lines if _ts(l) and _ts(l) >= t0 and ("Traceback" in l or "[ERROR]" in l)]
    rec("PASS" if not errs else "WARN", "ошибки с последнего старта", f"{len(errs)}"
        + (f": {errs[-1].strip()[:140]}" if errs else ""))


@check("сеть: отпечаток и состояние")
def c_net():
    import net_context as nc
    fp = nc.fingerprint()
    if fp is None:
        rec("FAIL", "сеть", "нет маршрута или MAC шлюза"); return
    st = nc.load_state()
    same = st.get("current") == fp["key"]
    rec("PASS" if same else "WARN", "сеть",
        f"шлюз {fp['gw']}, домашняя: {nc.is_home(st, fp['key'])}, "
        + ("состояние совпадает" if same else f"в состоянии {st.get('current_gw')} (смена ещё не подтверждена?)"))


@check("ТВ: связь, экран, настройки")
def c_tv():
    import tv_control as t
    tv = t.TVClient.from_env()
    try:
        ctx = tv._call("GET", "context") or {}
    except t.TVTransportError as e:
        rec("FAIL", "ТВ", f"не отвечает по {tv.host} (выключен?) — {str(e)[:80]}"); return
    rec("PASS", "ТВ: связь", f"{tv.host}" + (f" (найден заново, был {tv.moved_from})" if tv.moved_from else ""))
    rec("PASS", "ТВ: экран (context)", f"{ctx.get('level1')}")
    r = tv._request("POST", "menuitems/settings/current", {"nodes": [{"nodeid": 710}, {"nodeid": 320}]})
    vals = {}
    for e in (r.json() or {}).get("values", []):
        v = e.get("value") or {}
        n = next((v[k] for k in v if k.lower() == "nodeid"), None)
        if n is not None:
            vals[n] = v.get("data") or {}
    ok = 710 in vals and "value" in vals[710]
    rec("PASS" if ok else "FAIL", "ТВ: чтение настроек",
        f"яркость Ambilight {vals.get(710, {}).get('value')}/10, за видео = {vals.get(320, {}).get('selected_item')}")


@check("SmartThings: OAuth и облако")
def c_st():
    import device_watch as dw
    import smartthings_oauth as so
    data = so.load() or {}
    left_h = (float(data.get("expires_at") or 0) - time.time()) / 3600
    rec("PASS" if left_h > 1 else ("WARN" if left_h > 0 else "FAIL"), "SmartThings OAuth",
        f"access живёт ещё {left_h:.1f} ч" + (" (обновление — внутри опроса, когда < 1 ч)" if left_h <= 1 else ""))
    token, notes = dw.current_token()
    snaps = dw.fetch_snapshots(token)
    rec("PASS", "SmartThings облако", f"машин {len(snaps)}: " + ", ".join(f"{s.kind}={s.machine}" for s in snaps)
        + (f" | {notes}" if notes else ""))


@check("Garmin: токены и свежесть базы")
def c_garmin():
    import garmin_report as gr
    import garmin_sync as gs
    if not gr.enabled():
        rec("FAIL", "Garmin", "нет файла токенов"); return
    con = sqlite3.connect(f"file:///{gs.db_path().as_posix()}?mode=ro", uri=True)
    try:
        last = con.execute("SELECT MAX(date) FROM garmin_daily").fetchone()[0]
    finally:
        con.close()
    today = dt.datetime.now(gr.tz()).date().isoformat()
    rec("PASS" if last == today else "WARN", "Garmin база", f"последний день {last} (сегодня {today})")


@check("ТВ: понимание смысла (модель)")
def c_intent():
    import os
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith(("OLLAMA_MODEL=", "OKO_TV_INTENT_MODEL=", "OLLAMA_HOST=")):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    import tv_intent as ti
    if not ti.enabled():
        rec("WARN", "ТВ: модель смысла", "выключена (нет OLLAMA_MODEL / OKO_TV_INTENT_MODEL)"); return
    t0 = time.time()
    got = ti.understand("включи свет под музыку")
    dt_s = time.time() - t0
    ok = got == "подсветка музыка"
    rec("PASS" if ok else "FAIL", "ТВ: модель смысла",
        f"{ti.model_name()}: «включи свет под музыку» -> {got!r} за {dt_s:.1f} с")


def main() -> int:
    print(f"== Око: общий тест {dt.datetime.now():%d.%m.%Y %H:%M} (только чтение)")
    for c in CHECKS:
        c()
    n = {s: sum(1 for r in RES if r[0] == s) for s in ("PASS", "WARN", "FAIL")}
    print(f"== ИТОГ: PASS {n['PASS']}  WARN {n['WARN']}  FAIL {n['FAIL']}")
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
