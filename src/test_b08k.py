"""(17.11): закрытие H1 и полировка публичного среза. Боевая логика не меняется: проверяется текст файлов
(без ссылок на непубличные документы), генератор параметров Σ (ссылка ref без имён внутренних файлов, числа те же),
docstring шлюза Σ == белый список журнала, .gitignore, обезличенный docstring cam_probe. Значения синтетические."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PARAMS_FILE = REPO / "config" / "sigma_params.json"
# метки ссылок на документы, которых нет в публичном срезе (рабочий журнал решений, черновики, задания, библиотека)
INTERNAL_REF = re.compile(r"(?<![A-Za-z0-9_])DECISIONS(?![A-Za-z0-9_])|DECISION_draft|to_oko[\\/]|library[\\/ ]+0\d"
                          r"|task_[a-z0-9_]+\.md")


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cs():
    return _load("tools/calibrate_sigma.py", "b08k_calibrate_sigma")


# --- H1: скрипт очистки истории в публичном срезе (tools/, не tools/archive/) -------------------------------------
def test_b08k_clear_history_lives_in_tools():
    assert (REPO / "tools" / "_clear_history.py").is_file()
    assert not (REPO / "tools" / "archive" / "_clear_history.py").exists()


# --- п.2: файл параметров Σ -- без ссылок на непубличные документы, JSON и числа читаются ядром ------------------
def test_b08k_params_file_has_no_internal_refs():
    text = PARAMS_FILE.read_text(encoding="utf-8")
    assert not INTERNAL_REF.search(text)
    doc = json.loads(text)
    assert doc["dt_0"]["value"] == 13.0 and doc["dt_0"]["status"] == "MANUAL"
    assert "допуск d_∞" in doc["eps_norm"]["use"]


def test_b08k_params_file_is_generator_format(cs):
    raw = PARAMS_FILE.read_bytes()
    assert cs.dumps(json.loads(raw.decode("utf-8"))) == raw          # тот же сериализатор, что пишет calibrate_sigma


def test_b08k_params_file_loads_in_kernel():
    sys.path.insert(0, str(REPO / "src"))
    import abstraction_layer as al
    p = al.params_from_doc(json.loads(PARAMS_FILE.read_text(encoding="utf-8")))
    assert (p.tau_D, p.dt_0, p.theta_crit, p.theta_reset) == (60.0, 13.0, 0.5, 0.2)


@pytest.mark.parametrize("ref, want", [
    ("DECISIONS 2026-11-16 12:04 — ручные значения 2d; 13:43 — закрытие 2e (DECISION_draft_2026-11-16_2e_close.md); 18:21",
     "решение 2026-11-16 12:04 — ручные значения 2d; 13:43 — закрытие 2e; 18:21"),
    ("решение 2026-11-16 — ручные значения (тест)", "решение 2026-11-16 — ручные значения (тест)"),
    ("DECISION_THRESHOLDS остаётся", "DECISION_THRESHOLDS остаётся"),
])
def test_b08k_public_ref_vectors(cs, ref, want):
    assert cs.public_ref(ref) == want
    assert cs.public_ref(want) == want                                   # идемпотентно


def test_b08k_manual_ref_is_public_in_output(cs):
    man, missing = cs.validate_manual({"ref": "DECISIONS 2026-11-16 12:04 (DECISION_draft_x.md)", "tau_D": 60,
                                       "Theta_crit": 0.5, "Theta_reset": 0.2,
                                       "R_t": {"N_obs": 3, "N_route": 3, "N_reflect": 2}})
    assert missing == [] and man["ref"] == "решение 2026-11-16 12:04"
    assert man["tau_D"] == 60.0 and man["Theta_crit"] == 0.5 and man["Theta_reset"] == 0.2


def test_b08k_generator_constants_have_no_internal_refs(cs):
    consts = [v for k, v in vars(cs).items() if k.startswith("REF_") and isinstance(v, str)]
    consts += [v for v in cs.NO_DATA_FIXED.values()] + [cs.SMOOTH_METHOD] + [t for _, t in cs.KAPPA_KNOWN.values()]
    assert consts and not any(INTERNAL_REF.search(v) for v in consts)


# --- п.3 (M1): docstring шлюза Σ соответствует белому списку журнала --------------------------------------------
def test_b08k_sigma_gate_doc_matches_shadow_fields():
    sys.path.insert(0, str(REPO / "src"))
    import sigma_gate
    import tv_bot_handler
    doc = sigma_gate.__doc__
    assert "значения признаков в журнал не пишет" not in doc
    for f in ("s_exp", "s_obs", "s_raw", "node_from", "node_to", "ts", "sigma", "sigma_p", "sigma_why", "a_t",
              "sigma_live"):
        assert f in tv_bot_handler.SHADOW_FIELDS and re.search(r"(?<![A-Za-z0-9_])" + f + r"(?![A-Za-z0-9_])", doc), f
    assert "SHADOW_FIELDS" in doc


# --- п.4 (M2, O7): .gitignore -------------------------------------------------------------------------------------
def test_b08k_gitignore_rules():
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.jsonl" in lines and "sigma_state.json" in lines
    tmp_sync = [".tmp." + "drive" + d + "load/" for d in ("up", "down")]   # имена временных папок синхронизации
    for gone in ["export_public.py", "sync_public.py", "tools/oko_week.ps1", "games_candidates.log"] + tmp_sync:
        assert gone not in lines, gone
    assert not any("game watcher" in ln for ln in lines)


# --- п.5 (O8): cam_probe -- нейтральное описание проверки ---------------------------------------------------------
def test_b08k_cam_probe_doc_is_neutral():
    doc = (REPO / "tools" / "cam_probe.py").read_text(encoding="utf-8").split('"""')[1]
    assert "RTSP" in doc and "ONVIF" in doc
    for bad in ("34567", "Xiong" + "mai", "iC" + "See", "X6" + "C", "MAC", "198.51.100."):   # имена -- по частям
        assert bad not in doc, bad
