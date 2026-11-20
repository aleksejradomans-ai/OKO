"""B27 (T4): sigma_gate -- пять путей `except Exception: pass` -> явный исход HALT с кодом причины.

Пути: шкалы признаков (scales), π_0 (_pi0), шаг отложенного события (_decide), счётчики по исходу (_count),
событие readback Active Probing (probe). На каждый путь -- вектор, вызывающий исключение -> HALT с ожидаемым кодом,
счётчик metric["faults"] / metric[<код>] и строка журнала службы (код + класс, без текста исключения).
Нормальный путь -- без изменений (тот же вердикт, faults = 0). OKO_SIGMA_LIVE=0: команда идёт по-старому,
вердикт HALT с кодом только пишется в теневой журнал.
"""
from __future__ import annotations

import json
import logging

import pytest

import abstraction_layer as AL
import sigma_gate as G
import tv_bot_handler as H
import test_b08d as B

_iso = B._iso                     # та же изоляция окружения, что в test_b08d (autouse)
SECRET = "user-text-must-not-leak"


def _gate_with_obs(now: float = 1000.0) -> G.Gate:
    g = B.gate()
    g.feat_of["amb_brighter"] = "setting_710"
    g.obs["setting_710"] = [4, now - 1.0]
    return g


def _before(g: G.Gate, now: float = 1000.0) -> G.Verdict:
    return g.before("amb_brighter", "verified", 1, None, now=now)


def test_b27_codes_in_one_place():
    assert G.FAULT_CODES == (G.FAULT_SCALES, G.FAULT_PI0, G.FAULT_EVENT, G.FAULT_COUNT, G.FAULT_PROBE)
    assert len(set(G.FAULT_CODES)) == 5
    for c in G.FAULT_CODES:
        assert c.startswith("SIGMA_") and c.isascii() and len(c) <= 30


def test_b27_normal_path_unchanged():
    g = _gate_with_obs()
    v = _before(g)
    assert v.outcome == "HALT" and v.why == "NO_DATA_theta_intent"     # прежний вердикт (Θ_intent = NO_DATA)
    assert v.c_state is not None
    assert g.metric["faults"] == 0 and g.carry == [] and g._faults == []
    assert not any(k in g.metric for k in G.FAULT_CODES)


def test_b27_scales_error_halt(caplog):
    g = _gate_with_obs()
    g.doc["params"]["kappa_j"] = {"value": SECRET}               # .items() у строки -> AttributeError
    with caplog.at_level(logging.WARNING, logger="oko.sigma_gate"):
        v = _before(g)
    assert v.outcome == "HALT" and v.proposal == "HALT"
    assert v.why == "SIGMA_SCALES_ERROR_AttributeError"
    assert g.metric["faults"] >= 1 and g.metric[G.FAULT_SCALES] >= 1
    msgs = [r.getMessage() for r in caplog.records]
    assert any("HALT code=SIGMA_SCALES_ERROR (AttributeError)" in m for m in msgs)
    assert not any(SECRET in m for m in msgs)


def test_b27_pi0_error_halt():
    g = _gate_with_obs()
    g.doc["params"]["pi_0"]["value"] = {}                        # признака нет -> KeyError (раньше: равномерный π_0)
    v = _before(g)
    assert v.outcome == "HALT" and v.why == "SIGMA_PI0_ERROR_KeyError"
    assert g.metric[G.FAULT_PI0] == 1 and g.metric["faults"] == 1


@pytest.mark.parametrize("node", ["pi_0", "kappa_j"])
def test_b27_no_data_params_are_not_faults(node):
    """NO_DATA в params (синтетический дефолт публичного среза: value = null) -- штатный путь, не сбой: π_0 = равномерный,
    шкал нет -> прежний исход (HALT NO_DATA / по порогам), faults = 0."""
    g = _gate_with_obs()
    ref = _before(g).why
    G.reset()
    g = _gate_with_obs()
    g.doc["params"][node] = {"status": "NO_DATA", "value": None}
    v = _before(g)
    assert g.metric["faults"] == 0 and not (v.why or "").startswith("SIGMA_")
    if node == "pi_0":
        assert v.why == ref
    else:
        assert v.outcome == "HALT" and v.why == "NO_DATA_kernel"


def test_b27_eps_per_feature_no_data_not_fault():
    g = _gate_with_obs()
    g.doc["eps_norm"]["per_feature"] = None
    assert g.scales() == {} and g.metric["faults"] == 0


def test_b27_event_rejected_halt():
    g = B.gate()
    g.pending = [[5, 2000.0, 0], [6, 1000.0, 1], [7, 2001.0, 0]]   # среднее событие: Δt < 0 -> шаг отвергнут
    v = g.before(None, "verified", None, None, now=2001.0)
    assert v.outcome == "HALT" and v.why == "SIGMA_EVENT_REJECTED_INVALID_TIMESTAMP"
    assert g.integ.last_event_id == 7                             # остальные события учтены, отвергнутое -- нет
    assert g.metric[G.FAULT_EVENT] == 1


def test_b27_event_ok_series_no_fault():
    g = B.gate()
    g.pending = [[5, 2000.0, 0], [6, 2000.5, 1], [7, 2001.0, 0]]
    v = g.before(None, "verified", None, None, now=2001.0)
    assert not (v.why or "").startswith("SIGMA_") and g.metric["faults"] == 0


