"""02.11: src/test_export_public.py (приватный) -- счётчик WHITELIST 142 -> 143 (+src/game_mode.py).

game_mode.py публикуется ВЫНУЖДЕННО: его импортирует governor.py (публичный), ALLOWED_GAPS для ядра
сломал бы публичный импорт. Содержимое без личных данных (пути флагов, тексты режима).
Хэш «до» DB439EB1DAFEA186 (вывод Оператора 15:32), якорь -- строка 55. Бэкап *.bak_ep_count_0211.
Запуск из C:\\oko: python tools\\patch_ep_count_0211.py [--apply]. Коды: 0 OK/ALREADY/DRY-OK, 2 STOP.
"""
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
P = ROOT / "src" / "test_export_public.py"
BEFORE = "DB439EB1DAFEA186"
OLD = "    assert len(ep.WHITELIST) == len(set(ep.WHITELIST)) == 142  # 30.10:"
NEW = "    assert len(ep.WHITELIST) == len(set(ep.WHITELIST)) == 143  # 02.11: +1 game_mode (governor imports it); 30.10:"


def h16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16].upper()


def main() -> int:
    apply = "--apply" in sys.argv
    if not P.is_file():
        print("STOP missing src\\test_export_public.py")
        return 2
    raw = P.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = (raw[3:] if bom else raw).decode("utf-8")
    if NEW in text:
        print(f"ALREADY src\\test_export_public.py {h16(raw)}")
        return 0
    if h16(raw) != BEFORE:
        print(f"STOP hash src\\test_export_public.py have={h16(raw)} want={BEFORE}")
        return 2
    if text.count(OLD) != 1:
        print(f"STOP anchor count={text.count(OLD)}")
        return 2
    out = text.replace(OLD, NEW).encode("utf-8")
    new = b"\xef\xbb\xbf" + out if bom else out
    print(f"{'APPLY' if apply else 'DRY-OK'} src\\test_export_public.py {BEFORE} -> {h16(new)}")
    if apply:
        shutil.copy2(P, P.with_name(P.name + ".bak_ep_count_0211"))
        P.write_bytes(new)
        print(f"WROTE src\\test_export_public.py {h16(P.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
