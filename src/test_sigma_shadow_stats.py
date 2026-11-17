"""(15.11, фаза 2, этап 2c): tools/sigma_shadow_stats.py -- только счётчики, битые строки, пустой/нет файла."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "sigma_shadow_stats.py"
_spec = importlib.util.spec_from_file_location("sigma_shadow_stats_b07c", _TOOL)
sss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sss)

# Заметные значения: ни одно из них не должно попасть в вывод.
TS0 = 1794700000123
SEXP = 987654321
SOBS = 876543219
NODE_A = "NODEZQXSECRETA"
NODE_B = "NODEZQXSECRETB"
LAT = 4242427


def _cmd(seq, ts, action="volume_up", feature="volume", outcome="PASS", err=None, menu=None, pair=True):
    rows = [{"ts": ts, "step": "send", "seq": seq, "cmd_kind": "verified", "action": action, "feature": feature,
             "s_exp": SEXP},
            {"ts": ts + 1, "step": "ack", "seq": seq, "cmd_kind": "verified", "action": action, "feature": feature,
             "latency_ms": LAT},
            {"ts": ts + 2, "step": "readback", "seq": seq, "cmd_kind": "verified", "action": action,
             "feature": feature, "s_obs": SOBS, "latency_ms": LAT}]
    if menu:
        rows.append({"ts": ts + 3, "step": "menu_step", "seq": seq, "cmd_kind": "verified", "action": action,
                     "node_from": menu[0], "node_to": menu[1]})
    o = {"ts": ts + 4, "step": "outcome", "seq": seq, "cmd_kind": "verified", "action": action, "feature": feature,
         "outcome": outcome, "latency_ms": LAT}
    if pair:
        o.update(s_exp=SEXP, s_obs=SOBS)
    if err:
        o["err"] = err
    rows.append(o)
    return rows


def _write(p: Path, rows, extra_lines=()):
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        for x in extra_lines:
            fh.write(x + "\n")


def _sample(tmp_path: Path) -> Path:
    p = tmp_path / "sigma_shadow.jsonl"
    rows = []
    rows += _cmd(1, TS0, menu=(NODE_A, NODE_B))
    rows += _cmd(2, TS0 + 60_000, menu=(NODE_A, NODE_B))
    rows += _cmd(3, TS0 + 120_000, outcome="FAIL", err="verify_fail", menu=(NODE_B, NODE_A))
    rows += _cmd(4, TS0 + 180_000, action="amb_on", feature="ambilight_power", outcome="FAIL", pair=False)
    rows.append({"ts": TS0 + 200_000, "step": "error", "seq": 5, "err": "net"})
    rows.append({"ts": TS0 + 200_001, "step": "error", "seq": 6, "err": "cancel"})
    _write(p, rows)
    return p


def _all_text(st) -> str:
    return sss.render(st) + "\n" + json.dumps(st, ensure_ascii=True, sort_keys=True)


def test_b07c_counts(tmp_path):
    st = sss.compute(_sample(tmp_path))
    assert st["lines_total"] == 21 and st["lines_ok"] == 21 and st["lines_bad"] == 0
    assert st["commands_seq"] == 6 and st["commands_with_outcome"] == 4
    assert st["step"] == {"send": 4, "ack": 4, "readback": 4, "outcome": 4, "menu_step": 3, "error": 2}
    assert st["action_outcome"]["volume_up"] == {"PASS": 2, "WARN": 0, "FAIL": 1}
    assert st["action_outcome"]["amb_on"] == {"PASS": 0, "WARN": 0, "FAIL": 1}
    assert st["feature_pairs"] == {"volume": {"pairs": 3, "non_pass": 1}}
    assert st["feature_action_cmds"] == {"ambilight_power|amb_on": 1, "volume|volume_up": 3}
    assert st["err"] == {"net": 1, "parse_fail": 0, "cancel": 1, "verify_fail": 1, "warn": 0, "other": 1}
    assert st["error_commands"] == 4
    assert st["menu"] == {"steps": 3, "edges": 3, "edges_distinct": 2, "edges_distinct_ge3": 0,
                          "edges_distinct_lt3": 2}
    assert st["tau_b_pairs"] == {"ambilight_power": 0, "volume": 2}
    assert st["readback"] == {"rows": 4, "with_s_obs": 4}
    assert st["period"]["hours"] == round((200_001) / 3_600_000, 2)


def test_b07c_output_counters_only(tmp_path):
    st = sss.compute(_sample(tmp_path))
    text = _all_text(st)
    for secret in (str(SEXP), str(SOBS), NODE_A, NODE_B, str(LAT), str(TS0), str(TS0 + 60_000), str(TS0 + 200_001)):
        assert secret not in text
    assert "node_from" not in text and "node_to" not in text


def test_b07c_bad_lines_skipped_and_counted(tmp_path):
    p = tmp_path / "sigma_shadow.jsonl"
    leak = "TEXTZQX vvedi gromche pozhalujsta"
    _write(p, _cmd(1, TS0), extra_lines=[
        "{not json",
        "[1,2,3]",
        json.dumps({"ts": TS0, "step": "send", "input": leak}),       # поле вне белого списка
        json.dumps({"ts": TS0, "step": "talk"}),                      # step вне списка
        json.dumps({"step": "send"}),                                 # без ts
        json.dumps({"ts": "x", "step": "send"}),                      # ts не число
        json.dumps({"ts": TS0, "step": "outcome", "action": leak, "outcome": "PASS"}),   # текст вместо кода
        json.dumps({"ts": TS0, "step": "outcome", "s_exp": "12", "outcome": "PASS"}),   # s_exp не число
        "\xff\xfe garbage",
    ])
    st = sss.compute(p)
    assert st["lines_total"] == 13 and st["lines_ok"] == 4 and st["lines_bad"] == 9
    assert st["commands_seq"] == 1 and st["step"]["outcome"] == 1
    assert list(st["action_outcome"]) == ["volume_up"]
    text = _all_text(st)
    assert "TEXTZQX" not in text and "gromche" not in text and "garbage" not in text


def test_b07c_invalid_utf8_line_counted(tmp_path):
    p = tmp_path / "sigma_shadow.jsonl"
    p.write_bytes(b'{"ts":1,"step":"send"}\n\xff\xfe\xfd\n')
    st = sss.compute(p)
    assert st["lines_total"] == 2 and st["lines_ok"] == 1 and st["lines_bad"] == 1


@pytest.mark.parametrize("mode", ["absent", "empty", "blank"], ids=["absent", "empty", "blank"])
def test_b07c_empty_or_missing_gives_zeros(tmp_path, mode):
    p = tmp_path / "sigma_shadow.jsonl"
    if mode == "empty":
        p.write_bytes(b"")
    elif mode == "blank":
        p.write_text("\n\n  \n", encoding="utf-8")
    st = sss.compute(p)
    assert st["lines_total"] == 0 and st["lines_ok"] == 0 and st["lines_bad"] == 0
    assert st["commands_seq"] == 0 and st["commands_with_outcome"] == 0 and st["error_commands"] == 0
    assert set(st["step"].values()) == {0} and set(st["err"].values()) == {0}
    assert st["menu"]["steps"] == 0 and st["readback"]["rows"] == 0
    assert st["period"] == {"first": None, "last": None, "hours": 0.0}
    assert all(t["status"] == "NOT_MET" for t in st["thresholds"])
    assert "lines: total=0 ok=0 bad=0" in sss.render(st)
    assert st["files"]["main"] == ("absent" if mode == "absent" else "lines=0")


def test_b07c_rotated_file_read_first(tmp_path):
    p = tmp_path / "sigma_shadow.jsonl"
    _write(p.with_name(p.name + ".1"), _cmd(1, TS0))
    _write(p, _cmd(2, TS0 + 1000))
    st = sss.compute(p)
    assert st["files"] == {"rotated": "lines=4", "main": "lines=4"}
    assert st["lines_ok"] == 8 and st["commands_seq"] == 2 and st["tau_b_pairs"] == {"volume": 1}


def test_b07c_thresholds(tmp_path):
    p = tmp_path / "sigma_shadow.jsonl"
    rows = []
    for i in range(30):
        rows += _cmd(i + 1, TS0 + i * 1000, outcome="WARN" if i == 0 else "PASS", err="warn" if i == 0 else None,
                     menu=(NODE_A, NODE_B) if i < 3 else None)
    for i in range(20):
        rows.append({"ts": TS0 + 100_000 + i, "step": "error", "seq": 1000 + i, "err": "net"})
    _write(p, rows)
    st = sss.compute(p)
    th = {(t["param"], t["key"]): t["status"] for t in st["thresholds"]}
    assert th[("eps_state/kappa_j/alpha_j", "volume")] == "MET"
    assert th[("T_theta(API)", "volume|volume_up")] == "MET"
    assert th[("tau_b", "volume")] == "MET"
    assert th[("pi_0", "-")] == "MET"
    assert th[("tau_D/Theta_crit/Theta_reset", "-")] == "MET"
    assert th[("T_theta(menu)", "-")] == "NOT_MET"
    assert st["menu"]["edges_distinct_ge3"] == 1 and st["menu"]["edges_distinct_lt3"] == 0
    text = _all_text(st)
    assert NODE_A not in text and str(SEXP) not in text


def test_b07c_pairs_need_non_pass(tmp_path):
    p = tmp_path / "sigma_shadow.jsonl"
    rows = []
    for i in range(31):
        rows += _cmd(i + 1, TS0 + i * 1000)
    _write(p, rows)
    st = sss.compute(p)
    th = {(t["param"], t["key"]): t["status"] for t in st["thresholds"]}
    assert st["feature_pairs"]["volume"] == {"pairs": 31, "non_pass": 0}
    assert th[("eps_state/kappa_j/alpha_j", "volume")] == "NOT_MET"


def test_b07c_cli_text_and_json(tmp_path):
    p = _sample(tmp_path)
    r = subprocess.run([sys.executable, str(_TOOL), "--path", str(p)], capture_output=True, timeout=60)
    assert r.returncode == 0
    out = r.stdout.decode("utf-8")
    assert "lines: total=21 ok=21 bad=0" in out and "thresholds:" in out
    for secret in (str(SEXP), str(SOBS), NODE_A, str(TS0)):
        assert secret not in out
    r = subprocess.run([sys.executable, str(_TOOL), "--path", str(p), "--json"], capture_output=True, timeout=60)
    assert r.returncode == 0
    js = json.loads(r.stdout.decode("ascii"))
    assert js["commands_seq"] == 6 and js["lines_bad"] == 0


def test_b07c_cli_missing_file_rc0(tmp_path):
    r = subprocess.run([sys.executable, str(_TOOL), "--path", str(tmp_path / "nope.jsonl")], capture_output=True,
                       timeout=60)
    assert r.returncode == 0 and "lines: total=0 ok=0 bad=0" in r.stdout.decode("utf-8")


def test_b07c_tool_is_stdlib_only():
    import ast
    tree = ast.parse(_TOOL.read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    std = getattr(sys, "stdlib_module_names", None)
    assert mods, "no imports parsed"
    if std:
        assert mods <= set(std) | {"__future__"}, mods - set(std)
    assert "src" not in mods and "tv_bot_handler" not in mods


def test_b07c_tool_does_not_write(tmp_path):
    p = _sample(tmp_path)
    before = sorted((x.name, x.stat().st_size, x.stat().st_mtime_ns) for x in tmp_path.iterdir())
    sss.compute(p)
    sss.main(["--path", str(p)])
    after = sorted((x.name, x.stat().st_size, x.stat().st_mtime_ns) for x in tmp_path.iterdir())
    assert before == after
