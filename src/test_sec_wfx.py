"""Секреты и песочница (15.11, векторы утечки файлов): песочница не тащит секреты и не отдаёт их дочернему pytest;
install_from_downloads.ps1 не пишет за пределы Root. Значения секретов в тестах -- синтетические."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import src.sandbox_environment as se
from src.sandbox_environment import SandboxEnvironment, safe_subprocess_env

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "tools" / "install_from_downloads.ps1"


# --- 1. файлы секретов не копируются ни на каком уровне -------------------------------------------------------------
def test_copy_filtered_skips_env_copies_at_any_level(tmp_path):
    src = tmp_path / "project_src"
    (src / "pkg" / "deep").mkdir(parents=True)
    secret_names = [".env", ".env.local", ".env.bak_20000101_000000", ".env.before_x1", "prod.env",
                    "config.py.bak_x1", "bot.py.before_x1"]
    for n in secret_names:
        (src / n).write_text("FAKE=1", encoding="utf-8")
        (src / "pkg" / "deep" / n).write_text("FAKE=1", encoding="utf-8")
    for n in ("keep.py", ".env.example"):
        (src / n).write_text("x", encoding="utf-8")
    (src / "pkg" / "deep" / "mod.py").write_text("x", encoding="utf-8")
    dst = tmp_path / "dst"
    dst.mkdir()
    SandboxEnvironment(src)._copy_filtered(src, dst)
    copied = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file())
    assert copied == [".env.example", "keep.py", "pkg/deep/mod.py"]


@pytest.mark.parametrize("name, secret", [
    (".env", True), (".ENV.BAK_X", True), (".env.before_x", True), ("x.env", True), ("a.py.bak_1", True),
    ("b.py.before_amb", True), (".env.example", False), ("environment.py", False), ("bak_notes.md", False),
    ("before.py", False),
], ids=["env", "env-bak-upper", "env-before", "x-env", "py-bak", "py-before", "example", "environment",
        "bak-notes", "before-py"])
def test_is_secret_file(name, secret):
    assert se._is_secret_file(name) is secret


# --- 2. дочерний pytest не получает переменные-секреты --------------------------------------------------------------
SECRET_VARS = ["OKO_SHELL_KEY", "GEMINI_API_KEY", "SMARTTHINGS_TOKEN", "MY_PASSWORD", "X_AUTH_HEADER",
               "DB_CREDENTIAL", "client_secret"]
KEEP_VARS = ["PATH", "SYSTEMROOT_TEST", "GIT_CONFIG_KEY_0", "GIT_CONFIG_COUNT", "OKO_COLLECTOR"]


def test_safe_subprocess_env_unit():
    base = {n: "fake" for n in SECRET_VARS + KEEP_VARS}
    out = safe_subprocess_env(base)
    assert sorted(out) == sorted(KEEP_VARS)
    assert base[SECRET_VARS[0]] == "fake"                     # исходный словарь не тронут


def test_run_tests_child_env_has_no_secrets(tmp_path, monkeypatch):
    project = tmp_path / "project_src"
    project.mkdir()
    (project / "module.py").write_text("X = 1", encoding="utf-8")
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(se.tempfile, "gettempdir", lambda: str(temp_root))
    for n in SECRET_VARS + KEEP_VARS:
        monkeypatch.setenv(n, "fake-value")
    runs = []

    def fake_run(cmd, **kw):
        runs.append(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed", stderr="")

    monkeypatch.setattr(se.subprocess, "run", fake_run)
    SandboxEnvironment(project).run_tests()
    env = runs[0]["env"]
    assert not [k for k in env if k.upper() in {s.upper() for s in SECRET_VARS}]
    for k in KEEP_VARS:
        assert k in env
    assert env["TELEGRAM_TOKEN"] == "sandbox_fake_token"     # заглушка для config._require_env
    assert Path(env["AI_BASE_DIR"]).parent.parent == temp_root


def test_module_docstring_says_not_a_security_boundary():
    assert "НЕ ГРАНИЦА БЕЗОПАСНОСТИ" in se.__doc__


# --- 3. install_from_downloads.ps1: цель только внутри Root --------------------------------------------------------
def _ps():
    for exe in ("powershell", "pwsh"):
        p = shutil.which(exe)
        if p:
            return p
    return None


PS = _ps()
needs_ps = pytest.mark.skipif(PS is None, reason="PowerShell not available")


def _run_install(root: Path, downloads: Path, rel: str, payload: bytes = b"payload-1"):
    (downloads / "a.txt").write_bytes(payload)
    h16 = hashlib.sha256(payload).hexdigest()[:16].upper()
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}   # 5.1 из pwsh 7: свои пути модулей
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALL),
                        "-Spec", f"a.txt|{rel}|{h16}", "-Root", str(root), "-Downloads", str(downloads)],
                       capture_output=True, timeout=120, env=env)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", errors="replace")   # байты: без потока чтения в cp1252


@pytest.fixture
def dirs(tmp_path):
    root = tmp_path / "oko"
    dl = tmp_path / "dl"
    root.mkdir()
    dl.mkdir()
    return tmp_path, root, dl


@needs_ps
@pytest.mark.parametrize("rel", ["..\\evil.txt", "../evil.txt", "sub\\..\\..\\evil.txt", "..\\oko_evil\\x.txt",
                                 "{ABS}", "\\\\server\\share\\x.txt"],
                         ids=["dotdot-back", "dotdot-fwd", "nested-dotdot", "prefix-trick", "absolute", "unc"])
def test_install_rejects_target_outside_root(dirs, rel):
    tmp, root, dl = dirs
    rel = rel.replace("{ABS}", str(tmp / "abs_evil.txt"))
    rc, out = _run_install(root, dl, rel)
    assert rc == 3, out
    assert "not inside root" in out
    assert (dl / "a.txt").exists()                            # ничего не перемещено
    assert not (tmp / "evil.txt").exists() and not (tmp / "oko_evil").exists() and not (tmp / "abs_evil.txt").exists()


@needs_ps
def test_install_inside_root_still_works(dirs):
    tmp, root, dl = dirs
    rc, out = _run_install(root, dl, "sub\\a.txt")
    assert rc == 0, out
    assert (root / "sub" / "a.txt").read_bytes() == b"payload-1" and not (dl / "a.txt").exists()
