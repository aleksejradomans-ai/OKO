"""Контракты тестов после GAME_MODE v2 и TV_HUES_FORMS (02.11.2026).

1. src/test_router.py::test_intent_only_with_subject_and_rechecked: живой случай 30.10 «Подсветка жёлтая»
   был примером «правила не узнали -> модель смысла». После TV_HUES_FORMS правила его узнают (так и нужно:
   в игре модели смысла нет). Тест разделяется на два: живой случай -> tv_rule без вызова модели;
   проверка модели смысла -> фраза, которую правила по-прежнему не знают («подсветка цвета солнца»).
2. export_public.py (приватный, вне git, 05BF44641849F08B): src/game_mode.py в WHITELIST -- его импортируют
   bot и governor (test_export_public::test_live_repo_whitelist_has_no_blocking_gaps). Отдельной строкой
   после строки 32 («...src/governor.py, src/io_retry.py,»).

test_router.py без хэша (его нет у Claude): якорь должен встретиться ровно один раз, иначе STOP; повтор -> ALREADY.
Бэкап *.bak_contracts_0211. Запуск из C:\\oko: python tools\\patch_contracts_0211.py [--apply]
Коды: 0 OK/ALREADY/DRY-OK, 2 STOP (ничего не записано).
"""
import hashlib
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

ROUTER_OLD = '''def test_intent_only_with_subject_and_rechecked():
    calls = []

    def intent(t):
        calls.append(t)
        return "подсветка желтый"

    r = _route("Подсветка жёлтая", intent)        # живой случай 30.10 15:27 (голос вне /tv)
    assert r.kind == R.TV and r.text == "подсветка желтый"
    assert r.note == "Понял как «подсветка желтый». " and r.reason == "tv_intent:amb_colour"
    assert calls == ["подсветка желтая"]
'''
ROUTER_NEW = '''def test_live_case_colour_by_rule_without_intent():
    """Живой случай 30.10 15:27 «Подсветка жёлтая»: с 02.11 (TV_HUES_FORMS) -- правило, модель не зовётся
    (в игровом режиме модели смысла нет)."""
    calls = []
    r = _route("Подсветка жёлтая", lambda t: calls.append(t) or "подсветка красный")
    assert r.kind == R.TV and r.reason == "tv_rule:amb_colour"
    assert calls == []


def test_intent_only_with_subject_and_rechecked():
    calls = []

    def intent(t):
        calls.append(t)
        return "подсветка желтый"

    r = _route("Подсветка цвета солнца", intent)   # правила не знают -> модель смысла, ответ перепроверен
    assert r.kind == R.TV and r.text == "подсветка желтый"
    assert r.note == "Понял как «подсветка желтый». " and r.reason == "tv_intent:amb_colour"
    assert calls == ["подсветка цвета солнца"]
'''



def h16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16].upper()


def _read(p: pathlib.Path):
    raw = p.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = (raw[3:] if bom else raw).decode("utf-8")
    return raw, bom, text, ("\r\n" if "\r\n" in text else "\n")


def _enc(text: str, bom: bool) -> bytes:
    out = text.encode("utf-8")
    return b"\xef\xbb\xbf" + out if bom else out


def plan_router():
    p = ROOT / "src" / "test_router.py"
    if not p.is_file():
        return None, "STOP missing src\\test_router.py"
    raw, bom, text, nl = _read(p)
    new_n = ROUTER_NEW.replace("\n", nl)
    if new_n in text:
        return None, f"ALREADY src\\test_router.py {h16(raw)}"
    old_n = ROUTER_OLD.replace("\n", nl)
    c = text.count(old_n)
    if c != 1:
        return None, f"STOP src\\test_router.py anchor count={c}"
    return (p, raw, _enc(text.replace(old_n, new_n), bom)), None


WL_FILE_HASH = "05BF44641849F08B"   # export_public.py на 02.11 15:29 (вывод Оператора)
WL_OLD = '''    "src/data_hygiene.py", "src/db.py", "src/governor.py", "src/io_retry.py",
'''
WL_NEW = '''    "src/data_hygiene.py", "src/db.py", "src/governor.py", "src/io_retry.py",
    "src/game_mode.py",   # 02.11: GAME_MODE v2 (bot.py и governor.py импортируют)
'''


def plan_whitelist():
    p = ROOT / "export_public.py"
    if not p.is_file():
        return None, "STOP missing export_public.py"
    raw, bom, text, nl = _read(p)
    if re.search(r'["\']src/game_mode\.py["\']', text):
        return None, f"ALREADY export_public.py {h16(raw)}"
    if h16(raw) != WL_FILE_HASH:
        return None, f"STOP hash export_public.py have={h16(raw)} want={WL_FILE_HASH}"
    old_n = WL_OLD.replace("\n", nl)
    c = text.count(old_n)
    if c != 1:
        return None, f"STOP export_public.py anchor count={c}"
    return (p, raw, _enc(text.replace(old_n, WL_NEW.replace("\n", nl)), bom)), None


def main() -> int:
    apply = "--apply" in sys.argv
    plans = []
    for fn in (plan_router, plan_whitelist):
        plan, msg = fn()
        if msg and msg.startswith("STOP"):
            print(msg)
            return 2
        if msg:
            print(msg)
        if plan:
            p, raw, new = plan
            print(f"{'APPLY' if apply else 'DRY-OK'} {p.relative_to(ROOT)} {h16(raw)} -> {h16(new)}")
            plans.append(plan)
    if not apply:
        return 0
    for p, raw, new in plans:
        shutil.copy2(p, p.with_name(p.name + ".bak_contracts_0211"))
    for p, raw, new in plans:
        p.write_bytes(new)
        print(f"WROTE {p.relative_to(ROOT)} {h16(p.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
