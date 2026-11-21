"""CLI закрытия блока (src/block_finish.py): METRIC-строки блока -> строка run_metrics -> чтение назад -> SIGNAL.

  python tools/block_finish.py --metrics <file | -> --db <path>

Вход -- файл (или stdin) со строками `METRIC <ключ>=<значение>`; прочие строки не читаются.
Вывод: `BLOCK_FINISH recorded=<True|False> new=<..> run_id=<..> reason=<..>`, при записи -- строка «призрака»
`GHOST kind=<run|stage|promote|elevated> ...` (B30 P7, только цифры), и последней
`SIGNAL: <n> - <GREEN|RED|ASK|MANUAL>; ...`. `SIGNAL: 1` -- только если строка записана и прочитана назад.
Код возврата: 0 -- строка записана и прочитана назад; 1 -- нет (итог в строке SIGNAL всё равно напечатан).
База указывается явно (--db): закрытие блока не должно зависеть от того, откуда запущен этот файл."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import block_finish as bf  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="block_finish")
    ap.add_argument("--metrics", required=True)
    ap.add_argument("--db", required=True)
    a = ap.parse_args(argv)
    try:
        raw = sys.stdin.read() if a.metrics == "-" else Path(a.metrics).read_text(encoding="utf-8-sig")
        lines = raw.splitlines()
    except OSError:
        lines = []
    r = bf.finish(lines, Path(a.db))
    out = (f"BLOCK_FINISH recorded={r['recorded']} new={r['new']} run_id={r['run_id']} attempt={r['attempt']} "
           f"outcome={r['outcome']} reason={r['reason'] or '-'}\n"
           + (r["ghost"] + "\n" if r.get("ghost") else "")
           + r["line"] + "\n")
    sys.stdout.buffer.write(out.encode("utf-8"))
    return 0 if r["recorded"] else 1


if __name__ == "__main__":
    sys.exit(main())
