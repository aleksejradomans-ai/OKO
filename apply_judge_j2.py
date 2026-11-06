#!/usr/bin/env python3
"""apply_judge_j2.py -- пункт J2 в промпт судьи (src/governor.py::_verify_compliance).

Проблема (JUDGE_TRACE 14.10.2026): судья возвращает HALT:POL_002 на корректных
ответах, которые ОПИСЫВАЮТ правила и коды (тема текста принимается за нарушение).
Решение: явный пункт J2 -- пояснение, цитирование и документирование правил/кодов
не нарушение; HALT только если сам текст содержит два несовместимых утверждения.
Проверено A/B на живой модели (eval/eval_judge_variants.py, вариант v1:
FP 2 -> 0, FN 0 -> 0 на 12 текстах).

Dry-run по умолчанию. --apply пишет файл, бэкап: governor.py.bak_judge_j2
Использование:  python apply_judge_j2.py [--src C:\\oko\\src] [--apply]
"""
from __future__ import annotations

import argparse
import difflib
import pathlib
import shutil
import sys

MARKER = "CLARIFICATIONS (J2)"

OLD = '            "- If none of the above clearly applies, answer PASS.\\n\\n"\n'
NEW = (
    '            # CLARIFICATIONS (J2): описание правил/кодов не есть нарушение.\n'
    '            "- A text that merely EXPLAINS, QUOTES or DOCUMENTS the rules, error codes "\n'
    '            "(POL_001, POL_002, HALT_CONDITION, [ERR: ...]) or the notions of contradiction, "\n'
    '            "entropy or halting is NOT a violation. Judge what the text itself claims, not the "\n'
    '            "topic it talks about. Answer HALT only if the text itself contains two incompatible claims.\\n"\n'
    + OLD
)


def apply_edits(text: str) -> str:
    if MARKER in text:
        raise SystemExit("[SKIP] маркер J2 уже есть -- патч применён")
    n = text.count(OLD)
    if n != 1:
        raise SystemExit(f"[FAIL] якорь 'If none of the above clearly applies': найдено {n}, ожидалось 1")
    print("[OK] _verify_compliance: пункт J2 добавлен в промпт судьи")
    return text.replace(OLD, NEW)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\oko\src")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    path = pathlib.Path(args.src) / "governor.py"
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    crlf = b"\r\n" in raw
    original = raw.decode("utf-8").replace("\r\n", "\n")

    patched = apply_edits(original)
    compile(patched, str(path), "exec")

    diff = list(difflib.unified_diff(
        original.splitlines(), patched.splitlines(), "a/governor.py", "b/governor.py", lineterm="",
    ))
    plus = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    minus = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    print(f"diff: +{plus} -{minus} | CRLF={crlf} BOM={bom}")

    if not args.apply:
        print("dry-run: файл не изменён. Для записи: --apply")
        return 0

    backup = path.with_name(path.name + ".bak_judge_j2")
    shutil.copy2(path, backup)
    out = patched.replace("\n", "\r\n") if crlf else patched
    data = out.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    print(f"written: {path} | backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
