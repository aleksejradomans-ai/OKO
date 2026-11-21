"""Журнал согласий (src/consent.py, tools/consent.py): запись -> чтение назад, цепочка, require -> GO / ASK.
Только временные базы (tmp_path); время -- константы."""
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import consent as cs

CLI = Path(__file__).resolve().parent.parent / "tools" / "consent.py"
NOW = dt.datetime(2026, 11, 21, 17, 0, 0)


def e(**kw):
    x = {"ts": "2026-11-21T16:54:00", "source": "chat", "question_id": "sb-20261121-1654-b30", "answer": 1,
         "question_text": "B30: start the block", "question_code": "block_go", "task": "B30"}
    x.update(kw)
    return x


def test_add_then_read_back_without_text(tmp_path):
    db = tmp_path / "c.db"
    r = cs.add(e(), db, now=NOW)
    assert r["new"] is True
    rows = cs.list_entries(db)
    assert len(rows) == 1 and rows[0]["entry_id"] == r["entry_id"] and rows[0]["seq"] == 1
    assert rows[0]["answer"] == 1 and rows[0]["question_code"] == "block_go" and rows[0]["task"] == "B30"
    assert rows[0]["question_sha16"] == cs.question_sha16("B30: start the block")
    raw = sqlite3.connect(str(db)).execute("SELECT * FROM consent").fetchall()
    assert "start the block" not in json.dumps(raw)                        # текст вопроса не хранится
    assert rows[0]["prev_sha16"] == cs.GENESIS and cs.verify(db)["ok"]


def test_repeat_is_same_row(tmp_path):
    db = tmp_path / "c.db"
    a, b = cs.add(e(), db, now=NOW), cs.add(e(), db, now=NOW)
    assert a["entry_id"] == b["entry_id"] and b["new"] is False and len(cs.list_entries(db)) == 1


def test_require_go_only_with_answer_1(tmp_path):
    db = tmp_path / "c.db"
    assert cs.require("B30", "block_go", db)["decision"] == "ASK"           # нет базы / записи -> ASK
    cs.add(e(answer=0, ts="2026-11-21T16:00:00"), db, now=NOW)
    q = cs.require("B30", "block_go", db)
    assert q["decision"] == "ASK" and q["reason"] == "operator_answered_0"
    cs.add(e(), db, now=NOW)                                                  # позже -- [1]
    q = cs.require("B30", "block_go", db)
    assert q["decision"] == "GO" and q["ts"] == "2026-11-21T16:54:00"
    assert cs.require("B30", "uac_restart", db)["decision"] == "ASK"         # другой вопрос -- нет записи
    assert cs.require("B31", "block_go", db)["decision"] == "ASK"


def test_latest_answer_wins_by_time(tmp_path):
    db = tmp_path / "c.db"
    cs.add(e(ts="2026-11-21T17:10:00", answer=0, question_id="q2"), db, now=NOW)   # записан раньше, но позже по времени
    cs.add(e(), db, now=NOW)
    assert cs.require("B30", "block_go", db)["decision"] == "ASK"


def test_chain_detects_edit_and_delete(tmp_path):
    db = tmp_path / "c.db"
    cs.add_many([e(), e(question_id="q2", question_code="uac_restart"), e(question_id="q3", question_code="x_go")],
                db, now=NOW)
    assert cs.verify(db) == {"ok": True, "rows": 3, "broken_seq": None, "reason": None}
    con = sqlite3.connect(str(db))
    con.execute("UPDATE consent SET answer = 1 - answer WHERE seq = 2")       # правка задним числом
    con.commit()
    con.close()
    v = cs.verify(db)
    assert not v["ok"] and v["broken_seq"] == 2
    assert cs.require("B30", "block_go", db)["decision"] == "ASK"            # цепочка нарушена -> не действие
    db2 = tmp_path / "d.db"
    cs.add_many([e(), e(question_id="q2"), e(question_id="q3")], db2, now=NOW)
    con = sqlite3.connect(str(db2))
    con.execute("DELETE FROM consent WHERE seq = 2")
    con.commit()
    con.close()
    assert cs.verify(db2)["reason"] == "seq_gap"


