#!/usr/bin/env python3
"""apply_judge_trace.py -- JUDGE_TRACE для src/governor.py::_verify_compliance.

Любой вердикт судьи, кроме PASS (FAIL / HALT / ERROR), пишется отдельной
строкой WARNING `[JUDGE_TRACE]`: сырой ответ судьи + первые 400 символов
проверяемого текста. Нужен, чтобы понять, ПОЧЕМУ судья блокирует конкретный
ответ (например, ложный HALT на вопросе про коды ядра). Уровень WARNING:
process_watchdog эти строки не считает. Поведение ядра не меняется.

Dry-run по умолчанию. --apply пишет файл, бэкап: governor.py.bak_judge_trace
Использование:  python apply_judge_trace.py [--src C:\\oko\\src] [--apply]
"""
from __future__ import annotations

import argparse
import difflib
import pathlib
import shutil
import sys

MARKER = "JUDGE_TRACE"

OLD = "        return self._parse_verdict(result, self.manifesto)\n"
NEW = '''        parsed = self._parse_verdict(result, self.manifesto)
        if parsed.verdict != ComplianceVerdict.PASS:
            # JUDGE_TRACE: диагностика ложных блокировок. WARNING, не ERROR --
            # не попадает в счётчик process_watchdog.
            logger.warning(
                "[JUDGE_TRACE] verdict=%s rule=%s raw=%r checked_text=%r",
                parsed.verdict.value, parsed.violation_id,
                (result or "")[:120], text[:400],
            )
        return parsed
'''


def apply_edits(text: str) -> str:
    if MARKER in text:
        raise SystemExit("[SKIP] маркер JUDGE_TRACE уже есть -- патч применён")
    n = text.count(OLD)
    if n != 1:
        raise SystemExit(f"[FAIL] якорь '_verify_compliance return': найдено {n}, ожидалось 1")
    print("[OK] _verify_compliance: трассировка не-PASS вердиктов")
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

    backup = path.with_name(path.name + ".bak_judge_trace")
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
