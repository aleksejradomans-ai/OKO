#!/usr/bin/env python3
"""apply_marker_gate.py -- закрывает обход судьи через префикс "[ERR:".

Было: в _run_request ответ, НАЧИНАЮЩИЙСЯ с "[ERR:", возвращался без судьи
(raw.startswith("[ERR:")). Текст вида "[ERR: X] + произвольный текст" проходил
мимо проверки манифеста.
Стало: судью пропускает только ТОЧНОЕ совпадение всего ответа с известным
служебным маркером "[ERR: <CODE>]" (CODE из SERVICE_CODES). Всё остальное --
в судью. Для вывода структурных наблюдений (точка B) тот же критерий.
Не меняет: _probe_succeeded (там префикс консервативен -- проба "не успех").

Три якоря, каждый ровно по одному разу; все-или-ничего.
Dry-run по умолчанию. --apply пишет файл, бэкап: governor.py.bak_marker_gate
Использование:  python apply_marker_gate.py [--src C:\\oko\\src] [--apply]
"""
from __future__ import annotations

import argparse
import difflib
import pathlib
import shutil
import sys

MARKER = "def is_service_marker"

OLD_HELPER = "    return marker\n\n\n# /discuss — расширенный контекст"
NEW_HELPER = '''    return marker


_SERVICE_MARKERS = frozenset(f"[ERR: {_c}]" for _c in SERVICE_CODES)


def is_service_marker(text: str) -> bool:
    """True ТОЛЬКО если весь текст -- ровно один известный служебный маркер
    "[ERR: <CODE>]" (пробелы по краям допустимы). Префиксной проверки
    startswith("[ERR:") недостаточно: маркер с приписанным произвольным текстом
    прошёл бы мимо судьи. Незнакомый код или лишний текст -> False -> к судье."""
    return isinstance(text, str) and text.strip() in _SERVICE_MARKERS


# /discuss — расширенный контекст'''

OLD_RAW = (
    '        if raw.startswith("[ERR:"):\n'
    '            self.monitor.record_event(ComplianceEvent.UNCHECKED)\n'
    '            return raw\n'
)
NEW_RAW = (
    '        # Только ТОЧНЫЙ служебный маркер (is_service_marker), не префикс "[ERR:".\n'
    '        if is_service_marker(raw):\n'
    '            self.monitor.record_event(ComplianceEvent.UNCHECKED)\n'
    '            return raw\n'
)

OLD_B = '        if not final.startswith("[ERR:"):\n            self._observe_structure(final, point="B"'
NEW_B = '        if not is_service_marker(final):\n            self._observe_structure(final, point="B"'

EDITS = [
    ("helper is_service_marker", OLD_HELPER, NEW_HELPER),
    ("_run_request: гейт судьи", OLD_RAW, NEW_RAW),
    ("_run_request: точка B", OLD_B, NEW_B),
]


def apply_edits(text: str) -> str:
    if MARKER in text:
        raise SystemExit("[SKIP] is_service_marker уже есть -- патч применён")
    for name, old, _ in EDITS:
        n = text.count(old)
        if n != 1:
            raise SystemExit(f"[FAIL] якорь '{name}': найдено {n}, ожидалось 1")
    for name, old, new in EDITS:
        text = text.replace(old, new)
        print(f"[OK] {name}")
    return text


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

    backup = path.with_name(path.name + ".bak_marker_gate")
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
