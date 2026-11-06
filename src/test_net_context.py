"""net_context: отпечаток сети, подтверждение смены, изоляция проверок, домашние сети (29.10.2026)."""
import json
import subprocess
import sys
import types

import pytest

import net_context as nc

ROUTE_RU = b"""
===========================================================================
\xd0\x90\xd0\xba\xd1\x82\xd0\xb8\xd0\xb2\xd0\xbd\xd1\x8b\xd0\xb5 \xd0\xbc\xd0\xb0\xd1\x80\xd1\x88\xd1\x80\xd1\x83\xd1\x82\xd1\x8b:
\xd0\xa1\xd0\xb5\xd1\x82\xd0\xb5\xd0\xb2\xd0\xbe\xd0\xb9 \xd0\xb0\xd0\xb4\xd1\x80\xd0\xb5\xd1\x81           \xd0\x9c\xd0\xb0\xd1\x81\xd0\xba\xd0\xb0
          0.0.0.0          0.0.0.0     192.168.50.1   192.168.50.167     55
===========================================================================
"""


def _run(out):
    return lambda *a, **k: types.SimpleNamespace(stdout=out)


def test_gateway_from_localized_route_print():
    assert nc.default_gateway(_run(ROUTE_RU)) == ("192.168.50.1", "192.168.50.167")


def test_gateway_lowest_metric_wins():
    out = (b"          0.0.0.0          0.0.0.0     10.133.133.1   10.133.133.20     80\n"
           b"          0.0.0.0          0.0.0.0     192.168.50.1   192.168.50.167     55\n")
    assert nc.default_gateway(_run(out)) == ("192.168.50.1", "192.168.50.167")


def test_gateway_none_and_failure():
    assert nc.default_gateway(_run(b"nothing here")) is None

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("route", 10)
    assert nc.default_gateway(boom) is None


def test_fingerprint_needs_gateway_mac():
    arp = lambda: [("192.168.50.1", "9C-9D-7E-48-A4-1D"), ("192.168.50.41", "00-11-22-33-44-55")]
    fp = nc.fingerprint(lambda: ("192.168.50.1", "192.168.50.167"), arp)
    assert fp == {"key": "192.168.50.1/9c9d7e48a41d", "gw": "192.168.50.1", "ip": "192.168.50.167",
                  "mac": "9c9d7e48a41d"}
    assert nc.fingerprint(lambda: ("192.168.50.1", "x"), lambda: []) is None
    assert nc.fingerprint(lambda: None, arp) is None


def _fp(gw, mac="aabbccddeeff"):
    return {"key": f"{gw}/{mac}", "gw": gw, "ip": "1.1.1.1", "mac": mac}


def test_observe_first_then_confirmed_change():
    st = {}
    assert nc.observe(st, _fp("A")) == "first"
    assert nc.observe(st, _fp("A")) == "same"
    assert nc.observe(st, _fp("B")) == "pending"
    assert st["current"] == "A/aabbccddeeff"
    assert nc.observe(st, _fp("B")) == "changed"
    assert st["current"] == "B/aabbccddeeff" and "pending" not in st


def test_observe_flap_resets_and_none_is_not_change():
    st = {}
    nc.observe(st, _fp("A"))
    assert nc.observe(st, _fp("B")) == "pending"
    assert nc.observe(st, _fp("A")) == "same"            # дребезг: вернулись — счётчик сброшен
    assert nc.observe(st, _fp("B")) == "pending"
    assert nc.observe(st, None) == "none"
    assert nc.observe(st, _fp("B")) == "pending"         # «нет сети» тоже сбрасывает подтверждение
    assert nc.observe(st, _fp("B")) == "changed"


def test_poll_once_first_silent_change_reports_once(tmp_path):
    p = tmp_path / "net.json"
    seq = iter([_fp("A"), _fp("B"), _fp("B"), _fp("B")])
    calls = []
    checks = (("X", lambda fp, st: calls.append(fp["gw"]) or ["x ok"]),)
    assert nc.poll_once(lambda: next(seq), checks, p) == []          # первая сеть — молча
    assert nc.poll_once(lambda: next(seq), checks, p) == []          # ждём подтверждения
    out = nc.poll_once(lambda: next(seq), checks, p)
    assert len(out) == 1 and "Сеть сменилась: шлюз B" in out[0] and "x ok" in out[0]
    assert nc.poll_once(lambda: next(seq), checks, p) == []          # повторно не шлёт
    assert calls == ["B"]
    assert json.loads(p.read_text(encoding="utf-8"))["current"] == "B/aabbccddeeff"


def test_change_while_bot_down_reported_after_start(tmp_path):
    p = tmp_path / "net.json"
    nc.save_state({"current": "A/aabbccddeeff", "current_gw": "A"}, p)
    checks = (("X", lambda fp, st: ["ok"]),)
    assert nc.poll_once(lambda: _fp("B"), checks, p) == []
    assert len(nc.poll_once(lambda: _fp("B"), checks, p)) == 1


