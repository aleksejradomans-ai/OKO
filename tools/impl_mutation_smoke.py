"""Мутационный smoke РЕАЛИЗАЦИИ красной зоны (B33 v2 C5; B32r H4б): сломанный код обязан ронять свои тесты.

  python tools/impl_mutation_smoke.py [--root .] [--only NAME]

Для каждой мутации: копия дерева (без .git, data, exchange) во временной папке, ОДНА точная замена текста в файле
реализации (ровно одно вхождение), затем pytest подтверждающих тестов: строки docs/control_map.csv по id инварианта
(колонка tests) и, где у правила нет строки карты, -- названные тесты. Мутация поймана, только если pytest упал
(код != 0 и есть failed). Контроль: те же тесты на неизменённой копии -- зелёные (иначе smoke не доказывает ничего).
Копия: в git-дереве только файлы git (ls-files: отслеживаемые + новые не игнорируемые), иначе copytree.
Мутации: SIGN_VOLUME_MAX 15 -> 99 (INV-19); срок подтверждения CONFIRM_TTL_S не применяется (tv_bot_handler);
sigma_gate.live_enabled всегда False (INV-10, INV-05); хозяин без проверки ADMIN_USER_IDS (INV-07);
sigma_live.live всегда False (B33 v2 C1: пути мимо шлюза). Печатает `IMPL_SMOKE <имя> caught=yes|no ...` и
`IMPL_SMOKE_SUMMARY mutants=<n> caught=<n> control=green|red`. Код возврата 0 -- все пойманы и контроль зелёный.
B33 v2 попытка 3 (selfrun ПК 25.11 09:09: `control rc=1` без строки итога): контроль красный -> `IMPL_SMOKE control
failed ...` + первые строки падения (FAILED / ERROR / stderr), мутанты НЕ гоняются (`not_run=control_failed`), а не
«caught=no». pytest-ребёнок изолирован: своё --basetemp в папке smoke, PYTHONIOENCODING=utf-8 / PYTHONUTF8=1, без
PYTHONPATH / PYTEST_ADDOPTS / PYTEST_CURRENT_TEST родителя; вывод CLI -- всегда UTF-8. Удаление папок -- rmtree_force
(Windows: объекты git только для чтения)."""
from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# (имя, файл, было, стало, [инварианты карты], [названные тесты вне карты])
MUTANTS = [
    ("sign_volume_max_99", "src/tv_commands.py", "SIGN_VOLUME_MAX = 15", "SIGN_VOLUME_MAX = 99", ["INV-19"], []),
    ("confirm_ttl_ignored", "src/tv_bot_handler.py", "_Pending(command, now + CONFIRM_TTL_S)",
     "_Pending(command, now + CONFIRM_TTL_S * 1000)", [],
     ["src/test_tv_bot_handler.py::test_expired_confirmation_is_not_executed",
      "src/test_tv_bot_handler.py::test_expired_then_new_command_runs"]),
    ("sigma_live_enabled_false", "src/sigma_gate.py", 'return os.getenv(LIVE_ENV, "").strip() == "1"', "return False",
     ["INV-10", "INV-05"], []),
    ("host_without_admin_check", "src/host_only.py", "if hid is None or hid not in _admins(cfg):", "if hid is None:",
     ["INV-07"], []),
    ("bypass_live_false", "src/sigma_live.py", 'return os.getenv(LIVE_ENV, "").strip() == "1"', "return False", [],
     ["src/test_b33v2_sigma_single_point.py"]),
]
IGNORE = shutil.ignore_patterns(".git", "__pycache__", "data", "exchange", "reports", "*.pyc", "nssm-2.24", "eval",
                                "pt_tmp", "tmp_*")
_SKIP_TOP = {"data", "exchange", "reports", "eval", "pt_tmp", "nssm-2.24"}


def copy_tree(root: Path, dst: Path) -> str:
    """Копия для мутаций. В git-дереве -- только файлы git (отслеживаемые + новые не игнорируемые: на ПК нет копии
    venv, логов и моделей); без git (экспорт снимка) -- copytree с IGNORE. Возвращает способ."""
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                       capture_output=True, env=child_env()) if (root / ".git").exists() else None
    if r is None or r.returncode != 0:
        shutil.copytree(root, dst, ignore=IGNORE)
        return "copytree"
    for rel in sorted(set(x for x in r.stdout.decode("utf-8", "replace").split("\0") if x)):
        parts = rel.split("/")
        if parts[0] in _SKIP_TOP or parts[0].startswith("tmp_") or "__pycache__" in parts or rel.endswith(".pyc"):
            continue
        src = root / rel
        if src.is_file():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst / rel)
    return "git_files"


def map_tests(root: Path, ids: list) -> list:
    out = []
    with (root / "docs" / "control_map.csv").open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["id"] in ids:
                out += [t.strip() for t in r["tests"].split(";") if t.strip()]
    return out


