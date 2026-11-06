"""tv_bot_handler: запасной путь через смысл (tv_intent) и честный текст «разные сети» (30.10.2026)."""
import pytest

H = pytest.importorskip("tv_bot_handler")
import tv_intent
from tv_control import TVTransportError


class FakeTV:
    host = "10.17.115.41"

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def rec(*a, **k):
            self.calls.append((name, a))
            return {"power": "On"} if name == "get_ambilight_power" else \
                {"styleName": "FOLLOW_AUDIO", "menuSetting": "ENERGY_ADAPTIVE_BRIGHTNESS"} if name == "get_ambilight_config" else {}
        return rec


def call(text, tv, monkeypatch, understood):
    monkeypatch.setattr(tv_intent, "understand", lambda t: understood)
    return H.handle(1, text, lambda: tv, admin_ids=[1], pending=H.PendingStore(), now=0.0)


def test_rules_first_model_not_called(monkeypatch):
    called = []
    monkeypatch.setattr(tv_intent, "understand", lambda t: called.append(t))
    tv = FakeTV()
    H.handle(1, "подсветка музыка", lambda: tv, admin_ids=[1], pending=H.PendingStore(), now=0.0)
    assert called == [] and ("set_ambilight_style", ("FOLLOW_AUDIO", "ENERGY_ADAPTIVE_BRIGHTNESS")) in tv.calls


def test_model_understood_runs_and_says_how(monkeypatch):
    tv = FakeTV()
    out = call("включи свет, это, под звуковое сопровождение", tv, monkeypatch, "подсветка музыка")
    assert out.startswith("Понял как «подсветка музыка». ")
    assert ("set_ambilight_style", ("FOLLOW_AUDIO", "ENERGY_ADAPTIVE_BRIGHTNESS")) in tv.calls


def test_model_not_understood_keeps_old_answer(monkeypatch):
    out = call("свари кофе", FakeTV(), monkeypatch, None)
    assert out.startswith("Не выполнено")


def test_model_standby_still_asks_confirmation(monkeypatch):
    tv = FakeTV()
    store = H.PendingStore()
    monkeypatch.setattr(tv_intent, "understand", lambda t: "выключи телевизор")
    out = H.handle(1, "гаси всё, спать", lambda: tv, admin_ids=[1], pending=store, now=0.0)
    assert "подтвержд" in out.lower() and tv.calls == [] and store.pending_for(1)


def test_guest_never_reaches_model(monkeypatch):
    called = []
    monkeypatch.setattr(tv_intent, "understand", lambda t: called.append(t))
    out = H.handle(2, "включи свет", lambda: FakeTV(), admin_ids=[1], pending=H.PendingStore(), now=0.0)
    assert "только администратору" in out and called == []


class _Cause(Exception):
    pass


ConnectTimeout = type("ConnectTimeout", (Exception,), {})


def _timeout():
    e = TVTransportError("POST input/key: timeout")
    e.__cause__ = ConnectTimeout("x")
    return e


def test_other_network_hint(monkeypatch):
    monkeypatch.setattr(H, "_other_network", lambda host: ("10.17.115.x", "192.168.50.x"))
    hint = H.network_hint(_timeout(), "10.17.115.41")
    assert "Сеть ПК сейчас 192.168.50.x" in hint and "10.17.115.x" in hint and "разные сети" in hint
    assert H.network_hint(TVTransportError("read"), "10.17.115.41") == ""      # не сбой соединения — молчим


def test_same_network_no_hint(monkeypatch):
    monkeypatch.setattr(H, "_other_network", lambda host: None)
    assert H.network_hint(_timeout(), "192.168.50.182") == ""


def test_execute_appends_hint(monkeypatch):
    monkeypatch.setattr(H, "LOCAL_IPS", lambda: ["192.168.50.167"])
    class TV:
        host = "10.17.115.41"
        def send_key(self, k):
            raise _timeout()
    from tv_commands import parse
    out = H._execute(parse("вверх"), lambda: TV())
    assert "не отвечает по адресу 10.17.115.41" in out and "Сеть ПК сейчас 192.168.50.x" in out


def test_other_network_detection():
    assert H._other_network("10.17.115.41", lambda: ["192.168.50.167"]) == ("10.17.115.x", "192.168.50.x")
    assert H._other_network("192.168.50.182", lambda: ["192.168.50.167"]) is None
    assert H._other_network(None, lambda: ["192.168.50.167"]) is None
    assert H._other_network("10.17.115.41", lambda: []) is None
    def boom():
        raise OSError
    assert H._other_network("10.17.115.41", boom) is None


@pytest.mark.parametrize("text", ["запусти ютуб"])      # 03.11: «включи телевизор» -- уже не отказ (Wake-on-LAN)
def test_refusal_not_sent_to_model(monkeypatch, text):
    called = []
    monkeypatch.setattr(tv_intent, "understand", lambda t: called.append(t) or "статус")
    out = H.handle(1, text, lambda: FakeTV(), admin_ids=[1], pending=H.PendingStore(), now=0.0)
    assert out.startswith("Не выполнено") and called == []


def test_nav_refusal_not_sent_to_model(monkeypatch):
    nav = pytest.importorskip("tv_nav")
    if not hasattr(nav, "NavError"):
        pytest.skip("заглушка tv_nav")
    called = []
    monkeypatch.setattr(tv_intent, "understand", lambda t: called.append(t) or "вниз 9")
    out = H.handle(1, "вниз 99", lambda: FakeTV(), admin_ids=[1], pending=H.PendingStore(), now=0.0)
    assert out.startswith("Не выполнено") and called == []


def test_real_model_never_called_under_pytest(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:14b-instruct-q4_K_M")
    monkeypatch.setattr(tv_intent, "_ollama", lambda text: (_ for _ in ()).throw(AssertionError("вызов Ollama")))
    assert tv_intent.understand("включи свет") is None


def test_power_on_by_rules_not_by_model(monkeypatch):
    """03.11: «включи телевизор» -- правило (пакет пробуждения), модель смысла не вызывается."""
    called = []
    monkeypatch.setattr(tv_intent, "understand", lambda t: called.append(t) or "статус")
    tv = FakeTV()
    out = H.handle(1, "включи телевизор", lambda: tv, admin_ids=[1], pending=H.PendingStore(), now=0.0)
    assert out.startswith("Отправил") and called == [] and ("wake", ()) in tv.calls
