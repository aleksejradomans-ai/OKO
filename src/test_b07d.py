"""(16.11, фаза 2, 2c-fix): пары (s_exp, s_obs) по setting_N и изоляция volume в счётчике журнала Σ.

(а) Запись настройки меню ТВ (set_setting_value / set_setting_enum) читает значение после записи ВНУТРИ клиента
    (TVClient._write_setting -> read_settings) -- мимо обёртки _ShadowTV, поэтому у amb_step / amb_brighter
    (setting_710) в outcome было s_exp без s_obs. Теперь: на время одного вызова записи у экземпляра клиента стоит
    прокладка на _write_setting; вызовы ТВ, их порядок, аргументы, исключения и ответы -- те же.
(б) tools/sigma_shadow_stats.py: признак volume изолирован (внешний аудиовыход) -- не входит в error_commands, пары,
    T_theta, tau_b, pi_0 и пороги; отдельная строка в выводе; значения не печатаются.
Сеть не затрагивается: клиент ТВ -- настоящий TVClient с подменённым транспортом _call, модель смысла заглушена.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import tv_bot_handler as H
import tv_intent
from tv_control import TVClient, TVTransportError

ADMIN = 4242
_TOOL = Path(__file__).resolve().parent.parent / "tools" / "sigma_shadow_stats.py"
_spec = importlib.util.spec_from_file_location("sigma_shadow_stats_b07d", _TOOL)
sss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sss)


class WireTV(TVClient):
    """Настоящий клиент API v6, транспорт -- в памяти. Пишет каждый вызов _call (метод, путь, тело)."""

    def __init__(self, accept: bool = True, start: int = 4, net_on_read_after_write: bool = False) -> None:
        super().__init__(host="192.0.2.10", username="u", password="p", session=object())
        self.wire: list[tuple[str, str, str]] = []
        self.accept = accept
        self.net_after = net_on_read_after_write
        self.state = {710: {"value": start}, 720: {"value": 0},
                      320: {"selected_item": 300, "enum_values": [{"enum_id": 300}, {"enum_id": 321}]}}
        self._wrote = False

    def _call(self, method, path, payload=None, want_json=None):
        self.wire.append((method, path, json.dumps(payload, sort_keys=True)))
        if path == "menuitems/settings/current":
            if self._wrote and self.net_after:
                raise TVTransportError("нет сети")
            vals = [{"value": {"Nodeid": n["nodeid"], "Controllable": True, "Available": True,
                               "data": dict(self.state[n["nodeid"]])}} for n in payload["nodes"]
                    if n["nodeid"] in self.state]
            return {"values": vals}
        if path == "ambilight/currentconfiguration" and method == "GET":   # B20: режим «за картинкой» -> ползунок 710
            return {"styleName": "FOLLOW_VIDEO", "menuSetting": "STANDARD"}
        if path == "menuitems/settings/update":
            v = payload["values"][0]["value"]
            self._wrote = True
            if self.accept:
                self.state[v["Nodeid"]] = dict(v["data"])
            return {}
        if path == "ambilight/power":
            return {"power": "On"} if method == "GET" else None
        return {}


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "SHADOW_DEFAULT_PATH", tmp_path / "default" / "sigma_shadow.jsonl")
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(tmp_path / "j" / "sigma_shadow.jsonl"))
    monkeypatch.delenv(H.SHADOW_MAX_ENV, raising=False)
    monkeypatch.delenv("OKO_COLLECTOR", raising=False)
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")
    monkeypatch.setenv("TV_SETTING_SETTLE_S", "0")
    monkeypatch.setattr(tv_intent, "understand", lambda *a, **k: None)
    monkeypatch.setattr(H, "VERIFY", {})
    monkeypatch.setattr(H, "PRE", {})
    monkeypatch.setattr(H, "_stop_music", lambda name: "")
    H._SHADOW_WARNED.clear()


def _journal() -> Path:
    return Path(os.environ[H.SHADOW_PATH_ENV])


def _rows() -> list[dict]:
    p = _journal()
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def _play(monkeypatch, flag, script, make=WireTV):
    if flag is None:
        monkeypatch.delenv(H.SHADOW_FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(H.SHADOW_FLAG_ENV, flag)
    tvs = []

    def factory():
        tv = make()
        tvs.append(tv)
        return tv
    store = H.PendingStore()
    answers = [H.handle(ADMIN, t, factory, pending=store, admin_ids=[ADMIN], now=float(i))
               for i, t in enumerate(script)]
    return tvs, answers


def _outcomes() -> list[dict]:
    return [r for r in _rows() if r["step"] == "outcome"]


BRIGHT = ["подсветка ярче", "подсветка яркость плюс 2", "подсветка тусклее", "подсветка яркость 7"]


# --- (а) пары по setting_N ----------------------------------------------------------------------------------------

def test_b07d_setting_710_pair_in_outcome(monkeypatch):
    tvs, answers = _play(monkeypatch, "1", BRIGHT)
    outs = _outcomes()
    assert len(outs) == len(BRIGHT)
    acts = [o.get("action") for o in outs]
    assert "amb_brighter" in acts and "amb_step" in acts
    for o, tv in zip(outs, tvs):
        assert o["feature"] == "setting_710" and o["outcome"] == "PASS"
        assert o["s_exp"] == o["s_obs"] == tv.state[710]["value"]      # записанное = считанное после записи
    # readback после записи идёт после ack своей команды
    for seq in {o["seq"] for o in outs}:
        steps = [r["step"] for r in _rows() if r["seq"] == seq]
        assert steps.index("ack") < len(steps) - 1 - steps[::-1].index("readback")


def test_b07d_same_wire_calls_and_answers_on_off(monkeypatch):
    script = BRIGHT + ["подсветка авто", "подсветка яркость 10", "подсветка ярче"]
    tv_off, ans_off = _play(monkeypatch, None, script)
    assert not _journal().exists()
    tv_on, ans_on = _play(monkeypatch, "1", script)
    assert [t.wire for t in tv_on] == [t.wire for t in tv_off] and sum(len(t.wire) for t in tv_off) > 10
    assert ans_on == ans_off
    assert all("_write_setting" not in vars(t) for t in tv_on)       # прокладка снята после вызова


def test_b07d_rejected_write_is_fail_verify_with_pair(monkeypatch):
    def reject():
        return WireTV(accept=False)
    tv_off, ans_off = _play(monkeypatch, None, ["подсветка ярче"], make=reject)
    tvs, ans = _play(monkeypatch, "1", ["подсветка ярче"], make=reject)
    assert ans == ans_off and [t.wire for t in tvs] == [t.wire for t in tv_off]
    (o,) = _outcomes()
    assert o["outcome"] == "FAIL" and o["err"] == "verify_fail" and o["feature"] == "setting_710"
    assert o["s_exp"] == 5 and o["s_obs"] == 4                       # ТВ остался на 4 при записи 5
    assert "_write_setting" not in vars(tvs[0])


def test_b07d_net_on_readback_no_pair_net_error(monkeypatch):
    def flaky():
        return WireTV(net_on_read_after_write=True)
    tv_off, ans_off = _play(monkeypatch, None, ["подсветка ярче"], make=flaky)
    tvs, ans = _play(monkeypatch, "1", ["подсветка ярче"], make=flaky)
    assert ans == ans_off and [t.wire for t in tvs] == [t.wire for t in tv_off]
    (o,) = _outcomes()
    assert o["outcome"] == "FAIL" and o["err"] == "net" and "s_exp" not in o     # пары нет (чтения после записи нет)
    assert "_write_setting" not in vars(tvs[0])


def test_b07d_enum_setting_pair(monkeypatch):
    tvs, _ = _play(monkeypatch, "1", ["подсветка авто"])
    (o,) = _outcomes()
    assert o["action"] == "amb_ai" and o["feature"] == "setting_320" and o["outcome"] == "PASS"
    assert o["s_exp"] == o["s_obs"] == 321


def test_b07d_at_limit_no_write_no_pair(monkeypatch):
    """У предела записи нет (команда только читает) -> пары нет, есть только считанное."""
    tvs, _ = _play(monkeypatch, "1", ["подсветка ярче"], make=lambda: WireTV(start=10))
    assert not any(c[1] == "menuitems/settings/update" for c in tvs[0].wire)
    (o,) = _outcomes()
    assert o["feature"] == "setting_710" and "s_exp" not in o and o["s_obs"] == 10


def test_b07d_client_without_write_setting_unchanged():
    """Двойник без _write_setting (как FakeTV в test_b07b) -- прокладка не ставится, путь прежний."""
    class Plain:
        def __init__(self):
            self.calls = []

        def set_setting_value(self, node, value, lo, hi):
            self.calls.append((node, value))
            return value
    run = H._ShadowRun(H.Command(kind="verified", name="amb_step", run=lambda tv: ""))
    tv = Plain()
    assert run.call("set_setting_value", tv.set_setting_value, (710, 3, 0, 10), {}, tv) == 3
    assert tv.calls == [(710, 3)] and run.s_exp == 3 and run.s_obs is None and "_write_setting" not in vars(tv)


def test_b07d_hook_errors_do_not_touch_command(monkeypatch):
    tv_off, ans_off = _play(monkeypatch, None, BRIGHT)

    def boom(*a, **k):
        raise RuntimeError("hook")
    monkeypatch.setattr(H, "_hook_readback", boom)
    tv_on, ans_on = _play(monkeypatch, "1", BRIGHT)
    assert ans_on == ans_off and [t.wire for t in tv_on] == [t.wire for t in tv_off]
    assert all(o["outcome"] == "PASS" and "s_obs" not in o for o in _outcomes())   # без замера -- как до «изоляция volume»


def test_b07d_hook_internal_failure_is_silent():
    class Odd(WireTV):
        __slots__ = ()
    tv = Odd()
    object.__setattr__(tv, "_write_setting", tv._write_setting)       # уже подменено кем-то -> не трогаем
    box: dict = {}
    assert H._hook_readback(tv, "value", box) is None
    assert H._hook_readback(object(), "value", box) is None             # без __dict__


def test_b07d_rows_whitelist_only(monkeypatch):
    _play(monkeypatch, "1", BRIGHT + ["подсветка авто"])
    _play(monkeypatch, "1", ["подсветка ярче"], make=lambda: WireTV(accept=False))
    raw = _journal().read_text(encoding="utf-8")
    for r in _rows():
        assert set(r) <= H.SHADOW_FIELDS and H.shadow_validate(r) == r
    assert "192.0.2" not in raw and "подсвет" not in raw
    # идентификатор -- по значениям полей, не по сырому тексту (в метке времени «4242» встречается случайно)
    assert not any(str(ADMIN) in str(v) for r in _rows() for k, v in r.items() if k not in ("ts", "seq", "latency_ms"))
    assert not any(k in ("id", "user", "user_id", "chat_id") or k.endswith("_id") for r in _rows() for k in r)


def test_b07d_journal_pairs_counted_by_stats(monkeypatch):
    _play(monkeypatch, "1", BRIGHT)
    _play(monkeypatch, "1", ["подсветка ярче"], make=lambda: WireTV(accept=False))
    st = sss.compute(_journal(), sss.ISOLATE_DEFAULT)
    assert st["feature_pairs"]["setting_710"] == {"pairs": len(BRIGHT) + 1, "non_pass": 1}
    assert st["err"]["verify_fail"] == 1 and st["error_commands"] == 1


# --- (б) volume изолирован в счётчике -----------------------------------------------------------------------------

SEXP, SOBS, TS0 = 987654321, 876543219, 1794700000123


def _cmd(seq, ts, action, feature, outcome="PASS", err=None):
    rows = [{"ts": ts, "step": "send", "seq": seq, "action": action, "feature": feature, "s_exp": SEXP},
            {"ts": ts + 1, "step": "ack", "seq": seq, "action": action, "feature": feature},
            {"ts": ts + 2, "step": "readback", "seq": seq, "action": action, "feature": feature, "s_obs": SOBS}]
    o = {"ts": ts + 3, "step": "outcome", "seq": seq, "action": action, "feature": feature, "outcome": outcome,
         "s_exp": SEXP, "s_obs": SOBS}
    if err:
        o["err"] = err
    return rows + [o]


def _vol_journal(tmp_path) -> Path:
    p = tmp_path / "sigma_shadow.jsonl"
    rows = []
    for i in range(12):
        rows += _cmd(i + 1, TS0 + i * 1000, "volume_up", "volume", "FAIL", "verify_fail")
    rows += _cmd(100, TS0 + 50_000, "amb_step", "setting_710", "FAIL")             # other
    rows += _cmd(101, TS0 + 51_000, "amb_brighter", "setting_710")
    rows.append({"ts": TS0 + 60_000, "step": "error", "seq": 200, "err": "net"})
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def test_b07d_volume_isolated_counts(tmp_path):
    st = sss.compute(_vol_journal(tmp_path), sss.ISOLATE_DEFAULT)
    assert st["isolated"] == {"volume": {"reason": "внешний аудиовыход", "commands": 12,
                                         "outcome": {"PASS": 0, "WARN": 0, "FAIL": 12}, "readback": 12}}
    assert st["error_commands"] == 2 and st["err"]["verify_fail"] == 0 and st["err"]["other"] == 1
    assert st["err"]["net"] == 1
    assert "volume" not in st["feature_pairs"] and st["feature_pairs"]["setting_710"] == {"pairs": 2, "non_pass": 1}
    assert not any(k.startswith("volume|") for k in st["feature_action_cmds"])
    assert "volume" not in st["tau_b_pairs"] and st["readback"]["rows"] == 2
    assert all(t["key"] != "volume" and not t["key"].startswith("volume|") for t in st["thresholds"])
    # описательные счётчики -- как раньше, по всем строкам
    assert st["commands_seq"] == 15 and st["action_outcome"]["volume_up"]["FAIL"] == 12


def test_b07d_raw_compute_unchanged_without_isolate(tmp_path):
    st = sss.compute(_vol_journal(tmp_path))
    assert st["isolated"] == {} and st["error_commands"] == 14 and st["err"]["verify_fail"] == 12
    assert st["feature_pairs"]["volume"] == {"pairs": 12, "non_pass": 12}


def test_b07d_render_isolated_line_counters_only(tmp_path):
    st = sss.compute(_vol_journal(tmp_path), sss.ISOLATE_DEFAULT)
    text = sss.render(st) + json.dumps(st, ensure_ascii=True)
    assert "volume: изолирован (внешний аудиовыход) commands=12" in sss.render(st)
    for secret in (str(SEXP), str(SOBS), str(TS0)):
        assert secret not in text


def test_b07d_cli_isolates_by_default(tmp_path):
    p = _vol_journal(tmp_path)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    r = subprocess.run([sys.executable, str(_TOOL), "--path", str(p)], capture_output=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr[-400:]
    out = r.stdout.decode("utf-8")
    assert "volume: изолирован (внешний аудиовыход)" in out and "error_commands=2" in out
    r = subprocess.run([sys.executable, str(_TOOL), "--path", str(p), "--no-isolate", "--json"],
                       capture_output=True, timeout=60, env=env)
    js = json.loads(r.stdout.decode("ascii"))
    assert r.returncode == 0 and js["error_commands"] == 14 and js["isolated"] == {}