@pytest.mark.parametrize("bad,frag", [({"answer": 2}, "answer"), ({"answer": True}, "answer"),
                                      ({"source": "email"}, "source"), ({"question_code": "Has Text"}, "question_code"),
                                      ({"task": "b30"}, "task"), ({"ts": "16:54"}, "ts"),
                                      ({"question_text": None}, "question_sha16"), ({"question_id": ""}, "question_id")])
def test_invalid_entry_writes_nothing(tmp_path, bad, frag):
    db = tmp_path / "c.db"
    with pytest.raises(cs.ConsentError) as ei:
        cs.add_many([e(), e(**bad)], db, now=NOW)
    assert frag in str(ei.value)
    assert cs.list_entries(db) == []


def test_block_without_consent_is_ask_not_action(tmp_path):
    """Блок, которому нужен [1]: решение только из журнала; без записи -> ASK (действие не начинается)."""
    db = tmp_path / "c.db"
    acted = []

    def block():
        q = cs.require("B30", "block_go", db)
        if q["decision"] != "GO":
            return "ASK"
        acted.append(1)
        return "GO"
    assert block() == "ASK" and acted == []
    cs.add(e(), db, now=NOW)
    assert block() == "GO" and acted == [1]


def test_broken_database_is_ask(tmp_path):
    db = tmp_path / "c.db"
    db.write_bytes(b"not a database")
    q = cs.require("B30", "block_go", db)
    assert q["decision"] == "ASK" and q["reason"].startswith("db_error:")


def _cli(*args):
    r = subprocess.run([sys.executable, str(CLI), *args], capture_output=True)
    return r.returncode, json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_cli_add_list_verify_require(tmp_path):
    db = str(tmp_path / "c.db")
    rc, j = _cli("--db", db, "verify", "--require-task", "B30", "--require-code", "block_go")
    assert rc == 3 and j["require"]["decision"] == "ASK"
    rc, j = _cli("--db", db, "add", "--ts", "2026-11-21T16:54:00", "--source", "chat", "--question-id", "q1",
                 "--answer", "1", "--code", "block_go", "--task", "B30", "--question-text", "B30 start")
    assert rc == 0 and j["new"] is True
    rc, j = _cli("--db", db, "list", "--task", "B30")
    assert rc == 0 and j["rows"] == 1 and j["entries"][0]["answer"] == 1 and "B30 start" not in json.dumps(j)
    rc, j = _cli("--db", db, "verify", "--require-task", "B30", "--require-code", "block_go")
    assert rc == 0 and j["ok"] and j["require"]["decision"] == "GO"
    rc, j = _cli("--db", db, "add", "--ts", "x", "--source", "chat", "--question-id", "q1", "--answer", "1",
                 "--code", "block_go", "--question-sha16", "0123456789abcdef")
    assert rc == 2 and "ts" in j["error"]


def test_cli_add_file_retro(tmp_path):
    db = str(tmp_path / "c.db")
    f = tmp_path / "r.json"
    f.write_text(json.dumps([dict(e(), origin="retro_summary", ts_exact=False, question_text=None,
                                  question_sha16="0123456789abcdef"),
                             dict(e(question_id="q0", answer=0, question_code="keep_frames"), origin="retro_summary")]),
                 encoding="utf-8")
    rc, j = _cli("--db", db, "add-file", str(f))
    assert rc == 0 and (j["records"], j["new"]) == (2, 2)
    rc, j = _cli("--db", db, "add-file", str(f))
    assert rc == 0 and j["new"] == 0
    rows = cs.list_entries(tmp_path / "c.db")
    assert [r["origin"] for r in rows] == ["retro_summary"] * 2 and rows[0]["ts_exact"] == 0