def _force_writable(func, path, *_exc) -> None:
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def rmtree_force(path) -> None:
    """shutil.rmtree, которое снимает «только чтение» (Windows: .git/objects) и повторяет; отсутствие -- не ошибка."""
    if not Path(path).exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_writable)
    else:  # pragma: no cover - Python < 3.12
        shutil.rmtree(path, onerror=_force_writable)


_DROP_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH", "PYTHONWARNINGS",
             "PYTHONINSPECT", "PYTHONEXECUTABLE", "PYTHONNOUSERSITE", "PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST",
             "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG", "OKO_NOTIFY_OFF", "OKO_SIGMA_LIVE",
             "OKO_REQUEST_TS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
             "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CONFIG_PARAMETERS",
             "GIT_CONFIG_COUNT", "GIT_CEILING_DIRECTORIES")   # B33 attempt 4: + the same list as every block child


def child_env() -> dict:
    """Окружение pytest-ребёнка: как у родителя, но без того, что меняет сборку / импорт, и с UTF-8 выводом."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in _DROP_ENV
           and not k.upper().startswith(("OKO_B33_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def run_tests(tree: Path, tests: list, basetemp: Path) -> tuple:
    """-> (rc, строка итога pytest или rc=N, [до 6 строк причины: FAILED / ERROR / первые строки stderr])."""
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", "-rfE",
                        f"--basetemp={basetemp}", *tests], cwd=tree, capture_output=True, timeout=900, env=child_env())
    out = r.stdout.decode("utf-8", "replace").splitlines()
    err = r.stderr.decode("utf-8", "replace").splitlines()
    tail = [ln for ln in out if re.search(r"\d+ (passed|failed|error)", ln)]
    why = [ln.strip() for ln in out if ln.startswith(("FAILED ", "ERROR ", "E ", "INTERNALERROR"))][:4]
    why += [ln.strip() for ln in err if ln.strip()][:6 - len(why)]
    return r.returncode, (tail[-1].strip() if tail else f"rc={r.returncode} no pytest summary"), why


def run(root: Path, only: str = "") -> tuple:
    lines, caught, mutants = [], 0, [m for m in MUTANTS if not only or m[0] == only]
    td = tempfile.mkdtemp(prefix="impl_smoke_")
    try:
        base = Path(td) / "base"
        how = copy_tree(root, base)
        all_tests = sorted({t for m in mutants for t in map_tests(root, m[4]) + m[5]})
        crc, ctail, cwhy = run_tests(base, all_tests, Path(td) / "bt_control")
        control = crc == 0
        lines.append(f"IMPL_SMOKE control rc={crc} {ctail} tests={len(all_tests)} copy={how}")
        if not control:
            lines.append(f"IMPL_SMOKE control failed: the unmutated copy is red -- mutants not run (this is NOT 'caught=no')")
            lines += [f"IMPL_SMOKE control_why {w[:300]}" for w in (cwhy or ["(pytest printed nothing)"])]
            lines += [f"IMPL_SMOKE {m[0]} not_run=control_failed" for m in mutants]
            mutants = []
        for name, rel, old, new, ids, named in mutants:
            tree = Path(td) / name
            shutil.copytree(base, tree)
            f = tree / rel
            text = f.read_text(encoding="utf-8-sig")
            n = text.count(old)
            if n != 1:
                lines.append(f"IMPL_SMOKE {name} caught=no reason=mutation_text_found_{n}_times file={rel}")
                continue
            f.write_text(text.replace(old, new), encoding="utf-8")
            tests = map_tests(root, ids) + named
            rc, tail, _ = run_tests(tree, tests, Path(td) / f"bt_{name}")
            ok = rc != 0 and "failed" in tail
            caught += ok
            lines.append(f"IMPL_SMOKE {name} caught={'yes' if ok else 'no'} file={rel} map={','.join(ids) or '-'} "
                         f"tests={len(tests)} rc={rc} | {tail}")
            rmtree_force(tree)
    finally:
        try:
            rmtree_force(td)
        except OSError:
            pass
    n = len([m for m in MUTANTS if not only or m[0] == only])
    lines.append(f"IMPL_SMOKE_SUMMARY mutants={n} caught={caught} control={'green' if control else 'red'}")
    return (0 if caught == n and control else 1), lines


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):                    # попытка 3: вывод CLI всегда UTF-8 (и под -I на Windows)
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="impl_mutation_smoke")
    ap.add_argument("--root", default=".")
    ap.add_argument("--only", default="")
    a = ap.parse_args(argv)
    rc, lines = run(Path(a.root).resolve(), a.only)
    for ln in lines:
        sys.stdout.buffer.write((ln + "\n").encode("ascii", "backslashreplace"))  # B33 attempt 4: output ASCII (non-ASCII data escaped)
    return rc


if __name__ == "__main__":
    sys.exit(main())
