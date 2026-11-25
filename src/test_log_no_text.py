"""Текст ответа и полный путь BASE_DIR -- вне рабочих логов (флаг диагностики OKO_DIAG_LOG_TEXT по умолчанию выключен).

JUDGE_TRACE -- в test_judge_trace.py. Здесь: строка «Response routed» бота (длина + sha16, без текста) и печать
config при импорте (без полного пути; полный -- только под флагом, с пометкой флага).
"""
from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
# B33 attempt 4 (item 3): every child process of this test file gets a hermetic environment -- without the parent
# variables that change Python / pytest / git (selfrun / Operator profile: PYTHONHOME, PYTHONPATH, PYTEST_ADDOPTS,
# OKO_B33_*, OKO_NOTIFY_OFF, GIT_DIR ...), with UTF-8 stdio. tools/build_contour_guard.py: bare_child is red here.
_DIRTY_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH", "PYTHONWARNINGS",
              "PYTHONINSPECT", "PYTHONEXECUTABLE", "PYTHONNOUSERSITE", "PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST",
              "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG", "OKO_NOTIFY_OFF", "OKO_SIGMA_LIVE",
              "OKO_REQUEST_TS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CONFIG_PARAMETERS",
              "GIT_CONFIG_COUNT", "GIT_CEILING_DIRECTORIES")


def _henv(**extra) -> dict:
    import os as _os
    env = {k: v for k, v in _os.environ.items() if k.upper() not in _DIRTY_ENV
           and not k.upper().startswith(("OKO_B33_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    env.update(extra)
    return env


def _hrun(*a, **k):
    k.setdefault("env", _henv())
    return subprocess.run(*a, **k)


SRC = Path(__file__).resolve().parent


def _logger_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "logger"):
            yield n


def test_bot_response_routed_logs_length_and_fingerprint_only():
    calls = [c for c in _logger_calls(SRC / "bot.py")
             if c.args and isinstance(c.args[0], ast.Constant) and "Response routed" in str(c.args[0].value)]
    assert len(calls) == 1
    (call,) = calls
    assert call.args[0].value == "Response routed to %s: len=%d sha16=%s"
    for a in call.args[1:]:                        # текст ответа -- только внутри len() / _sha16()
        if any(isinstance(x, ast.Name) and x.id == "final_response" for x in ast.walk(a)):
            assert isinstance(a, ast.Call) and getattr(a.func, "id", "") in {"len", "_sha16"}
    src = (SRC / "bot.py").read_text(encoding="utf-8-sig")
    assert "final_response[:50]" not in src


def test_no_logger_call_formats_the_response_text_into_the_message():
    """Сторож: ни один вызов logger в bot.py / governor.py не подставляет в сообщение срез текста ответа
    (final_response / clean_response / checked text) -- кроме строки под флагом диагностики в governor.py."""
    bad = []
    for name in ("bot.py", "governor.py"):
        for c in _logger_calls(SRC / name):
            fmt = c.args[0].value if c.args and isinstance(c.args[0], ast.Constant) else ""
            if "OKO_DIAG_LOG_TEXT=1" in str(fmt):
                continue
            for a in list(c.args) + [k.value for k in c.keywords]:
                for x in ast.walk(a):
                    if (isinstance(x, ast.Subscript) and isinstance(x.slice, ast.Slice) and isinstance(x.value, ast.Name)
                            and x.value.id in {"final_response", "clean_response", "text", "response", "corrected"}):
                        bad.append(f"{name}:{c.lineno}")
    assert bad == []


def test_bot_sha16_helper():
    import bot   # обычный import: сломанный модуль ядра = красный тест, не пропуск
    assert bot._sha16("ответ") == hashlib.sha256("ответ".encode("utf-8")).hexdigest()[:16]
    assert len(bot._sha16("")) == 16 and bot._sha16(None) == bot._sha16("")


def _import_config(tmp_path, flag):
    env = {k: v for k, v in _henv().items() if k != "OKO_DIAG_LOG_TEXT"}
    env.update({"AI_BASE_DIR": str(tmp_path / "secret_place" / "data"), "TELEGRAM_TOKEN": "test-token-not-real",
                "ADMIN_USER_IDS": "", "PYTHONIOENCODING": "utf-8"})
    if flag is not None:
        env["OKO_DIAG_LOG_TEXT"] = flag
    r = _hrun([sys.executable, "-c", "import config; print(config.DIAG_LOG_TEXT)"], cwd=str(SRC), env=env,
                       capture_output=True, timeout=120)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("flag", [None, "0", "true", ""])
def test_config_import_does_not_print_full_base_dir_by_default(tmp_path, flag):
    rc, out, err = _import_config(tmp_path, flag)
    assert rc == 0, err
    assert out.strip() == "False"
    assert "secret_place" not in err and str(tmp_path) not in err
    assert "[config] BASE_DIR resolved: name=data" in err and "exists=False" in err


def test_config_import_prints_full_base_dir_only_with_flag_and_says_so(tmp_path):
    rc, out, err = _import_config(tmp_path, "1")
    assert rc == 0, err
    assert out.strip() == "True"
    assert "[OKO_DIAG_LOG_TEXT=1]" in err and "secret_place" in err
