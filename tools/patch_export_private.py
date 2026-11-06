"""EXPORT_PRIVATE (01.11.2026): export_public.py -- модули этапов A-C приватны (решение Оператора 01.11 16:46).

Добавляет в ALLOWED_GAPS пары публичного bot.py с приватными модулями:
  capabilities.py, proof_registry.py, router.py, event_log.py.
WHITELIST не меняется (142).

Файл export_public.py в чат не присылался, поэтому патч -- по СМЫСЛУ, не по байтам:
  1. ast: ALLOWED_GAPS -- одно присваивание литералом множества (ast.Set);
  2. его значение == известным 4 парам (по test_export_public.py) -- иначе стоп;
  3. новые пары вставляются сразу после открывающей «{» литерала;
  4. импорт результата: ALLOWED_GAPS == старые 4 + новые 4, WHITELIST не изменился.
Коды выхода: 0 -- OK/ALREADY; 2 -- предусловие; 3 -- структура; 4 -- проверка после (откат).
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "export_public.py"
OLD = {("src/conftest.py", "src/snapshot_manager.py"), ("src/bot.py", "src/device_watch.py"),
       ("src/bot.py", "src/garmin_report.py"), ("src/bot.py", "src/net_context.py")}
NEW = [("src/bot.py", "src/capabilities.py"), ("src/bot.py", "src/proof_registry.py"),
       ("src/bot.py", "src/router.py"), ("src/bot.py", "src/event_log.py")]
BOM = b"\xef\xbb\xbf"


def h16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16].upper()


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("_export_public_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    if not TARGET.is_file():
        print(f"TARGET_NOT_FOUND {TARGET}")
        return 2
    raw = TARGET.read_bytes()
    has_bom = raw.startswith(BOM)
    text = (raw[len(BOM):] if has_bom else raw).decode("utf-8")
    crlf = "\r\n" in text
    norm = text.replace("\r\n", "\n")

    before = _load(TARGET)
    current = set(before.ALLOWED_GAPS)
    if set(NEW) <= current:
        print(f"ALREADY | {h16(raw)}")
        return 0
    if current != OLD:
        print(f"PRECONDITION ALLOWED_GAPS differs: extra={sorted(current - OLD)} missing={sorted(OLD - current)}")
        return 2
    wl_before = list(before.WHITELIST)

    tree = ast.parse(norm)
    nodes = [n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
             and any(isinstance(t, ast.Name) and t.id == "ALLOWED_GAPS"
                     for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    if len(nodes) != 1 or not isinstance(nodes[0].value, ast.Set):
        print(f"STRUCTURE ALLOWED_GAPS assignments={len(nodes)} "
              f"type={type(nodes[0].value).__name__ if nodes else None}")
        return 3
    val = nodes[0].value
    lines = norm.split("\n")
    open_b = lines[val.lineno - 1].encode("utf-8")
    pos = val.col_offset                                 # «{» литерала (ast: байтовые смещения UTF-8)
    if open_b[pos:pos + 1] != b"{":
        print("STRUCTURE opening brace not found")
        return 3
    # Новые пары -- СРАЗУ после «{»: так не трогаются хвостовые запятые и комментарии старых пар.
    insert = "\n" + "".join(
        f'    ("{a}", "{b}"),  # 01.11: этапы A-C приватны (решение Оператора)\n' for a, b in NEW) + "    "
    lines[val.lineno - 1] = (open_b[:pos + 1] + insert.encode("utf-8") + open_b[pos + 1:]).decode("utf-8")
    out = "\n".join(lines)
    out = out.replace("\n", "\r\n") if crlf else out
    out_bytes = (BOM if has_bom else b"") + out.encode("utf-8")

    backup = TARGET.with_name(TARGET.name + ".before_private")
    i = 2
    while backup.exists():
        backup = TARGET.with_name(f"{TARGET.name}.before_private{i}")
        i += 1
    shutil.copy2(TARGET, backup)
    tmp = TARGET.with_name(TARGET.name + ".private_tmp")
    tmp.write_bytes(out_bytes)
    os.replace(tmp, TARGET)
    try:
        after = _load(TARGET)
        ok = set(after.ALLOWED_GAPS) == OLD | set(NEW) and list(after.WHITELIST) == wl_before
    except Exception as e:  # noqa: BLE001
        ok, e_txt = False, f"{type(e).__name__}: {e}"
    else:
        e_txt = "content check failed"
    if not ok:
        shutil.copy2(backup, TARGET)
        print(f"CHECK_FAIL rolled back from {backup.name}: {e_txt}")
        return 4
    print(f"PATCHED export_public.py | {h16(raw)} -> {h16(out_bytes)} | bom={has_bom} crlf={crlf} | "
          f"backup={backup.name} | ALLOWED_GAPS {len(OLD)} -> {len(OLD) + len(NEW)}, WHITELIST {len(wl_before)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