def _after_pass(g: G.Gate, seq: int = 50, now: float = 1000.0) -> None:
    g.after(seq, action="amb_brighter", kind="verified", outcome="PASS", err=None, feature="setting_710",
            expected=5, observed=5, d_cmd=1, sent=True, now=now)


def test_b27_count_error_halts_next_decision_once(monkeypatch):
    g = _gate_with_obs()

    def boom(*a, **k):
        raise ZeroDivisionError(SECRET)
    monkeypatch.setattr(AL, "transition_residual", boom)
    _after_pass(g)
    assert g.metric[G.FAULT_COUNT] == 1 and g.carry == ["SIGMA_COUNT_ERROR:ZeroDivisionError"]
    st = json.loads(g.state_path.read_text(encoding="utf-8"))
    assert st["carry"] == ["SIGMA_COUNT_ERROR:ZeroDivisionError"] and SECRET not in json.dumps(st)
    G.reset()                                                    # перенос переживает перезапуск службы
    g2 = B.gate()
    assert g2.carry == ["SIGMA_COUNT_ERROR:ZeroDivisionError"]
    v = _before(g2, now=1002.0)
    assert v.outcome == "HALT" and v.why == "SIGMA_COUNT_ERROR_ZeroDivisionError"
    v2 = _before(g2, now=1003.0)                                 # один раз: следующее решение -- обычное
    assert not (v2.why or "").startswith("SIGMA_COUNT_ERROR") and g2.carry == []


def test_b27_count_ok_no_carry():
    g = _gate_with_obs()
    _after_pass(g)
    assert g.carry == [] and g.metric["faults"] == 0


def test_b27_carry_from_state_filtered_and_capped(tmp_path):
    g = B.gate()
    g.carry = ["SIGMA_COUNT_ERROR:X"] * 20 + ["foreign"]
    g.save()
    st = json.loads(g.state_path.read_text(encoding="utf-8"))
    st["carry"] = st["carry"] + ["NOT_A_CODE:x", 7]
    g.state_path.write_text(json.dumps(st), encoding="utf-8")
    G.reset()
    g2 = B.gate()
    assert g2.carry == ["SIGMA_COUNT_ERROR:X"] * G.CARRY_MAX


def test_b27_probe_event_error_halts_probing():
    g = B.fail4()
    ref = g.obs["setting_710"][0]
    tv = B.WireTV(reads=[ref] * 8)

    def bad_seq():
        raise OverflowError(SECRET)
    res = g.probe(H._ProbeTV(tv), "setting_710", bad_seq, pause=0)
    assert res["reason"] == "HALT" and res["halt"] == "SIGMA_PROBE_EVENT_ERROR_OverflowError"
    assert res["reads"] == 1 and B.writes(tv) == []               # остановлен после первого чтения, записи нет
    assert g.metric[G.FAULT_PROBE] == 1


def test_b27_probe_scales_error_no_reads():
    g = B.fail4()
    g.doc["eps_norm"] = None
    tv = B.WireTV(reads=[4] * 8)
    res = g.probe(H._ProbeTV(tv), "setting_710", lambda: H._SHADOW_SEQ.__next__(), pause=0)
    assert res["reason"] == "HALT" and res["halt"] == "SIGMA_SCALES_ERROR_TypeError"
    assert res["reads"] == 0 and tv.wire == []


def test_b27_probe_normal_path_unchanged():
    g = B.fail4()
    ref = g.obs["setting_710"][0]
    tv = B.WireTV(reads=[ref] * 10)
    res = g.probe(H._ProbeTV(tv), "setting_710", lambda: H._SHADOW_SEQ.__next__(), pause=0)
    assert res["halt"] is None and res["cleared"] is True and res["ok"] == 5
    assert g.metric["faults"] == 0


@pytest.mark.parametrize("live", [None, "0"])
def test_b27_live0_command_still_runs_halt_in_journal(monkeypatch, live):
    if live is None:
        monkeypatch.delenv(G.LIVE_ENV, raising=False)
    else:
        monkeypatch.setenv(G.LIVE_ENV, live)
    tv = B.WireTV()
    B.play(["подсветка ярче"], tv)                                # первое наблюдение признака
    g = B.gate()
    g.doc["params"]["pi_0"]["value"] = {}
    n0 = len(B.writes(tv))
    B.play(["подсветка ярче"], tv)
    assert len(B.writes(tv)) > n0                                 # теневой режим: команда исполнена, как раньше
    o = B.outcomes()[-1]
    assert o["sigma"] == "HALT" and o["sigma_why"] == "SIGMA_PI0_ERROR_KeyError" and o["sigma_live"] == 0
    assert g.metric[G.FAULT_PI0] >= 1


def test_b27_live1_fault_does_not_send(monkeypatch):
    tv = B.WireTV()
    B.play(["подсветка ярче"], tv)
    g = B.gate()
    g.doc["params"]["pi_0"]["value"] = {}
    monkeypatch.setenv(G.LIVE_ENV, "1")
    n0 = len(tv.wire)
    ans = B.play(["подсветка ярче"], tv)[0]
    assert len(tv.wire) == n0 and ans.startswith("⏸ Σ: команда не отправлена")
    sig = [r for r in B.rows() if r["step"] == "sigma"]
    assert sig[-1]["sigma"] == "HALT" and sig[-1]["sigma_why"] == "SIGMA_PI0_ERROR_KeyError"


def test_b27_no_silent_pass_left_in_gate():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(G))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            bad.append(node.lineno)
    assert bad == []
