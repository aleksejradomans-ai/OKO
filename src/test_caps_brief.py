"""CAPS_BRIEF (02.11.2026): короткий каталог в system prompt, полный -- в «что умеешь»."""
import datetime as dt
import re

import capabilities as caps
import tv_commands

NOW = dt.datetime(2026, 11, 2, 22, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
TS = "2026-11-02T21:00:00+02:00"


def _all_marked():
    return {c.key: {"ts": TS, "fn": c.key, "status": "PASS", "src": "t", "net": "x"} for c in caps.DECLARED}


def test_every_capability_has_shorter_brief():
    for c in caps.DECLARED:
        assert c.brief, c.key
        assert len(c.brief) < len(c.text), c.key


def test_catalog_uses_brief_and_keeps_rule():
    text = caps.build_catalog(now=NOW)
    assert text.startswith(caps.HEADER + "\n" + caps.RULE)
    for c in caps.DECLARED:
        assert "- " + c.brief in text
        assert c.text not in text


def test_catalog_worst_case_size():
    worst = max(len(caps.build_catalog(_all_marked(), now=NOW)),
                len(caps.build_catalog(_all_marked(), net="other", now=NOW)))
    assert worst <= 1000, worst         # было 1323 (02.11 13:41); худший случай -- «в этой сети не проверено» x5


def test_human_text_keeps_full_text():
    text = caps.human_text(now=NOW)
    for c in caps.DECLARED:
        assert c.text in text


def test_brief_tv_quotes_parse_and_are_examples():
    tv = next(c for c in caps.DECLARED if c.key == "tv")
    quoted = set(re.findall(r"«([^»]+)»", tv.brief))
    assert quoted and quoted <= set(tv.tv_examples)
    for phrase in quoted:
        assert isinstance(tv_commands.parse(phrase), tv_commands.Command)


def test_brief_slash_mentions_declared():
    declared = {cmd for c in caps.DECLARED for cmd in c.commands}
    for c in caps.DECLARED:
        for cmd in re.findall(r"/([a-z_]+)", c.brief):
            assert cmd in declared, f"«/{cmd}» в brief {c.key}, но не в commands"


def test_brief_keeps_refusals():
    tv = next(c for c in caps.DECLARED if c.key == "tv")
    assert "нельзя" in tv.brief
    dev = next(c for c in caps.DECLARED if c.key == "devices")
    assert "нельзя" in dev.brief
