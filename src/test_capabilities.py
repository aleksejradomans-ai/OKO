"""CAPS_CATALOG (этап A, 30.10.2026): реестр подтверждений, каталог функций,
drift-сверка каталога с кодом (bot.py, tv_commands) и подключение в bot.py."""
import datetime as dt
import json
import re
from pathlib import Path

import pytest

import capabilities as caps
import proof_registry as pr
import tv_commands

SRC = Path(__file__).resolve().parent
NOW = dt.datetime(2026, 10, 30, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
TS = "2026-10-30T13:11:04+02:00"


def _bot_text() -> str:
    return (SRC / "bot.py").read_text(encoding="utf-8-sig")


# --- proof_registry ---------------------------------------------------------

def test_registry_missing_file_is_empty(tmp_path):
    reg = pr.ProofRegistry(tmp_path / "none" / "r.jsonl")
    assert reg.latest() == {}
    assert reg.refresh() is False


def test_registry_record_and_reload(tmp_path):
    p = tmp_path / "data" / "r.jsonl"
    reg = pr.ProofRegistry(p)
    reg.record("tv", "PASS", src="test", ref="log", net="10.95.175", ts=TS)
    reg.record("tv", "FAIL", src="test", ts=TS)
    assert reg.latest()["tv"]["status"] == "FAIL"
    assert pr.ProofRegistry(p).latest()["tv"]["status"] == "FAIL"
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2  # append-only, история цела


def test_registry_refresh_reads_only_external_delta(tmp_path):
    p = tmp_path / "r.jsonl"
    reg = pr.ProofRegistry(p)
    reg.record("tv", "PASS", src="bot", ts=TS)
    with open(p, "a", encoding="utf-8") as f:  # другой процесс (selfcheck)
        f.write(json.dumps({"ts": TS, "fn": "garmin", "status": "WARN", "src": "selfcheck"}) + "\n")
    assert reg.refresh() is True
    assert set(reg.latest()) == {"tv", "garmin"}
    assert reg.refresh() is False


def test_registry_record_does_not_skip_external_lines(tmp_path):
    p = tmp_path / "r.jsonl"
    reg = pr.ProofRegistry(p)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": TS, "fn": "net", "status": "PASS", "src": "x"}) + "\n")
    reg.record("tv", "PASS", src="bot", ts=TS)  # без refresh между ними
    assert set(reg.latest()) == {"net", "tv"}


def test_registry_partial_tail_waits(tmp_path):
    p = tmp_path / "r.jsonl"
    line = json.dumps({"ts": TS, "fn": "tv", "status": "PASS", "src": "x"})
    p.write_text(line[:10], encoding="utf-8")
    reg = pr.ProofRegistry(p)
    assert reg.latest() == {}
    with open(p, "a", encoding="utf-8") as f:
        f.write(line[10:] + "\n")
    assert reg.refresh() is True
    assert reg.latest()["tv"]["status"] == "PASS"


def test_registry_bad_line_skipped(tmp_path):
    p = tmp_path / "r.jsonl"
    good = json.dumps({"ts": TS, "fn": "tv", "status": "PASS", "src": "x"})
    bad_status = json.dumps({"ts": TS, "fn": "tv", "status": "OK", "src": "x"})
    p.write_text("{oops\n" + bad_status + "\n" + good + "\n", encoding="utf-8")
    reg = pr.ProofRegistry(p)
    assert reg.bad_lines == 2
    assert reg.latest()["tv"]["status"] == "PASS"


def test_registry_shrunk_file_full_reload(tmp_path):
    p = tmp_path / "r.jsonl"
    reg = pr.ProofRegistry(p)
    reg.record("tv", "PASS", src="x", ts=TS)
    reg.record("garmin", "PASS", src="x", ts=TS)
    p.write_text(json.dumps({"ts": TS, "fn": "net", "status": "FAIL", "src": "x"}) + "\n",
                 encoding="utf-8")
    assert reg.refresh() is True
    assert set(reg.latest()) == {"net"}


@pytest.mark.parametrize("fn,status,src", [("", "PASS", "x"), ("tv", "OK", "x"), ("tv", "PASS", "")])
def test_registry_record_validates(tmp_path, fn, status, src):
    reg = pr.ProofRegistry(tmp_path / "r.jsonl")
    with pytest.raises(ValueError):
        reg.record(fn, status, src=src)


def test_registry_latest_is_copy(tmp_path):
    reg = pr.ProofRegistry(tmp_path / "r.jsonl")
    reg.record("tv", "PASS", src="x", ts=TS)
    reg.latest()["tv"]["status"] = "FAIL"
    assert reg.latest()["tv"]["status"] == "PASS"


def test_registry_env_path(monkeypatch, tmp_path):
    monkeypatch.setenv(pr.ENV_PATH, str(tmp_path / "x.jsonl"))
    assert pr.default_path() == tmp_path / "x.jsonl"


# --- capabilities: каталог --------------------------------------------------

def _rec(status="PASS", ts=TS, net="10.95.175"):
    return {"ts": ts, "fn": "x", "status": status, "src": "t", "net": net}


def test_catalog_without_proofs_has_no_status_marks():
    text = caps.build_catalog(now=NOW)
    assert text.startswith(caps.HEADER + "\n" + caps.RULE)
    fn_lines = [l for l in text.splitlines() if l.startswith("- ")]
    assert len(fn_lines) == len(caps.DECLARED)
    assert not any(re.search(r"\[(PASS|WARN|FAIL|давно|в этой сети)", l) for l in fn_lines)


def test_catalog_marks_checkable_only():
    index = {c.key: _rec() for c in caps.DECLARED}
    text = caps.build_catalog(index, now=NOW)
    for c in caps.DECLARED:
        body = c.brief or c.text      # CAPS_BRIEF (02.11): в каталоге короткая строка
        line = next(l for l in text.splitlines() if l == "- " + body or l.startswith("- " + body + " ["))
        assert line.endswith("[PASS 30.10]") is c.checkable


def test_status_suffix_variants():
    assert caps.status_suffix(None) == ""
    assert caps.status_suffix(_rec(ts="garbage"), now=NOW) == ""
    assert caps.status_suffix(_rec("WARN"), now=NOW) == " [WARN 30.10]"
    assert caps.status_suffix(_rec(), net="10.17.115", now=NOW) == " [в этой сети не проверено]"
    old = (NOW - dt.timedelta(seconds=caps.MAX_AGE_S + 1)).isoformat()
    assert caps.status_suffix(_rec(ts=old), now=NOW).startswith(" [давно: PASS")


def test_human_text_lists_all_without_rule():
    text = caps.human_text(now=NOW)
    assert caps.RULE not in text and caps.HEADER not in text
    assert sum(1 for l in text.splitlines() if l.startswith("• ")) == len(caps.DECLARED)


def test_catalog_fits_governor_budget_worst_case():
    import governor
    index = {c.key: _rec() for c in caps.DECLARED}
    worst = max(len(caps.build_catalog(index, net="other", now=NOW)),
                len(caps.build_catalog(index, now=NOW)))
    assert worst <= governor.CAPABILITIES_MAX_CHARS


def test_rule_survives_truncation():
    import governor
    text = caps.build_catalog(now=NOW)
    assert caps.RULE in text[: governor.CAPABILITIES_MAX_CHARS]


# --- drift: каталог против кода ---------------------------------------------

def test_drift_commands_registered_in_bot():
    registered = set(re.findall(r'CommandHandler\(\s*"([a-z_]+)"', _bot_text()))
    declared = {cmd for c in caps.DECLARED for cmd in c.commands}
    assert declared <= registered, f"в каталоге, но не в bot.py: {declared - registered}"


def test_drift_slash_mentions_are_declared():
    declared = {cmd for c in caps.DECLARED for cmd in c.commands}
    for c in caps.DECLARED:
        for cmd in re.findall(r"/([a-z_]+)", c.text):
            assert cmd in declared, f"«/{cmd}» в тексте {c.key}, но не в commands"


def test_drift_tv_phrases_parse():
    tv = next(c for c in caps.DECLARED if c.key == "tv")
    quoted = set(re.findall(r"«([^»]+)»", tv.text))
    assert quoted <= set(tv.tv_examples), f"фразы в тексте без проверки: {quoted - set(tv.tv_examples)}"
    for phrase in tv.tv_examples:
        cmd = tv_commands.parse(phrase)  # Unknown/Refusal -> тест падает
        assert isinstance(cmd, tv_commands.Command), phrase


def test_drift_tv_refusals_still_refused():
    """Каталог говорит «открыть приложения нельзя» -- код обязан отказывать. (03.11: включение -- Wake-on-LAN.)"""
    for phrase in ("открой ютуб",):
        with pytest.raises(tv_commands.Refusal):
            tv_commands.parse(phrase)
    assert tv_commands.parse("включи телевизор").name == "power_on"


# --- подключение в bot.py ---------------------------------------------------

def test_bot_applies_catalog_after_governor_start():
    text = _bot_text()
    body = text.split("async def post_init(", 1)[1].split("\nasync def ", 1)[0]
    start = body.index("await governor.start()")
    assert body.index("_caps_apply", start) > start


def test_bot_imports_modules_one_way():
    text = _bot_text()
    assert re.search(r"^import capabilities\b", text, re.M)
    assert re.search(r"^import proof_registry\b", text, re.M)
    assert "src.capabilities" not in text and "src.proof_registry" not in text
