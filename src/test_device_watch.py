"""Тесты каркаса уведомлений техники (28.10.2026). Сеть не используется: облако подменено."""
import re

import pytest

import device_watch as D

S = D.Snapshot


def snap(kind, machine, cycle="Table_02_Course_1B", completion=None):
    return S(kind=kind, label=kind, machine=machine, cycle=cycle, completion=completion)


def run_seq(states, kind="washer", cycle="Table_02_Course_1B"):
    w = D.Watcher()
    out = []
    for st in states:
        out.append(w.feed([snap(kind, st, cycle)]))
    return out


def test_first_poll_is_baseline_only():
    """Перезапуск бота посреди стирки не должен слать «запущена»/«закончила»."""
    assert run_seq(["run"]) == [[]]
    assert run_seq(["stop"]) == [[]]


def test_full_cycle_events_in_russian_without_codes():
    ev = run_seq(["stop", "run", "run", "pause", "run", "stop", "stop", "stop"])
    flat = [e for step in ev for e in step]
    assert flat == [
        "🧺 Стиральная: запущена «Хлопок».",
        "🧺 Стиральная: пауза («Хлопок»).",
        "🧺 Стиральная: продолжает «Хлопок».",
        "🧺 Стиральная: закончила «Хлопок».",
    ]
    for e in flat:
        assert not re.search(r"Table_|Course|\b1B\b", e)


def test_finish_needs_two_stop_polls():
    """Одиночный stop между фазами (не измерено, что его нет) не даёт ложного «закончила»."""
    ev = run_seq(["stop", "run", "stop", "run", "stop", "stop"])
    flat = [e for step in ev for e in step]
    assert flat.count("🧺 Стиральная: закончила «Хлопок».") == 1
    assert "запущена" in flat[0] and len([e for e in flat if "запущена" in e]) == 1


def test_dryer_names_and_title():
    ev = run_seq(["stop", "run"], kind="dryer", cycle="Table_03_Course_27")
    assert ev[1] == ["♨️ Сушильная: запущена «Сушка по времени»."]


def test_completion_time_shown():
    w = D.Watcher()
    w.feed([snap("washer", "stop")])
    out = w.feed([snap("washer", "run", completion="2026-10-28T14:05:00Z")])
    assert re.search(r"закончит около \d\d:\d\d\.$", out[0])


def test_unknown_code_does_not_leak():
    ev = run_seq(["stop", "run"], cycle="Table_02_Course_FF")
    assert "FF" not in ev[1][0] and "неизвестная программа" in ev[1][0]


class FakeCloud:
    def __init__(self):
        self.mode = "ok"
        self.machine = "stop"

    def __call__(self, path, token):
        if self.mode == "auth":
            raise D.AuthFailed("HTTP 401")
        if self.mode == "429":
            raise D.RateLimited("429")
        if self.mode == "net":
            raise D.Transport("down")
        if path == "/devices":
            return {"items": [{"deviceId": "w", "label": "W", "ocf": {"ocfDeviceType": "oic.d.washer"}}]}
        return {"components": {"main": {
            "washerOperatingState": {"machineState": {"value": self.machine}},
            "samsungce.washerCycle": {"washerCycle": {"value": "Table_02_Course_1B"}}}}}


def test_auth_failure_reported_once_then_recovery():
    c = FakeCloud()
    m = D.Monitor(token="t", get=c)
    assert m.poll_once() == []                 # базовый снимок
    c.mode = "auth"
    first = m.poll_once()
    assert len(first) == 1 and "токен" in first[0]
    assert m.poll_once() == [] and m.poll_once() == []
    c.mode = "ok"
    assert m.poll_once() == ["✅ Техника: связь с облаком SmartThings восстановлена."]


def test_rate_limit_is_silent_and_does_not_lose_transition():
    c = FakeCloud()
    m = D.Monitor(token="t", get=c)
    m.poll_once()
    c.mode, c.machine = "429", "run"
    assert m.poll_once() == []
    c.mode = "ok"
    assert m.poll_once() == ["🧺 Стиральная: запущена «Хлопок»."]


def test_transport_alert_after_threshold_once():
    c = FakeCloud()
    m = D.Monitor(token="t", get=c)
    m.poll_once()
    c.mode = "net"
    outs = [m.poll_once() for _ in range(D.TRANSPORT_ALERT_AFTER + 3)]
    alerts = [o for o in outs if o]
    assert len(alerts) == 1 and "недоступно" in alerts[0][0]


def test_poll_interval_has_floor(monkeypatch):
    monkeypatch.setenv("OKO_DEVICE_POLL_S", "5")
    assert D.poll_interval_s() == D.POLL_MIN_S
    monkeypatch.setenv("OKO_DEVICE_POLL_S", "мусор")
    assert D.poll_interval_s() == 60.0


def test_disabled_without_token(monkeypatch, tmp_path):
    monkeypatch.delenv("OKO_SMARTTHINGS_TOKEN", raising=False)
    monkeypatch.setenv("OKO_ST_TOKENS", str(tmp_path / "нет.json"))   # 29.10: файла OAuth тоже нет
    assert D.enabled() is False and D.poll_once() == []


def test_baseline_is_logged_once(caplog):
    """Контроль работы задачи: без этой строки в журнале «тихо» и «не запущено» неотличимы."""
    import logging
    caplog.set_level(logging.INFO, logger="oko.devices")
    w = D.Watcher()
    w.feed([snap("washer", "stop"), snap("dryer", "stop", "Table_03_Course_51")])
    w.feed([snap("washer", "stop")])
    lines = [r.getMessage() for r in caplog.records if "базовый снимок" in r.getMessage()]
    assert lines == ["[DEVICES] базовый снимок: washer=stop, dryer=stop"]


