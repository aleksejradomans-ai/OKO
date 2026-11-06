#!/usr/bin/env python3
"""apply_explain_fix.py -- _explain_error (src/bot.py) объясняет только строку-маркер.

Было: re.search по всему ответу -- цитата "[ERR: SYSTEM_DEGRADED]" в середине
объяснения получала приписку "Контур в режиме деградации... Повторите позже",
текст ломался (замечено в живом ответе 14.10.2026).
Стало: пояснение добавляется только если ПОСЛЕДНЯЯ непустая строка ответа --
ровно известный маркер "[ERR: КОД]". Так работает и при уведомлении слоя B,
которое _with_notice добавляет В НАЧАЛО ответа. Любой другой текст не трогается.

Один якорь (тело _explain_error), все-или-ничего.
Dry-run по умолчанию. --apply пишет файл, бэкап: bot.py.bak_explain_fix
Использование:  python apply_explain_fix.py [--src C:\\oko\\src] [--apply]
"""
from __future__ import annotations

import argparse
import difflib
import pathlib
import shutil
import sys

MARKER = "MARKER_LINE_ONLY"

OLD = (
    '    m = re.search(r"\\[ERR: (\\w+)\\]", response or "")\n'
    '    if not m or m.group(1) not in _ERR_TEXT:\n'
    '        return response\n'
    '    return response.replace(m.group(0), f"{m.group(0)} {_ERR_TEXT[m.group(1)]}", 1)\n'
)
NEW = (
    '    # MARKER_LINE_ONLY: объясняем только последнюю непустую строку, если она --\n'
    '    # ровно известный маркер. Цитаты маркера внутри текста не трогаем.\n'
    '    if not isinstance(response, str) or not response:\n'
    '        return response\n'
    '    lines = response.split("\\n")\n'
    '    idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)\n'
    '    if idx is None:\n'
    '        return response\n'
    '    m = re.fullmatch(r"\\[ERR: (\\w+)\\]", lines[idx].strip())\n'
    '    if not m or m.group(1) not in _ERR_TEXT:\n'
    '        return response\n'
    '    lines[idx] = f"{m.group(0)} {_ERR_TEXT[m.group(1)]}"\n'
    '    return "\\n".join(lines)\n'
)


def apply_edits(text: str) -> str:
    if MARKER in text:
        raise SystemExit("[SKIP] маркер MARKER_LINE_ONLY уже есть -- патч применён")
    n = text.count(OLD)
    if n != 1:
        raise SystemExit(f"[FAIL] якорь '_explain_error body': найдено {n}, ожидалось 1")
    print("[OK] _explain_error: объяснение только для строки-маркера")
    return text.replace(OLD, NEW)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\oko\src")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    path = pathlib.Path(args.src) / "bot.py"
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    crlf = b"\r\n" in raw
    original = raw.decode("utf-8").replace("\r\n", "\n")

    patched = apply_edits(original)
    compile(patched, str(path), "exec")

    diff = list(difflib.unified_diff(
        original.splitlines(), patched.splitlines(), "a/bot.py", "b/bot.py", lineterm="",
    ))
    plus = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    minus = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    print(f"diff: +{plus} -{minus} | CRLF={crlf} BOM={bom}")

    if not args.apply:
        print("dry-run: файл не изменён. Для записи: --apply")
        return 0

    backup = path.with_name(path.name + ".bak_explain_fix")
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
