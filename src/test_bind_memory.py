"""R2c (11.11, решение 13:1x вариант 1): BindMemory — память связок «фраза -> штатная команда»."""
from __future__ import annotations

import datetime as dt
import threading

import pytest

from oko import Settings
from oko.memory import BindMemory
from oko.registry import event_registry as ev
from oko.registry import open_db

OP = "op"


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 11, 11, 13, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.t                     # часы стоят: как на Windows в пределах тика (RULES №10)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def mem(clock):
    return BindMemory(open_db(), clock=clock)


def types(mem):
    return [e.type for e in mem.registry.events(OP)]


def bind(mem, key, command, status="VERIFIED"):
    cid = mem.executed(OP, key, command, status)
    return mem.remember(cid)


def test_observation_alone_is_not_a_binding(mem):
    mem.executed(OP, "вруби телек", "телевизор включи", "VERIFIED")
    assert mem.lookup(OP, "вруби телек") is None and mem.bind_count(OP) == 0
    assert ev.ALIAS_ADDED not in types(mem)


def test_one_press_after_verified_is_a_working_binding(mem):
    out = bind(mem, "вруби телек", "телевизор включи")
    assert out.status == "REMEMBERED"
    assert mem.lookup(OP, "вруби телек") == "телевизор включи"
    assert mem.lookup("other", "вруби телек") is None                  # память личная


@pytest.mark.parametrize("status", ["ACKNOWLEDGED", "MISMATCH", "FAILED"])
def test_no_binding_without_verified(mem, status):
    out = bind(mem, "вруби телек", "телевизор включи", status)
    assert out.status == "NOT_VERIFIED" and out.request["text"].startswith("Результат не подтверждён")
    assert mem.lookup(OP, "вруби телек") is None and ev.BIND_REFUSED in types(mem)


def test_verified_of_another_command_gives_no_binding(mem):
    good = mem.executed(OP, "свет", "подсветка красная", "VERIFIED")
    bad = mem.executed(OP, "вруби телек", "телевизор включи", "ACKNOWLEDGED")
    assert mem.remember(bad).status == "NOT_VERIFIED"
    assert mem.lookup(OP, "вруби телек") is None
    assert mem.remember(good).status == "REMEMBERED"
    with pytest.raises(KeyError):
        mem.remember("forged-id")


def test_repeat_press_is_already_and_writes_nothing(mem):
    cid = mem.executed(OP, "вруби телек", "телевизор включи", "VERIFIED")
    mem.remember(cid)
    n = len(mem.registry.events(OP))
    assert mem.remember(cid).status == "ALREADY" and len(mem.registry.events(OP)) == n


def test_reject_degrades_and_stops_auto_use(mem):
    bind(mem, "вруби телек", "телевизор включи")
    used = mem.executed(OP, "вруби телек", "телевизор включи", "VERIFIED")    # исполнено по связке
    assert mem.reject(used).status == "REJECTED"
    assert mem.lookup(OP, "вруби телек") is None
    assert mem.reject(used).status == "ALREADY"
    with pytest.raises(ValueError):
        mem.remember(used)


def test_rebinding_a_phrase_replaces_its_command(mem):
    """F01 (Fable 11.11): переученная фраза работает с НОВОЙ командой, а не замолкает «неоднозначно»."""
    bind(mem, "вруби", "телевизор включи")
    assert bind(mem, "вруби", "подсветка включи").status == "REMEMBERED"
    assert mem.lookup(OP, "вруби") == "подсветка включи" and mem.bind_count(OP) == 1
    (evt,) = mem.registry.events(OP, (ev.BIND_EVICTED,))
    assert evt.payload["reason"] == "rebind"


def test_full_table_asks_then_replaces_oldest_by_use(clock):
    mem = BindMemory(open_db(), Settings(bind_limit=2), clock=clock)
    bind(mem, "а", "команда а")
    bind(mem, "б", "команда б")
    mem.executed(OP, "а", "команда а", "VERIFIED")                       # «а» использована позже
    cid = mem.executed(OP, "в", "команда в", "VERIFIED")
    ask = mem.remember(cid)
    assert ask.status == "EVICT" and ask.request["text"] == "Таблица связок полна: 2. Самая старая: «б». Заменить?"
    assert ev.BIND_EVICTED not in types(mem) and mem.lookup(OP, "б") == "команда б"
    done = mem.resolve(ask.request["request_id"])
    assert done.status == "REMEMBERED" and mem.lookup(OP, "в") == "команда в" and mem.lookup(OP, "б") is None
    assert mem.bind_count(OP) == 2 and ev.BIND_EVICTED in types(mem)
    with pytest.raises(KeyError):
        mem.resolve(ask.request["request_id"])


def test_full_table_keep_saves_nothing(clock):
    mem = BindMemory(open_db(), Settings(bind_limit=1), clock=clock)
    bind(mem, "а", "команда а")
    ask = mem.remember(mem.executed(OP, "в", "команда в", "VERIFIED"))
    assert mem.cancel(ask.request["request_id"]).status == "CANCELLED"
    assert mem.lookup(OP, "а") == "команда а" and mem.lookup(OP, "в") is None


def test_evict_question_expires(clock):
    mem = BindMemory(open_db(), Settings(bind_limit=1), clock=clock)
    bind(mem, "а", "команда а")
    ask = mem.remember(mem.executed(OP, "в", "команда в", "VERIFIED"))
    clock.t += dt.timedelta(seconds=91)
    assert mem.resolve(ask.request["request_id"]).status == "EXPIRED"
    assert mem.lookup(OP, "а") == "команда а" and ev.BIND_EVICTED not in types(mem)


def test_replace_failure_rolls_back(tmp_path, clock, monkeypatch):
    path = str(tmp_path / "g6.db")
    mem = BindMemory(open_db(path), Settings(bind_limit=1), clock=clock)
    bind(mem, "а", "команда а")
    ask = mem.remember(mem.executed(OP, "в", "команда в", "VERIFIED"))
    real = mem.engine.record

    def boom(e, frozen=False):
        if e.remember:
            raise RuntimeError("disk")
        return real(e, frozen)
    monkeypatch.setattr(mem.engine, "record", boom)
    with pytest.raises(RuntimeError):
        mem.resolve(ask.request["request_id"])
    fresh = BindMemory(open_db(path), Settings(bind_limit=1), clock=clock)
    assert fresh.lookup(OP, "а") == "команда а" and fresh.lookup(OP, "в") is None


def test_bindings_survive_restart(tmp_path, clock):
    path = str(tmp_path / "g6.db")
    bind(BindMemory(open_db(path), clock=clock), "вруби телек", "телевизор включи")
    assert BindMemory(open_db(path), clock=clock).lookup(OP, "вруби телек") == "телевизор включи"


def test_threads_share_one_memory(tmp_path, clock):
    mem = BindMemory(open_db(str(tmp_path / "g6.db"), check_same_thread=False), clock=clock)
    errors = []

    def work(i):
        try:
            bind(mem, f"фраза {i}", f"команда {i}")
        except Exception as e:        # pragma: no cover
            errors.append(e)
    th = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    assert errors == [] and mem.bind_count(OP) == 8


def test_two_connections_one_file(tmp_path, clock):
    """P1.2: два писателя (Telegram + окно) — одна база; второй видит запись первого."""
    path = str(tmp_path / "g6.db")
    a = BindMemory(open_db(path), clock=clock)
    b = BindMemory(open_db(path), clock=clock)
    bind(a, "вруби телек", "телевизор включи")
    assert b.lookup(OP, "вруби телек") == "телевизор включи"
