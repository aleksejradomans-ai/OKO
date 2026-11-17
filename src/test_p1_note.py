"""P1_NOTE + P1_LOG (26.10): _save_exchange возвращает признак записи,
строка P1 несёт tpl/len/back, ответ при P1 помечается для Оператора."""
import asyncio
import logging
import re
from types import SimpleNamespace

import pytest

import bot

TPL = "Не распознал запрос. Сформулируй задачу."
LONG = "Ответ достаточно длинный, чтобы пройти порог P1 в сорок символов."


class _FakeCore:
    def __init__(self, history):
        self._history = list(history)
        self.recorded = []

    def resolve(self, channel, ext_id):
        return "sid-1"

    def epoch_start(self, sid):
        return None

    def history(self, sid, limit, since_ts=None):
        return self._history[-limit:]

    def record_exchange(self, envelope, user_text, assistant_text):
        self.recorded.append((user_text, assistant_text))


@pytest.fixture
def fake_core(monkeypatch):
    def _make(history):
        core = _FakeCore(history)
        monkeypatch.setattr(bot, "_get_memory", lambda: core)
        return core
    return _make


def _p1_lines(caplog):
    return [r.getMessage() for r in caplog.records if "P1 loop guard" in r.getMessage()]


def test_new_answer_persisted_returns_true(fake_core):
    core = fake_core([])
    assert bot._save_exchange(1, "вопрос", LONG) is True
    assert core.recorded == [("вопрос", LONG)]


def test_repeat_other_question_blocked_returns_false_and_logs_fields(fake_core, caplog):
    core = fake_core([("user", "старый вопрос"), ("assistant", LONG),
                      ("user", "ещё"), ("assistant", "другой ответ, тоже достаточно длинный текст")])
    with caplog.at_level(logging.WARNING):
        assert bot._save_exchange(1, "новый вопрос", LONG) is False
    assert core.recorded == []
    lines = _p1_lines(caplog)
    assert len(lines) == 1
    # Префикс сохранён (по нему ищут hud_state/sleep_digest/замер).
    assert lines[0].startswith("P1 loop guard: repeated answer not persisted (user=1)")
    m = re.search(r"tpl=(\d) len=(\d+) back=(\d+)$", lines[0])
    assert m, lines[0]
    assert m.group(1) == "0"
    assert int(m.group(2)) == len(" ".join(LONG.split()))
    assert m.group(3) == "3"  # совпавший ответ -- 3-е событие с конца


def test_template_repeat_flagged_tpl1(fake_core, caplog):
    fake_core([("user", "qwzx"), ("assistant", TPL)])
    with caplog.at_level(logging.WARNING):
        assert bot._save_exchange(1, "asdf", TPL) is False
    assert "tpl=1 " in _p1_lines(caplog)[0]


def test_template_is_exactly_at_threshold():
    # Шаблон identity = ровно 40 символов после нормализации -> под P1.
    assert len(" ".join(TPL.split()).lower()) == 40
    assert " ".join(TPL.split()).lower().startswith(bot.P1_TEMPLATE_PREFIX)


def test_same_question_same_answer_persisted(fake_core):
    core = fake_core([("user", "вопрос"), ("assistant", LONG)])
    assert bot._save_exchange(1, "вопрос", LONG) is True
    assert len(core.recorded) == 1


def test_short_answer_never_p1(fake_core):
    core = fake_core([("user", "a"), ("assistant", "коротко")])
    assert bot._save_exchange(1, "b", "коротко") is True
    assert len(core.recorded) == 1


def test_with_p1_note_contract():
    assert bot._with_p1_note("x", False) == f"{bot.P1_NOTE}\nx"
    assert bot._with_p1_note("x", True) == "x"
    assert bot._with_p1_note("x", None) == "x"


def test_note_survives_clean_output():
    out = bot.core.clean_output(bot._with_p1_note(LONG, False))
    assert out.startswith(bot.P1_NOTE)


class _Dispatcher:
    def dispatch(self, text):
        return SimpleNamespace(is_allowed=True, sanitized_text=text, incidents=[])


class _Monitor:
    def record_event(self, ev):
        pass


class _Governor:
    def __init__(self, answer):
        self.answer = answer

    async def execute_request(self, user_input, user_id, context):
        return self.answer


@pytest.fixture
def pipeline(monkeypatch):
    async def _ctx(q):
        return ""
    monkeypatch.setattr(bot, "retrieve_context", _ctx)
    monkeypatch.setattr(bot, "_fetch_history", lambda uid, limit: [])
    monkeypatch.setattr(bot, "_log_depth", lambda uid: None)
    monkeypatch.setattr(bot, "_with_notice", lambda r, inc: r)


def _run(answer):
    return asyncio.run(bot.process_logic("вопрос", 1, _Governor(answer), _Dispatcher(), _Monitor()))


def test_process_logic_marks_p1(pipeline, monkeypatch):
    monkeypatch.setattr(bot, "_save_exchange", lambda uid, q, a: False)
    assert _run(LONG).startswith(bot.P1_NOTE + "\n")


def test_process_logic_no_mark_when_persisted(pipeline, monkeypatch):
    monkeypatch.setattr(bot, "_save_exchange", lambda uid, q, a: True)
    assert _run(LONG) == LONG


def test_process_logic_error_marker_no_mark_no_save(pipeline, monkeypatch):
    called = []
    monkeypatch.setattr(bot, "_save_exchange", lambda *a: called.append(a) or False)
    assert _run("[ERR: LLM_UNAVAILABLE]") == "[ERR: LLM_UNAVAILABLE]"
    assert called == []
