"""Проверка комплекта клипов голоса по manifest (офлайн): нет / лишние / формат / длительность / клиппинг / пик.

  python tools/voice_check.py --manifest <manifest.json> --clips <папка клипов> [--list 20]
Вывод: строки MISSING / EXTRA / BAD (первые --list каждого вида) и итог CHECK одной строкой; код 0 -- комплект
полный и чистый, 1 -- нет.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import voice_clips as vc  # noqa: E402


def out(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="voice_check")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--clips", required=True)
    ap.add_argument("--list", type=int, default=20)
    a = ap.parse_args(argv)
    r = vc.check_set(vc.load_manifest(Path(a.manifest)), Path(a.clips))
    for x in r["missing"][:a.list]:
        out(f"MISSING {x}")
    for x in r["extra"][:a.list]:
        out(f"EXTRA {x}")
    for name, why in r["bad"][:a.list]:
        out(f"BAD {name} {why}")
    out(f"CHECK total={r['total']} present={r['present']} ok={r['ok']} missing={len(r['missing'])} "
        f"extra={len(r['extra'])} bad={len(r['bad'])} complete={r['complete']}")
    return 0 if r["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
