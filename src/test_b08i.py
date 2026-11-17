"""(17.11): публичный срез -- пути по умолчанию от папки скрипта, очистка истории без зашитого id,
.env.example без комментариев в значениях. Боевая логика не меняется; значения id / путей в тестах синтетические."""
import asyncio
import importlib.util
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- п.5: _clear_history -- id только из --user-id или единственного ADMIN_USER_IDS, иначе отказ ------------------------
@pytest.fixture
def ch():
    return _load("tools/_clear_history.py", "b08i_clear_history")


def test_b08i_clear_history_has_no_numeric_id_in_code():
    text = (REPO / "tools" / "_clear_history.py").read_text(encoding="utf-8")
    assert not re.search(r"\d{6,}", text)
    assert "user_id = ?" in text


def test_b08i_clear_history_root_is_repo(ch):
    assert ch.ROOT == REPO


@pytest.mark.parametrize("ids", [frozenset(), frozenset({101, 202})], ids=["none", "two"])
def test_b08i_clear_history_fail_closed_without_single_id(ch, monkeypatch, ids):
    import config
    import db
    monkeypatch.setattr(config, "ADMIN_USER_IDS", ids)
    opened = []
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: opened.append(1))
    with pytest.raises(SystemExit):
        ch.main([])
    assert opened == []


def test_b08i_clear_history_rejects_non_positive_id(ch):
    with pytest.raises(SystemExit):
        ch.resolve_user_id(["--user-id", "0"])


def _db(tmp_path):
    p = tmp_path / "kb.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE sessions (user_id INTEGER, text TEXT)")
    c.executemany("INSERT INTO sessions VALUES (?, ?)", [(111, "a"), (111, "b"), (222, "c")])
    c.commit()
    c.close()
    return p


def test_b08i_clear_history_deletes_only_given_user(ch, monkeypatch, tmp_path):
    import db
    p = _db(tmp_path)
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: sqlite3.connect(p))
    assert ch.main(["--user-id", "111"]) == 0
    rows = sqlite3.connect(p).execute("SELECT user_id FROM sessions").fetchall()
    assert rows == [(222,)]


def test_b08i_clear_history_single_admin_fallback(ch, monkeypatch, tmp_path):
    import config
    import db
    p = _db(tmp_path)
    monkeypatch.setattr(config, "ADMIN_USER_IDS", frozenset({222}))
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: sqlite3.connect(p))
    assert ch.main([]) == 0
    rows = sqlite3.connect(p).execute("SELECT user_id FROM sessions ORDER BY user_id").fetchall()
    assert rows == [(111,), (111,)]


# --- п.4: пути по умолчанию считаются от расположения файла (на ПК -- тот же корень, что раньше) -----------------------
def test_b08i_selfcheck_root_is_repo():
    mod = _load("tools/oko_selfcheck.py", "b08i_selfcheck")
    assert mod.ROOT == REPO
    assert mod.LOG == REPO / "logs" / "OkoBot.stderr.log"


def test_b08i_robo_inventory_refuses_store_inside_repo(monkeypatch):
    mod = _load("src/robo_cloud_inventory.py", "b08i_robo_inv")
    monkeypatch.setattr(sys, "argv", ["robo_cloud_inventory.py", "--email", "user@example.com",
                                      "--store", str(REPO / "data" / "b08i_never_written.json")])
    with pytest.raises(SystemExit) as e:
        asyncio.run(mod.main())
    assert "внутри" in str(e.value) and str(REPO) in str(e.value)
    assert not (REPO / "data" / "b08i_never_written.json").exists()


@pytest.mark.parametrize("rel, needle", [
    ("oko-ctl.ps1", "Join-Path $PSScriptRoot \"nssm.exe\""),
    ("tools/install_from_downloads.ps1", "[string]$Root = (Split-Path -Parent $PSScriptRoot)"),
    ("tools/oko_hotkeys.ahk", "SplitPath(A_ScriptDir, , &okoRoot)"),
], ids=["ctl", "install", "hotkeys"])
def test_b08i_scripts_use_own_folder(rel, needle):
    text = (REPO / rel).read_text(encoding="utf-8-sig")
    assert needle in text
    assert not re.search(r"(?i)[CE]:\\+oko(?![A-Za-z0-9_])", text)


# --- дополнительно, тот же файл: .env.example -- комментарий не попадает в значение (python-dotenv) ----------------------
def test_b08i_env_example_values_have_no_comments():
    from dotenv import dotenv_values
    vals = dotenv_values(REPO / ".env.example")
    assert vals and not [k for k, v in vals.items() if v and "#" in v]
    assert vals["TELEGRAM_TOKEN"] == "" and vals["ADMIN_USER_IDS"] == "" and vals["OKO_LOG_FILE"] == ""
    assert vals["LLM_TIMEOUT"] == "90" and vals["OKO_MEMORY_TTL_DAYS"] == "0" and vals["OKO_SIGMA_LIVE"] == "0"
