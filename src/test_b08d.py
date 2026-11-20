"""(16.11, фаза 3, этап 3d): шлюз Σ перед отправкой команды ТВ (src/sigma_gate.py + tv_bot_handler._execute).

Этап 3d п.1–7; решения Оператора 14:51 (fail-safe), 16:16 (λ_0), 18:21 (заморозка D при A_t = 1),
20:23 (Active Probing); реестр проверок: R-01, R-09, защёлка A_t, длина серии, заморозка D при A_t = 1. Каждый пункт -- свой тест:
  P1  guarded_decide_event до отправки; один predict_belief на действие (R-01); seq строго растёт (рестарт, перевод
      часов, ротация, потеря состояния -- R-09 + C5 ); A_t и счётчики в состоянии службы, восстановление;
  P2  fail-safe: исключение ядра / NO_DATA / Δt < 0 / SafetyGate -> HALT; N_hyp < 2, зазор < Θ_margin -> CLARIFY;
      C_state < Θ_state -> OBSERVE; повреждённое состояние / смена параметров -> HALT;
  P3  Active Probing (OKO_SIGMA_LIVE=1): при A_t = 1 ни одной записи в ТВ, только чтение ≤ R_t; 4 сбоя -> 5 удачных
      чтений -> снята на 5-м; 4 удачных + 1 неудачное -> держится, счёт заново; длина серии (длинная серия -> до 8);
  P4  OKO_SIGMA_LIVE=0 / не задан -> поведение для пользователя НЕ меняется (ответы и вызовы ТВ байт-в-байт);
      флаг -- только из окружения; LIVE=1 и HALT -> команда не уходит;
  P5  счётчики n(x', a, y) и m(r) (теневой режим); цель SET = s_exp;
  P6  журнал: новые поля только коды, белый список; счётчик fields_3d и метрика gate A (VERIFIED после SENT).
Сеть не затрагивается: настоящий TVClient с транспортом в памяти, модель смысла заглушена.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

import abstraction_layer as AL
import sigma_gate as G
import tv_bot_handler as H
import tv_intent
from tv_control import TVClient

ADMIN = 4343
_TOOL = Path(__file__).resolve().parent.parent / "tools" / "sigma_shadow_stats.py"
_spec = importlib.util.spec_from_file_location("sigma_shadow_stats_b08d", _TOOL)
sss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sss)


class WireTV(TVClient):
    """Настоящий клиент API v6, транспорт в памяти (как в test_b07d). accept=False -- ТВ не принимает запись."""

    def __init__(self, accept: bool = True, start: int = 4, reads: "list | None" = None) -> None:
        super().__init__(host="192.0.2.10", username="u", password="p", session=object())
        self.wire: list = []
        self.accept = accept
        self.reads = list(reads) if reads is not None else None   # сценарий чтений 710 (Active Probing)
        self.state = {710: {"value": start}}

    def _call(self, method, path, payload=None, want_json=None):
        self.wire.append((method, path, json.dumps(payload, sort_keys=True)))
        if path == "menuitems/settings/current":
            data = dict(self.state[710])
            if self.reads is not None and self.reads:
                nxt = self.reads.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                data = {"value": nxt}
            return {"values": [{"value": {"Nodeid": 710, "Controllable": True, "Available": True, "data": data}}]}
        if path == "ambilight/currentconfiguration" and method == "GET":   # B20: режим «за картинкой» -> ползунок 710
            return {"styleName": "FOLLOW_VIDEO", "menuSetting": "STANDARD"}
        if path == "menuitems/settings/update":
            v = payload["values"][0]["value"]
            if self.accept:
                self.state[v["Nodeid"]] = dict(v["data"])
            return {}
        return {}


def writes(tv: WireTV) -> list:
    return [w for w in tv.wire if w[1] != "menuitems/settings/current"]


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "SHADOW_DEFAULT_PATH", tmp_path / "default" / "sigma_shadow.jsonl")
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(tmp_path / "j" / "sigma_shadow.jsonl"))
    monkeypatch.setenv(H.SHADOW_FLAG_ENV, "1")
    monkeypatch.delenv(G.LIVE_ENV, raising=False)
    monkeypatch.delenv(G.STATE_ENV, raising=False)
    monkeypatch.delenv(G.PARAMS_ENV, raising=False)
    monkeypatch.delenv(H.SHADOW_MAX_ENV, raising=False)
    monkeypatch.delenv("OKO_COLLECTOR", raising=False)
    monkeypatch.setenv("TV_READBACK_DELAY_S", "0")
    monkeypatch.setenv("TV_SETTING_SETTLE_S", "0")
    monkeypatch.setattr(tv_intent, "understand", lambda *a, **k: None)
    monkeypatch.setattr(H, "VERIFY", {})
    monkeypatch.setattr(H, "PRE", {})
    monkeypatch.setattr(H, "_stop_music", lambda name: "")
    monkeypatch.setattr(G, "PROBE_PAUSE_S", 0.0)
    H._SHADOW_WARNED.clear()
    G.reset()
    yield
    G.reset()


def journal() -> Path:
    return Path(os.environ[H.SHADOW_PATH_ENV])


def rows() -> list:
    p = journal()
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def outcomes() -> list:
    return [r for r in rows() if r["step"] == "outcome"]


def gate() -> G.Gate:
    return G.get_gate(H.shadow_path())


def play(script, tv, store=None):
    store = store or H.PendingStore()
    return [H.handle(ADMIN, t, lambda: tv, pending=store, admin_ids=[ADMIN], now=float(i)) for i, t in enumerate(script)]


def fail4():
    """4 сбоя подряд (ТВ не принимает запись) -> тревога A_t = 1 после учёта 4-го события."""
    tv = WireTV(accept=False)
    play(["подсветка ярче"] * 4, tv)
    g = gate()
    g.before("amb_brighter", "verified", 1, None)        # учитывает отложенное 4-е событие
    assert g.integ.alarm is True
    return g


# ===================================================================== P1
def test_b08d_P1_guarded_decide_event_before_send(monkeypatch):
    calls = []
    real = AL.guarded_decide_event

    def spy(*a, **k):
        calls.append(len(tv.wire))
        return real(*a, **k)
    monkeypatch.setattr(AL, "guarded_decide_event", spy)
    tv = WireTV()
    play(["подсветка ярче", "подсветка ярче"], tv)
    assert len(calls) == 1                                  # второе действие учитывает событие первого
    n_first = len(tv.wire) // 2
    assert calls[0] == n_first                              # вызов -- до любого вызова ТВ второй команды


def test_b08d_P1_R01_one_predict_belief_per_action(monkeypatch):
    tv = WireTV()
    play(["подсветка ярче"], tv)
    cnt = []
    real = AL.predict_belief
    monkeypatch.setattr(AL, "predict_belief", lambda *a, **k: (cnt.append(1), real(*a, **k))[1])
    play(["подсветка ярче"], tv)
    assert len(cnt) == 1                                    # sigma и sigma_p -- одни входы, один predict_belief
    o = outcomes()[-1]
    assert o["sigma"] == "HALT" and o["sigma_why"].startswith("NO_DATA_theta_intent")


def test_b08d_P1_R09_seq_strictly_increasing_restart_clock_rotation(monkeypatch):
    tv = WireTV()
    play(["подсветка ярче"] * 3, tv)
    s1 = [r["seq"] for r in rows()]
    G.reset()                                               # перезапуск службы
    t0 = G._now_ms()
    monkeypatch.setattr(G, "_now_ms", lambda: t0 - 3_600_000)   # часы переведены на час назад
    play(["подсветка тусклее"], tv)
    s2 = [r["seq"] for r in rows()][len(s1):]
    assert min(s2) > max(s1)
    G.reset()                                               # шлюз сам (без запасного счётчика процесса)
    assert gate().next_seq() > max(s2)
    monkeypatch.setenv(H.SHADOW_MAX_ENV, "200")             # ротация журнала на каждой строке
    play(["подсветка ярче"] * 2, tv)
    gate().state_path.unlink()                              # состояние потеряно -> seq из журнала (.1 + текущий)
    G.reset()
    play(["подсветка ярче"], tv)
    allseq = [json.loads(x)["seq"] for p in (journal().with_name(journal().name + ".1"), journal()) if p.exists()
              for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    last = max(allseq)
    nxt = H._SHADOW_SEQ.__next__()
    assert nxt > last and nxt > max(s2)


def test_b08d_P1_R09_duplicate_event_no_second_impulse():
    g = gate()
    g.pending = [[10, 1000.0, 1]]
    g.before(None, "verified", None, None, now=1000.0)
    d1 = g.integ.d
    g.pending = [[10, 1001.0, 1]]                           # тот же event_id (seq) повторно
    g.before(None, "verified", None, None, now=1001.0)
    assert g.integ.d == d1 and g.integ.last_event_id == 10


def test_b08d_P1_state_restored_after_restart():
    g = fail4()
    path = g.state_path
    assert path.parent == journal().parent and path.name == G.STATE_NAME      # состояние службы, локально
    st = json.loads(path.read_text(encoding="utf-8"))
    assert st["integrator"]["alarm"] is True and st["schema"] == G.STATE_SCHEMA
    G.reset()
    g2 = gate()
    assert g2 is not g and g2.integ.alarm is True and g2.integ.d == g.integ.d
    assert g2.n == g.n and g2.metric["fail"] == 4 and g2.last_seq == g.last_seq


def test_b08d_P1_default_state_path_next_to_live_journal():
    assert H.SHADOW_DEFAULT_PATH.name == "sigma_shadow.jsonl"
    g = G.get_gate(Path("C:/app/logs/sigma_shadow.jsonl"))
    assert g.state_path == Path("C:/app/logs/sigma_state.json")
    assert "AI" not in g.state_path.parts


# ===================================================================== P2
def test_b08d_P2_kernel_exception_halt(monkeypatch):
    tv = WireTV()
    play(["подсветка ярче"], tv)

    def boom(*a, **k):
        raise ZeroDivisionError("x")
    monkeypatch.setattr(G.Gate, "c_state", boom)
    v = gate().before("amb_brighter", "verified", 1, None)
    assert v.outcome == "HALT" and v.proposal == "HALT"


def test_b08d_P2_no_data_threshold_halt():
    tv = WireTV()
    play(["подсветка ярче"] * 2, tv)
    v = gate().before("amb_brighter", "verified", 1, None)
    assert v.outcome == "HALT" and v.why == "NO_DATA_theta_intent"


def test_b08d_P2_negative_dt_halt_invalid_timestamp():
    g = gate()
    g.pending = [[5, 2000.0, 0]]
    g.before(None, "verified", None, None, now=2000.0)
    g.pending = [[6, 1000.0, 1]]                            # часы назад: Δt < 0
    v = g.before(None, "verified", None, None, now=1000.0)
    assert v.outcome == "HALT" and v.why == "INVALID_TIMESTAMP"
    assert g.integ.last_event_id == 5                       # состояние не изменилось


def _params_intent(g):
    from dataclasses import replace
    return replace(g.params, **G.INTENT_PROPOSAL)


def test_b08d_P2_safety_gate_halt_nhyp_clarify_margin_clarify_state_observe():
    g = gate()
    p = _params_intent(g)
    base = dict(c_state=0.9, q_best=1.0, q_second=0.0, c_goal=1.0, d_t=0.0, authorization=1, safety_gate=1,
                budget_remaining=8, dissonance_alarm=False, n_hyp=2)
    run = lambda **kw: g._decide(p, lambda: AL.DecisionInputs(**{**base, **kw})).outcome.value  # noqa: E731
    assert run() == "EXECUTE"
    assert run(safety_gate=0) == "HALT" and run(authorization=0) == "HALT"
    assert run(n_hyp=1) == "CLARIFY"
    assert run(q_best=0.7, q_second=0.3) == "CLARIFY"       # класс «model» предложения: зазор 0,4 < 0,5
    assert run(c_state=0.3) == "OBSERVE"                    # C_state < Θ_state 0,50


def test_b08d_P2_corrupted_state_or_changed_params_halt(tmp_path, monkeypatch):
    g = fail4()
    st = json.loads(g.state_path.read_text(encoding="utf-8"))
    st["integrator"]["alarm"] = False                       # противоречит D ≥ Θ
    g.state_path.write_text(json.dumps(st), encoding="utf-8")
    G.reset()
    v = gate().before("amb_brighter", "verified", 1, None)
    assert v.outcome == "HALT" and v.why == "INVALID_STATE" and v.alarm is True
    st["integrator"]["alarm"] = True
    st["integrator"]["tau_D"] = 30.0                        # параметры службы изменились
    gate().state_path.write_text(json.dumps(st), encoding="utf-8")
    G.reset()
    v = gate().before("amb_brighter", "verified", 1, None)
    assert v.outcome == "HALT" and v.why == "INVALID_CONFIG"


def test_b08d_P2_missing_params_file_halt(monkeypatch, tmp_path):
    monkeypatch.setenv(G.PARAMS_ENV, str(tmp_path / "nope.json"))
    G.reset()
    v = gate().before("amb_brighter", "verified", 1, None)
    assert v.outcome == "HALT" and v.alarm is True


# ===================================================================== P3 (боевой режим)
def test_b08d_P3_alarm_probe_reads_only_and_clears_on_5th(monkeypatch):
    fail4()
    monkeypatch.setenv(G.LIVE_ENV, "1")
    tv = WireTV(accept=True)
    ans = play(["подсветка ярче"], tv)[0]
    assert writes(tv) == []                                 # ни одной записи в ТВ
    assert ans.startswith("⏸ Σ: тревога")
    g = gate()
    assert g.integ.alarm is False and g.probe_ok_run == 5   # снята на 5-м удачном чтении
    assert len(tv.wire) == 5
    sig = [r for r in rows() if r["step"] == "sigma"]
    assert sig and sig[-1]["sigma_live"] == 1 and sig[-1]["a_t"] == 1


def test_b08d_P3_four_ok_one_bad_keeps_alarm_and_resets_count(monkeypatch):
    g = fail4()
    ref = g.obs["setting_710"][0]
    tv = WireTV(reads=[ref] * 4 + [ref + 5] * 4)            # 5-е расходится, затем стабильно
    res = g.probe(H._ProbeTV(tv), "setting_710", lambda: H._SHADOW_SEQ.__next__(), pause=0)
    assert res["ok"] == 7 and res["fail"] == 1
    assert res["reads"] <= 8 and writes(tv) == []          # бюджет R_t = 3 + 3 + 2
    assert g.integ.alarm is True and g.probe_ok_run == 3    # счёт удачных подряд начат заново после сбоя
    assert res["reason"] == "BUDGET_EXHAUSTED"


def test_b08d_P3_failed_read_is_delta_1(monkeypatch):
    from tv_control import TVTransportError
    g = fail4()
    tv = WireTV(reads=[TVTransportError("нет сети")] * 8)
    res = g.probe(H._ProbeTV(tv), "setting_710", lambda: H._SHADOW_SEQ.__next__(), pause=0)
    assert res["fail"] == 8 and res["ok"] == 0 and g.integ.alarm is True


@pytest.mark.parametrize("fails,need", [(4, 5), (5, 6), (8, 7), (12, 8)])
def test_b08d_P3_MA17_long_series_needs_up_to_8(fails, need):
    g = gate()
    t = 1000.0
    for i in range(fails):
        g.pending.append([100 + i, t + i, 1])
    g.before(None, "verified", None, None, now=t + fails)
    assert g.integ.alarm is True
    g.obs["setting_710"] = [3, t]
    tv = WireTV(reads=[3] * 10)
    res = g.probe(H._ProbeTV(tv), "setting_710", lambda: H._SHADOW_SEQ.__next__(), pause=0)
    assert res["cleared"] is True and res["ok"] == need and res["reads"] == need


def test_b08d_P3_probe_tv_blocks_any_write():
    tv = WireTV()
    p = H._ProbeTV(tv)
    for name in ("set_setting_value", "send_key", "set_volume", "volume_step", "set_lounge_color"):
        with pytest.raises(PermissionError):
            getattr(p, name)
    assert p.get_setting_value(710) == 4 and writes(tv) == []


def test_b08d_P3_MA18_pause_does_not_clear_alarm():
    g = fail4()
    G.reset()
    g2 = gate()
    d, a = g2.integ.project(g2.integ.last_ts + 7 * 86400)
    assert a is True and d == g2.integ.d                     # тишина тревогу не снимает (заморозка D при A_t = 1)


# ===================================================================== P4
def _run_all(monkeypatch, flag_live, shadow):
    G.reset()
    if flag_live is None:
        monkeypatch.delenv(G.LIVE_ENV, raising=False)
    else:
        monkeypatch.setenv(G.LIVE_ENV, flag_live)
    monkeypatch.setenv(H.SHADOW_FLAG_ENV, shadow)
    out = []
    for accept in (True, False):
        tv = WireTV(accept=accept)
        ans = play(["подсветка ярче", "подсветка яркость 7", "подсветка тусклее", "подсветка ярче"] * 2, tv)
        out.append((ans, tv.wire))
    return out


def test_b08d_P4_live0_behaviour_unchanged(monkeypatch, tmp_path):
    ref = _run_all(monkeypatch, None, "0")                  # шлюз не вызывается вовсе (как до )
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(tmp_path / "a" / "s.jsonl"))
    unset = _run_all(monkeypatch, None, "1")
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(tmp_path / "b" / "s.jsonl"))
    zero = _run_all(monkeypatch, "0", "1")
    assert ref == unset == zero
    assert any(r.get("sigma") == "HALT" for r in outcomes())   # вердикт HALT записан, команда исполнена


def test_b08d_P4_live0_gate_crash_behaviour_unchanged(monkeypatch, tmp_path):
    ref = _run_all(monkeypatch, None, "0")

    def boom(*a, **k):
        raise RuntimeError("gate down")
    monkeypatch.setattr(G, "get_gate", boom)
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(tmp_path / "c" / "s.jsonl"))
    assert _run_all(monkeypatch, None, "1") == ref


def test_b08d_P4_flag_only_from_env(monkeypatch):
    for v, exp in ((None, False), ("0", False), ("", False), ("true", False), ("1", True), (" 1 ", True)):
        if v is None:
            monkeypatch.delenv(G.LIVE_ENV, raising=False)
        else:
            monkeypatch.setenv(G.LIVE_ENV, v)
        assert G.live_enabled() is exp


def test_b08d_P4_live1_halt_does_not_send(monkeypatch):
    monkeypatch.setenv(G.LIVE_ENV, "1")
    tv = WireTV()
    ans = play(["подсветка ярче", "подсветка ярче"], tv)
    assert tv.wire == [] and all(a.startswith("⏸ Σ: команда не отправлена") for a in ans)
    assert [r["sigma"] for r in rows() if r["step"] == "sigma"] == ["HALT", "HALT"]


def test_b08d_P4_live1_without_shadow_journal_still_gated(monkeypatch):
    monkeypatch.setenv(G.LIVE_ENV, "1")
    monkeypatch.setenv(H.SHADOW_FLAG_ENV, "0")
    tv = WireTV()
    play(["подсветка ярче"], tv)
    assert tv.wire == []


# ===================================================================== P5
def test_b08d_P5_transition_and_residual_counters():
    tv = WireTV(start=4)
    play(["подсветка ярче", "подсветка ярче", "подсветка яркость 9", "подсветка тусклее"], tv)
    g = gate()
    n = g.n["setting_710"]
    assert n["STEP:1"]["5"]["6"] == 1                       # x' = 5 -> y = 6 при STEP +1
    assert n["SET:9"]["6"]["9"] == 1 and n["STEP:-1"]["9"]["8"] == 1
    assert g.m["setting_710"] == {"0": 3}                   # все остатки r = y − x_target = 0
    tv2 = WireTV(start=8, accept=False)
    play(["подсветка ярче"], tv2)
    assert g.m["setting_710"].get("-1") == 1                # ТВ не принял: y = 8, x_target = 9
    assert g.metric["sent"] == 5 and g.metric["verified"] == 4 and g.metric["fail"] == 1


def test_b08d_P5_set_target_is_s_exp_and_c_state_grows():
    tv = WireTV(start=4)
    play(["подсветка яркость 6", "подсветка яркость 4"] * 3, tv)
    g = gate()
    c = g.c_state("setting_710", "SET", 6, g.obs["setting_710"][1])
    c0 = G.Gate(g.state_path.with_name("fresh.json"), G.DEFAULT_PARAMS, [])
    c0.obs = dict(g.obs)
    assert c > c0.c_state("setting_710", "SET", 6, g.obs["setting_710"][1])   # счётчики n/m повышают C_state
    assert 0.0 <= c <= 1.0


def test_b08d_P5_isolated_volume_and_blind_no_event():
    g = gate()
    assert g.after(1, action="volume_up", kind="verified", outcome="FAIL", err="verify_fail", feature="volume",
                   expected=5, observed=0, d_cmd=1, sent=True) is None
    assert g.after(2, action="key_ok", kind="blind", outcome="WARN", err=None, feature="keys",
                   expected=1, observed=None, d_cmd=None, sent=True) is None
    assert g.pending == [] and g.metric["sent"] == 0


# ===================================================================== P6
def test_b08d_P6_journal_fields_codes_and_whitelist():
    tv = WireTV()
    play(["подсветка ярче", "подсветка яркость 7"], tv)
    raw = journal().read_text(encoding="utf-8")
    for r in rows():
        assert set(r) <= H.SHADOW_FIELDS and H.shadow_validate(r) == r
    for o in outcomes():
        assert o["sigma"] in H.SIGMA_OUTCOMES and o["sigma_p"] in H.SIGMA_OUTCOMES
        assert o["a_t"] in (0, 1) and o["sigma_live"] == 0
    assert "подсвет" not in raw
    # идентификатор -- по значениям полей, не по сырому тексту (в метке времени «4242» встречается случайно)
    assert not any(str(ADMIN) in str(v) for r in rows() for k, v in r.items() if k not in ("ts", "seq", "latency_ms"))
    assert not any(k in ("id", "user", "user_id", "chat_id") or k.endswith("_id") for r in rows() for k in r)
    assert H.shadow_validate(dict(ts=1, step="outcome", sigma="GO")) is None
    assert H.shadow_validate(dict(ts=1, step="outcome", a_t=True)) is None
    assert H.shadow_validate(dict(ts=1, step="outcome", sigma_why="тревога")) is None


def test_b08d_P6_stats_fields_3d_and_gate_A_metric():
    play(["подсветка ярче", "подсветка яркость 7"], WireTV())
    play(["подсветка ярче"], WireTV(accept=False))
    st = sss.compute(journal(), sss.ISOLATE_DEFAULT)
    f = st["fields_3d"]
    assert st["lines_bad"] == 0 and f["outcome_with_sigma"] == 3 and f["live1"] == 0
    assert f["gateA_sent"] == 3 and f["gateA_verified"] == 2 and f["gateA_ratio"] == round(2 / 3, 4)
    assert sum(f["sigma"].values()) == 3
    txt = sss.render(st)
    assert "gate A" in txt and "fields 3d" in txt
    assert st["step"] == {s: st["step"][s] for s in sss.STEPS}  # прежний счётчик step не изменился


def test_b08d_P6_old_rows_without_sigma_still_valid():
    p = journal()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(ts=1, step="outcome", seq=3, outcome="PASS")) + "\n", encoding="utf-8")
    st = sss.compute(p, sss.ISOLATE_DEFAULT)
    assert st["lines_bad"] == 0 and st["fields_3d"]["outcome_with_sigma"] == 0
    assert st["fields_3d"]["gateA_ratio"] is None


def test_b08d_P4_execute_signature_kept_and_intent_class(monkeypatch):
    import inspect
    assert list(inspect.signature(H._execute).parameters) == ["command", "tv_factory"]   # полигон: _execute(cmd, f)
    seen = []
    real = G.Gate.before

    def spy(self, action, kind, d_cmd, v_set, src="rule", now=None):
        seen.append(src)
        return real(self, action, kind, d_cmd, v_set, src, now)
    monkeypatch.setattr(G.Gate, "before", spy)
    monkeypatch.setattr(tv_intent, "understand", lambda *a, **k: "подсветка ярче")
    play(["подсветка ярче", "подсветка пожалуйста посветлее чуть-чуть"], WireTV())
    assert seen == ["rule", "model"] and getattr(H._INTENT, "src", "rule") == "rule"


def test_b08d_P4_without_sigma_module_old_path(monkeypatch, tmp_path):
    """Публичное зеркало без приватного sigma_gate: прежний путь (журнал «теневой журнал Σ» без полей Σ), ответы те же."""
    ref = _run_all(monkeypatch, None, "0")
    monkeypatch.setattr(H, "sigma_gate", None)
    monkeypatch.setenv(H.SHADOW_PATH_ENV, str(tmp_path / "d" / "s.jsonl"))
    assert _run_all(monkeypatch, None, "1") == ref
    assert outcomes() and all("sigma" not in o for o in outcomes())
