"""CLI сторожа ослабления тестов (src/weakening_guard.py): база против снимка, по git (в CI база -- опубликованный коммит).

  python tools/weakening_guard.py [--repo .] [--base auto|<ревизия>] [--head HEAD] [--collect]
      [--allow .github/test_weakening_allow.txt] [--guarded-allow .github/guarded_change_allow.txt]
  (--base по умолчанию HEAD~1 -- только для ручного прогона; CI и run блока -- --base auto / явная база)

Читает файлы обеих ревизий (коммит или дерево) из git (git ls-tree / cat-file), не из рабочего дерева: тестовые --
для ослаблений, все -- для охраняемой зоны (B33 H2.2 / H2.3); оба списка разрешений -- из ревизии head (нет файла --
пустой список; повтор ключа или entry_sha16 между файлами -- ошибка). B33 H2.5: в CI этот файл и src/weakening_guard.py
берутся из БАЗОВОЙ ревизии (git show <база>:...), правка сторожа действует со следующего блока. Печатает по строке на
находку (`WEAKENING red <ключ>` / `WEAKENING allowed <ключ>`), неиспользованные разрешения (`WEAKENING unused <ключ>`)
и итог `WEAKENING_SUMMARY base=<tree sha12> head=<tree sha12> test_files=<n>/<n> findings=<n> red=<n> allowed=<n>
guarded=<n>` (guarded -- находки охраняемой зоны, они входят в findings).
B33 v2 C3 (B32m D1): --base auto -- база = git merge-base <head> refs/remotes/origin/oko-live (последний
опубликованный коммит, в CI -- checkout с fetch-depth 0); базу определить нельзя -- код 2 (красный), молчаливого
HEAD~1 нет. B33 v2 C4 (B32m D2): --collect -- обе ревизии экспортируются (git archive) во временную папку и
собираются `python -m pytest --collect-only -q src`; исчезнувший node ID (в т.ч. вариант parametrize) --
removed_test; сбой сборки любой стороны -- код 2.
Код возврата: 0 -- ослаблений без разрешения нет; 1 -- есть (красный); 2 -- ошибка (нет базы: в CI checkout
с fetch-depth 0 и origin/oko-live; сбой --collect; плохой список разрешений) -- тоже красный."""
import argparse
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import weakening_guard as wg  # noqa: E402


_CHILD_DROP = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH", "PYTHONWARNINGS",
               "PYTHONINSPECT", "PYTHONEXECUTABLE", "PYTHONNOUSERSITE", "PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST",
               "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG", "OKO_NOTIFY_OFF", "OKO_SIGMA_LIVE",
               "OKO_REQUEST_TS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
               "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CONFIG_PARAMETERS",
               "GIT_CONFIG_COUNT", "GIT_CEILING_DIRECTORIES")


def child_env() -> dict:
    """B33 attempt 4 (item 3): hermetic environment of a child (git / pytest): without the parent variables that change
    Python / pytest / git (PYTHONHOME, PYTHONPATH, PYTEST_ADDOPTS -q would empty --collect-only, OKO_B33_*, GIT_DIR ...),
    with UTF-8 stdio."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in _CHILD_DROP
           and not k.upper().startswith(("OKO_B33_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def _git(repo: Path, *args: str) -> bytes:
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *args], capture_output=True, env=child_env())
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} rc={r.returncode}")
    return r.stdout


def all_blobs(repo: Path, rev: str) -> dict:
    """{путь: blob} всех файлов ревизии."""
    out = {}
    for ln in _git(repo, "ls-tree", "-r", "-z", rev).split(b"\0"):
        if not ln:
            continue
        meta, path = ln.split(b"\t", 1)
        mode, typ, blob = meta.decode("ascii").split()
        if typ == "blob":
            out[path.decode("utf-8")] = blob
    return out


def read_blob(repo: Path, blob: str) -> str:
    return _git(repo, "cat-file", "blob", blob).decode("utf-8-sig", errors="replace")


def test_files(repo: Path, rev: str, blobs: dict = None) -> dict:
    """{путь: (blob, исходник)} тестовых файлов ревизии."""
    blobs = all_blobs(repo, rev) if blobs is None else blobs
    return {p: (b, read_blob(repo, b)) for p, b in blobs.items() if wg.is_test_path(p)}


PUBLISHED = "refs/remotes/origin/oko-live"


def resolve_auto(repo: Path, head: str = "HEAD") -> str:
    """B33 v2 C3: база = merge-base(head, опубликованная ветка); нельзя определить -> ''."""
    try:
        return _git(repo, "merge-base", head, PUBLISHED).decode("ascii", "replace").strip()
    except RuntimeError:
        return ""


def collect_ids(repo: Path, rev: str) -> list:
    """Собранные node ID ревизии: git archive -> временная папка -> pytest --collect-only -q src. Сбой -> RuntimeError."""
    with tempfile.TemporaryDirectory(prefix="wg_collect_") as td:
        with tarfile.open(fileobj=io.BytesIO(_git(repo, "archive", "--format=tar", rev))) as tf:
            tf.extractall(td)
        r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "src"],
                           cwd=td, capture_output=True, timeout=900, env=child_env())
        if r.returncode not in (0, 5):
            raise RuntimeError(f"collect rc={r.returncode}")
        ids = [ln.strip() for ln in r.stdout.decode("utf-8", "replace").splitlines() if "::" in ln and not ln.startswith(" ")]
        if not ids and any(Path(td, "src").glob("test_*.py")):     # B33 attempt 4: 0 node IDs with test files = broken collect, not "nothing removed"
            raise RuntimeError("collect gave 0 node IDs although src/test_*.py exist")
        return ids


def read_allow(repo: Path, tree: str, rels: list) -> tuple:
    """Оба списка разрешений из ревизии tree -> (allow, ошибки). Нет файла -- пусто."""
    allow, errs, seen16 = {}, [], {}
    for rel in rels:
        try:
            lines = _git(repo, "show", f"{tree}:{rel}").decode("utf-8-sig").splitlines()
        except RuntimeError:
            lines = []
        a, e = wg.parse_allow(lines)
        errs += [f"{rel}: {x}" for x in e]
        for k, v in a.items():
            if k in allow:
                errs.append(f"{rel}: key {k} already in {seen16.get(allow[k][3], '?')}")
            elif v[3] in seen16:
                errs.append(f"{rel}: entry_sha16 {v[3]} already in {seen16[v[3]]}")
            else:
                allow[k] = v
                seen16[v[3]] = rel
    return allow, errs


def _utf8_stdio() -> None:
    """B33 v2 попытка 3: вывод CLI всегда UTF-8. Под `python -I` (PYTHONIOENCODING игнорируется) на Windows консоль
    и канал -- cp1251 / cp866; скрипты блока и тесты читают вывод как UTF-8 (selfrun 25.11 09:09: UnicodeDecodeError)."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _say(s: str) -> None:
    """B33 attempt 4: CLI output is ASCII (non-ASCII data -> backslash escapes); UTF-8 stdio stays as the second line."""
    print(s.encode("ascii", "backslashreplace").decode("ascii"))


