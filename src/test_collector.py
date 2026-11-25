"""COLLECTOR (13.11): OKO_COLLECTOR=1 -> память/связки/события на временной копии, оригиналы не меняются;
оценки и база знаний выключены."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _db(p: Path, rows: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.execute("create table events(id integer primary key, content text)")
    c.executemany("insert into events(content) values(?)", [(f"r{i}",) for i in range(rows)])
    c.commit()
    c.close()


def _probe(tmp_path: Path, collector: bool) -> dict:
    base = tmp_path / "data"
    _db(base / "memory" / "memory.db", 3)
    _db(base / "memory" / "g6.db", 2)
    ev = base / "events.jsonl"
    ev.write_text('{"e":1}\n', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("OKO_COLLECTOR", "OKO_EVENT_LOG")}
    env.update(AI_BASE_DIR=str(base), OKO_EVENT_LOG=str(ev), PYTHONIOENCODING="utf-8")
    if collector:
        env["OKO_COLLECTOR"] = "1"
    code = (
        "import json, os, sqlite3, config\n"
        "m = config.MEMORY_DB_PATH\n"
        "c = sqlite3.connect(m); c.execute(\"insert into events(content) values('new')\"); c.commit()\n"
        "n = c.execute('select count(*) from events').fetchone()[0]; c.close()\n"
        "g6 = m.parent / 'g6.db'\n"
        "print(json.dumps({'mem': str(m), 'n': n, 'g6': g6.exists(), 'col': config.COLLECTOR,"
        " 'dir': str(config.COLLECTOR_DIR), 'ev': os.environ.get('OKO_EVENT_LOG')}))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    import json
    out = json.loads(r.stdout.strip().splitlines()[-1])
    c = sqlite3.connect(base / "memory" / "memory.db")
    out["orig_n"] = c.execute("select count(*) from events").fetchone()[0]
    c.close()
    out["base"] = str(base)
    return out


def test_collector_writes_go_to_temp_copy(tmp_path):
    o = _probe(tmp_path, collector=True)
    assert o["col"] is True and o["g6"] is True
    assert o["n"] == 4 and o["orig_n"] == 3                  # копия выросла, оригинал -- нет
    assert not o["mem"].startswith(o["base"]) and "oko_collector_" in o["mem"]
    assert o["ev"].startswith(o["dir"])
    assert not Path(o["dir"]).exists()                       # временная папка удалена при выходе процесса


def test_without_collector_writes_original(tmp_path):
    o = _probe(tmp_path, collector=False)
    assert o["col"] is False and o["orig_n"] == 4 and o["mem"].startswith(o["base"])


def test_collector_rating_and_index_skipped(monkeypatch, tmp_path):
    import bot
    import config
    monkeypatch.setattr(config, "COLLECTOR", True)
    assert bot._record_rating(1, "так", "x") == bot.COLLECTOR_TEXT
    assert bot._index_approved_file_in_thread(tmp_path / "a.pdf", None) == bot.COLLECTOR_TEXT
