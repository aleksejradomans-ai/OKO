"""TV_HUES_FORMS (02.11.2026): цвета подсветки во всех родах -- детерминированно, без модели смысла.

Живой случай 02.11 15:10 (игра, GAME_MODE v2): «podsvetka zeltaja» -> [ROUTE] kind=brain -> заглушка.
Причина: в tv_commands.HUES были только «желтый/желтую» (у части цветов -- только мужской род), а
словарь транслита tv_nav.vocabulary() строится из КЛЮЧЕЙ HUES -> «zeltaja» не сводился ни к чему.
Раньше фразу спасала модель смысла (tv_intent, LLM), в игре она отключена.
Правка: HUES -- формы -ый/-ая/-ую/-ое (-ий/-яя/-юю/-ее) для всех цветов; белый -- по основе «бел».

Якорь + хэш «до/после», all-or-nothing, идемпотентно, бэкап *.bak_tv_hues.
Запуск из C:\\oko: python tools\\patch_tv_hues.py [--apply]. Коды: 0 OK/ALREADY/DRY-OK, 2 STOP.
"""
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

OLD_HUES = '''HUES: dict[str, int] = {
    "красный": 0, "красную": 0, "красная": 0,
    "оранжевый": 20, "желтый": 40, "желтую": 40,
    "зеленый": 85, "зеленую": 85, "зеленая": 85,
    "бирюзовый": 128, "голубой": 150,
    "синий": 170, "синюю": 170, "синяя": 170,
    "фиолетовый": 200, "сиреневый": 210,
    "розовый": 230, "белый": 0, "теплый": 25, "холодный": 160,
}
'''
NEW_HUES = '''# 02.11 (TV_HUES_FORMS): все формы (род/падеж). «podsvetka zeltaja» в игре уходила в мозг: не было «желтая»,
# а словарь транслита tv_nav.vocabulary() строится из КЛЮЧЕЙ этой таблицы -- поэтому формы ключами, не регуляркой.
# Модель смысла (tv_intent) в игровом режиме отключена, цвет обязан узнаваться правилами.
def _hue_forms(hard: str, soft: bool = False) -> tuple[str, ...]:
    """«желт» -> желтый/желтая/желтую/желтое; soft («син») -> синий/синяя/синюю/синее; «голуб» -- -ой."""
    if soft:
        return (hard + "ий", hard + "яя", hard + "юю", hard + "ее")
    first = hard + ("ой" if hard == "голуб" else "ый")
    return (first, hard + "ая", hard + "ую", hard + "ое")


_HUE_STEMS: tuple[tuple[str, bool, int], ...] = (
    ("красн", False, 0), ("оранжев", False, 20), ("желт", False, 40), ("зелен", False, 85),
    ("бирюзов", False, 128), ("голуб", False, 150), ("син", True, 170), ("фиолетов", False, 200),
    ("сиренев", False, 210), ("розов", False, 230), ("бел", False, 0), ("тепл", False, 25),
    ("холодн", False, 160),
)
HUES: dict[str, int] = {w: hue for stem, soft, hue in _HUE_STEMS for w in _hue_forms(stem, soft)}
'''

OLD_WHITE = '''                                tv.set_lounge_color(hue, 0 if word == "белый" else 255,
'''
NEW_WHITE = '''                                tv.set_lounge_color(hue, 0 if word.startswith("бел") else 255,
'''

TARGETS = [
    ("src\\tv_commands.py", "067B437E6002B355", "121DCD009D809FCA", [(OLD_HUES, NEW_HUES), (OLD_WHITE, NEW_WHITE)]),
]


def h16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16].upper()


def build(raw: bytes, edits):
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    for old, new in edits:
        o, n = old.replace("\n", nl), new.replace("\n", nl)
        c = text.count(o)
        if c != 1:
            return None, f"anchor count={c}: {old.strip().splitlines()[0][:60]}"
        text = text.replace(o, n)
    out = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + out if bom else out), None


def main() -> int:
    apply = "--apply" in sys.argv
    plan = []
    for rel, before, after, edits in TARGETS:
        p = ROOT.joinpath(*rel.split("\\"))
        if not p.is_file():
            print(f"STOP missing {rel}")
            return 2
        raw = p.read_bytes()
        cur = h16(raw)
        if cur == after:
            print(f"ALREADY {rel} {cur}")
            continue
        if cur != before:
            print(f"STOP hash {rel} have={cur} want={before}")
            return 2
        new, err = build(raw, edits)
        if err:
            print(f"STOP {rel} {err}")
            return 2
        if h16(new) != after:
            print(f"STOP result {rel} got={h16(new)} want={after}")
            return 2
        plan.append((p, rel, new))
        print(f"{'APPLY' if apply else 'DRY-OK'} {rel} {before} -> {after}")
    if not apply or not plan:
        return 0
    for p, rel, new in plan:
        shutil.copy2(p, p.with_name(p.name + ".bak_tv_hues"))
    for p, rel, new in plan:
        p.write_bytes(new)
        print(f"WROTE {rel} {h16(p.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