# --- живой замер 28.10 12:51–12:57: «Полоскание + отжим» остановлена кнопкой -------------------------------
from datetime import datetime, timezone

END = "2026-10-28T11:07:00Z"           # «закончит около 13:07» по Риге


def _replay(now_utc):
    w = D.Watcher(now=lambda: now_utc)
    seq = [("stop", "Table_02_Course_1C", None), ("run", "Table_02_Course_27", END),
           ("pause", "Table_02_Course_27", END),
           ("stop", "Table_02_Course_1C", None), ("stop", "Table_02_Course_1C", None)]   # после выключения — сброс на 1C
    out = []
    for m, c, e in seq:
        out += w.feed([S(kind="washer", label="w", machine=m, cycle=c, completion=e)])
    return out


def test_live_abort_named_by_running_program_not_reset_one():
    out = _replay(datetime(2026, 10, 28, 10, 57, tzinfo=timezone.utc))
    assert out[0].startswith("🧺 Стиральная: запущена «Полоскание + отжим»")
    assert out[1] == "🧺 Стиральная: пауза («Полоскание + отжим»)."
    assert re.fullmatch(r"🧺 Стиральная: остановлена до окончания «Полоскание \+ отжим» \(по плану до \d\d:\d\d\)\.", out[2])
    assert "Эко" not in out[2]


def test_real_finish_after_planned_end_is_finished():
    out = _replay(datetime(2026, 10, 28, 11, 8, tzinfo=timezone.utc))
    assert out[2] == "🧺 Стиральная: закончила «Полоскание + отжим»."


def test_finish_within_margin_is_finished():
    """Облако считает время окончания приблизительно: stop за 2 мин до плана — это окончание, не обрыв."""
    out = _replay(datetime(2026, 10, 28, 11, 5, tzinfo=timezone.utc))
    assert "закончила" in out[2]


def test_restart_mid_program_keeps_program_for_finish():
    w = D.Watcher(now=lambda: datetime(2026, 10, 28, 11, 10, tzinfo=timezone.utc))
    w.feed([S(kind="washer", label="w", machine="run", cycle="Table_02_Course_27", completion=END)])  # базовый снимок
    w.feed([S(kind="washer", label="w", machine="stop", cycle="Table_02_Course_1C", completion=None)])
    out = w.feed([S(kind="washer", label="w", machine="stop", cycle="Table_02_Course_1C", completion=None)])
    assert out == ["🧺 Стиральная: закончила «Полоскание + отжим»."]


# --- 29.10: OAuth первым, PAT запасной, детектор не сбрасывается при смене токена ---
import smartthings_oauth as O


class _OAuthStub:
    OAuthError = O.OAuthError

    def __init__(self, result):
        self.result = result
        self.stored = None

    def get_token(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def load(self):
        return self.stored

    def token_path(self):
        from pathlib import Path
        return Path("нет")


@pytest.fixture
def oauth(monkeypatch):
    monkeypatch.setattr(D, "_OAUTH_BROKEN", False)
    monkeypatch.setattr(D, "_MONITOR", None)

    def use(result, pat="PAT"):
        stub = _OAuthStub(result)
        monkeypatch.setattr(D, "_oauth", lambda: stub)
        if pat:
            monkeypatch.setenv("OKO_SMARTTHINGS_TOKEN", pat)
        else:
            monkeypatch.delenv("OKO_SMARTTHINGS_TOKEN", raising=False)
        return stub
    return use


def test_oauth_token_preferred(oauth):
    oauth("ACCESS")
    assert D.current_token() == ("ACCESS", [])


def test_oauth_not_configured_uses_pat(oauth):
    oauth(None)
    assert D.current_token() == ("PAT", [])


def test_oauth_auth_broken_falls_back_once_then_restored(oauth):
    stub = oauth(O.OAuthError("HTTP 400 invalid_grant", auth=True))
    tok, notes = D.current_token()
    assert tok == "PAT" and len(notes) == 1 and "повторный вход" in notes[0] and "токене из .env" in notes[0]
    assert D.current_token() == ("PAT", [])                     # без повторов
    stub.result = "NEW"
    assert D.current_token() == ("NEW", ["✅ Техника: вход SmartThings (OAuth) восстановлен."])


def test_oauth_broken_without_pat_says_stopped(oauth):
    oauth(O.OAuthError("x", auth=True), pat=None)
    tok, notes = D.current_token()
    assert tok == "" and "остановлены" in notes[0]


def test_oauth_network_uses_still_alive_access(oauth):
    import time
    stub = oauth(O.OAuthError("сеть: TimeoutError", auth=False))
    stub.stored = {"access_token": "OLD", "expires_at": time.time() + 1800}
    assert D.current_token() == ("OLD", [])
    stub.stored = {"access_token": "OLD", "expires_at": time.time() - 1}
    assert D.current_token() == ("PAT", [])


def test_token_rotation_keeps_detector_state(oauth, monkeypatch):
    stub = oauth("A1")
    seen = []

    def get(path, token):
        seen.append(token)
        if path == "/devices":
            return {"items": [{"deviceId": "w", "label": "w", "ocf": {"ocfDeviceType": "oic.d.washer"}}]}
        return {"components": {"main": {"washerOperatingState": {"machineState": {"value": "run"}}}}}

    monkeypatch.setattr(D, "_http_get", get)
    D._MONITOR = D.Monitor(token="A1", get=get)
    D.poll_once()
    w = D._MONITOR.watcher
    stub.result = "A2"
    D.poll_once()
    assert D._MONITOR.watcher is w and w.started and seen[-1] == "A2"