def test_check_failure_isolated():
    st = {}

    def bad(fp, s):
        raise RuntimeError("x")
    text = nc.run_checks(_fp("B"), st, (("Плохая", bad), ("Хорошая", lambda fp, s: ["ok"])))
    assert "⚠️ Плохая: проверка не выполнена (RuntimeError)." in text and "ok" in text


def test_poll_once_never_raises(tmp_path):
    def boom():
        raise OSError("x")
    assert nc.poll_once(boom, (), tmp_path / "n.json") == []


# --- ТВ: пинок только в домашней сети, находка делает сеть домашней ---

class FakeTV:
    DEFAULT_PORT = 1926

    def __init__(self, found=None, open_=True, cached=None):
        self.found, self.open_, self.cached = found, open_, cached or {}
        self.poke_used = None
        self.saved = None

    def poke_subnets(self):
        pass

    def find_host(self, mac, poke=None):
        self.poke_used = poke == self.poke_subnets
        return self.found

    def port_open(self, host, port, timeout=2.0):
        return self.open_

    def load_host_cache(self):
        return self.cached

    def save_host_cache(self, mac, host):
        self.saved = (mac, host)


@pytest.fixture
def tv(monkeypatch):
    def make(**kw):
        t = FakeTV(**kw)
        monkeypatch.setattr(nc, "_tv", lambda: t)
        monkeypatch.setenv("TV_MAC", "D4-0A-DC-4D-F6-2C")
        return t
    return make


def test_tv_foreign_net_no_poke_not_alarm(tv):
    t = tv(found=None)
    st = {}
    assert nc.check_tv(_fp("B"), st) == ["📺 ТВ: вне дома — недоступен, это нормально."]
    assert t.poke_used is False


def test_tv_home_net_poke_and_warn(tv):
    t = tv(found=None)
    st = {"home": ["B/aabbccddeeff"]}
    assert "в домашней сети не найден" in nc.check_tv(_fp("B"), st)[0]
    assert t.poke_used is True


def test_tv_found_saves_and_marks_home(tv):
    t = tv(found="192.168.50.77", cached={"host": "10.133.133.41"})
    st = {}
    assert nc.check_tv(_fp("B"), st) == ["📺 ТВ: на связи, 192.168.50.77 (был 10.133.133.41)"]
    assert t.saved == ("D4-0A-DC-4D-F6-2C", "192.168.50.77")
    assert nc.is_home(st, "B/aabbccddeeff")


def test_tv_no_mac_silent(monkeypatch):
    monkeypatch.delenv("TV_MAC", raising=False)
    assert nc.check_tv(_fp("B"), {}) == []


# --- SmartThings: заметки OAuth не теряются, 429 — не авария ---

def _dw(monkeypatch, fetch, notes=(), token="t"):
    class RateLimited(Exception): pass
    class AuthFailed(Exception): pass
    class Transport(Exception): pass
    m = types.SimpleNamespace(enabled=lambda: True, current_token=lambda: (token, list(notes)),
                              RateLimited=RateLimited, AuthFailed=AuthFailed, Transport=Transport)
    m.fetch_snapshots = lambda tok: fetch(m)
    monkeypatch.setitem(sys.modules, "device_watch", m)
    return m


def test_smartthings_ok_keeps_oauth_notes(monkeypatch):
    _dw(monkeypatch, lambda m: [1, 2], notes=["✅ восстановлен"])
    assert nc.check_smartthings(_fp("B"), {}) == ["✅ восстановлен", "🧺 SmartThings: облако отвечает, машин: 2."]


def test_smartthings_429_and_transport(monkeypatch):
    def rl(m):
        raise m.RateLimited("429")
    _dw(monkeypatch, rl)
    assert "(429)" in nc.check_smartthings(_fp("B"), {})[0]

    def tr(m):
        raise m.Transport("URLError: x")
    _dw(monkeypatch, tr)
    assert nc.check_smartthings(_fp("B"), {})[0].startswith("⚠️ SmartThings: облако недоступно")


# --- принтер ---

def test_printer_home_vs_away(monkeypatch, tv):
    monkeypatch.setitem(sys.modules, "doc_print", types.SimpleNamespace(printer_host=lambda: "192.168.50.41"))
    tv(open_=False)
    assert "вне дома" in nc.check_printer(_fp("B"), {})[0]
    assert "не отвечает (192.168.50.41)" in nc.check_printer(_fp("B"), {"home": ["B/aabbccddeeff"]})[0]
    tv(open_=True)
    assert nc.check_printer(_fp("B"), {}) == ["🖨 Принтер: на связи, 192.168.50.41"]
    monkeypatch.setitem(sys.modules, "doc_print", types.SimpleNamespace(printer_host=lambda: None))
    assert nc.check_printer(_fp("B"), {}) == []


def test_header_reflects_home_marked_by_checks():
    st = {}
    def marks(fp, s):
        nc.mark_home(s, fp["key"])
        return ["📺 ТВ: на связи"]
    text = nc.run_checks(_fp("B"), st, (("ТВ", marks),))
    assert text.splitlines()[0].endswith("— домашняя.")
    assert text.splitlines()[1] == "📺 ТВ: на связи"