def main(argv=None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(prog="weakening_guard")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default="HEAD~1")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--allow", default=".github/test_weakening_allow.txt")
    ap.add_argument("--guarded-allow", default=".github/guarded_change_allow.txt")
    ap.add_argument("--collect", action="store_true", help="B33 v2 C4: compare the collected node IDs (pytest --collect-only)")
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    if a.base == "auto":                                  # B33 v2 C3: база = опубликованный коммит
        a.base = resolve_auto(repo, a.head)
        if not a.base:
            _say(f"WEAKENING_ERROR base auto: merge-base {a.head} {PUBLISHED} not found (checkout fetch-depth 0 and the "
                  f"published branch are needed) -- red, no silent HEAD~1")
            return 2
        _say(f"WEAKENING_BASE auto = merge-base {a.head} oko-live = {a.base[:12]}")
    try:
        b = _git(repo, "rev-parse", "--verify", "-q", a.base + "^{tree}").decode("ascii", "replace").strip()
        h = _git(repo, "rev-parse", "--verify", "-q", a.head + "^{tree}").decode("ascii", "replace").strip()
    except RuntimeError:
        _say(f"WEAKENING_ERROR base or head not found ({a.base} / {a.head}): the base commit is needed "
              f"(actions/checkout fetch-depth: 0)")
        return 2
    # списки разрешений -- из проверяемой ревизии (head), не из рабочего дерева
    allow, errs = read_allow(repo, h, [a.allow, a.guarded_allow])
    if errs:
        for e in errs:
            _say(f"WEAKENING_ALLOW_ERROR {e}")
        return 2
    ball, hall = all_blobs(repo, b), all_blobs(repo, h)
    base, head = test_files(repo, b, ball), test_files(repo, h, hall)
    guarded = wg.compare_guarded(ball, hall, lambda blob: read_blob(repo, blob))
    found = sorted(set(wg.compare(base, head)) | set(guarded))
    if a.collect:                                         # B33 v2 C4: исчезнувшие собранные случаи
        try:
            cb, ch = collect_ids(repo, b), collect_ids(repo, h)
        except (RuntimeError, OSError, subprocess.SubprocessError, tarfile.TarError) as e:
            _say(f"WEAKENING_ERROR collect-only failed ({type(e).__name__}: {e}) -- red")
            return 2
        _say(f"WEAKENING_COLLECT base={len(cb)} head={len(ch)}")
        found = sorted(set(found) | set(wg.compare_cases(cb, ch)))
    j = wg.judge(found, allow)
    for k in j["red"]:
        _say(f"WEAKENING red {k}")
    for k in j["allowed"]:
        _say(f"WEAKENING allowed {k} | {allow[k][0]} | {allow[k][1]} | {allow[k][2]} | {allow[k][3]}")
    for k in j["unused_allow"]:
        _say(f"WEAKENING unused {k}")
    _say(f"WEAKENING_SUMMARY base={b[:12]} head={h[:12]} test_files={len(base)}/{len(head)} findings={len(found)} "
          f"red={len(j['red'])} allowed={len(j['allowed'])} guarded={len(guarded)}")
    return 0 if j["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
