"""CLI сторожа ослабления тестов (src/weakening_guard.py): база (родитель снимка) против снимка, по git.

  python tools/weakening_guard.py [--repo .] [--base HEAD~1] [--head HEAD] [--allow .github/test_weakening_allow.txt]

Читает тестовые файлы обеих ревизий (коммит или дерево) из git (git ls-tree / cat-file), не из рабочего дерева;
список разрешений -- из ревизии head. Печатает по строке на
находку (`WEAKENING red <ключ>` / `WEAKENING allowed <ключ>`), неиспользованные разрешения (`WEAKENING unused <ключ>`)
и итог `WEAKENING_SUMMARY base=<tree sha12> head=<tree sha12> test_files=<n>/<n> findings=<n> red=<n> allowed=<n>`.
Код возврата: 0 -- ослаблений без разрешения нет; 1 -- есть (красный); 2 -- ошибка (нет базы: в CI checkout
с fetch-depth 2; плохой список разрешений) -- тоже красный."""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import weakening_guard as wg  # noqa: E402


def _git(repo: Path, *args: str) -> bytes:
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *args], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} rc={r.returncode}")
    return r.stdout


def test_files(repo: Path, rev: str) -> dict:
    """{путь: (blob, исходник)} тестовых файлов ревизии."""
    out = {}
    for ln in _git(repo, "ls-tree", "-r", "-z", rev).split(b"\0"):
        if not ln:
            continue
        meta, path = ln.split(b"\t", 1)
        mode, typ, blob = meta.decode().split()
        p = path.decode("utf-8")
        if typ == "blob" and wg.is_test_path(p):
            out[p] = (blob, _git(repo, "cat-file", "blob", blob).decode("utf-8-sig", errors="replace"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="weakening_guard")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default="HEAD~1")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--allow", default=".github/test_weakening_allow.txt")
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    try:
        b = _git(repo, "rev-parse", "--verify", "-q", a.base + "^{tree}").decode().strip()
        h = _git(repo, "rev-parse", "--verify", "-q", a.head + "^{tree}").decode().strip()
    except RuntimeError:
        print(f"WEAKENING_ERROR base or head not found ({a.base} / {a.head}): the parent snapshot is needed "
              f"(actions/checkout fetch-depth: 2)")
        return 2
    try:                                    # список разрешений -- из проверяемой ревизии (head), не из рабочего дерева
        lines = _git(repo, "show", f"{h}:{a.allow}").decode("utf-8-sig").splitlines()
    except RuntimeError:
        lines = []
    allow, errs = wg.parse_allow(lines)
    if errs:
        for e in errs:
            print(f"WEAKENING_ALLOW_ERROR {e}")
        return 2
    base, head = test_files(repo, b), test_files(repo, h)
    found = wg.compare(base, head)
    j = wg.judge(found, allow)
    for k in j["red"]:
        print(f"WEAKENING red {k}")
    for k in j["allowed"]:
        print(f"WEAKENING allowed {k} | {allow[k][0]} | {allow[k][1]} | {allow[k][2]}")
    for k in j["unused_allow"]:
        print(f"WEAKENING unused {k}")
    print(f"WEAKENING_SUMMARY base={b[:12]} head={h[:12]} test_files={len(base)}/{len(head)} findings={len(found)} "
          f"red={len(j['red'])} allowed={len(j['allowed'])}")
    return 0 if j["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
