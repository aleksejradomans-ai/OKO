"""ROUTER_QMARK (02.11.2026): признак вопроса «?» -- из исходного текста, один раз для всех вариантов.

Живой случай 23:00:40: «diag_rag_ambi.txtкак сделать подсветку под музыку?» -> kind=tv tv_rule:amb_audio --
подсветка переключена по ВОПРОСУ. Защита «вопрос -- мозгу» проверяла каждый вариант отдельно, а вариант-транслит
(строится при латинице в тексте, tv_nav.tokens) знак «?» стирает -> команда исполнилась. Любой вопрос с латинским
словом и без слов QUESTION_RE рисковал так же («подсветка music?»).
Правка: перед циклом ТВ -- если «?» есть в text или raw -> BRAIN. Подсказка (HINT), «что умеешь», Garmin, техника
стоят в первом цикле и не затронуты.

router.py F9DCDDEC26C31B0A. Якорь + хэш «до/после», идемпотентно, бэкап *.bak_router_qmark.
Запуск из C:\\oko: python tools\\patch_router_qmark.py [--apply]. Коды: 0 OK/ALREADY/DRY-OK, 2 STOP.
"""
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
REL = "src\\router.py"
BEFORE = "F9DCDDEC26C31B0A"
AFTER = "27D0B12DB902E207"

OLD = '''    for v in variants:
        if "?" in v:
            continue         # вопрос -- мозгу (нормализация знак вопроса стирает)
        r = _tv_route(tv_commands.normalize(v), intent)
'''
NEW = '''    # ROUTER_QMARK (02.11): признак вопроса -- из ИСХОДНОГО текста, один раз для всех вариантов. Вариант-транслит
    # знак «?» стирает (живой случай 23:00:40: вопрос со слипшимся «…txtкак» переключил подсветку).
    if any("?" in s for s in (text or "", raw or "")):
        return Route(BRAIN, reason="brain")
    for v in variants:
        r = _tv_route(tv_commands.normalize(v), intent)
'''


def h16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16].upper()


def main() -> int:
    apply = "--apply" in sys.argv
    p = ROOT.joinpath(*REL.split("\\"))
    if not p.is_file():
        print(f"STOP missing {REL}")
        return 2
    raw = p.read_bytes()
    cur = h16(raw)
    if cur == AFTER:
        print(f"ALREADY {REL} {cur}")
        return 0
    if cur != BEFORE:
        print(f"STOP hash {REL} have={cur} want={BEFORE}")
        return 2
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = (raw[3:] if bom else raw).decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    o, n = OLD.replace("\n", nl), NEW.replace("\n", nl)
    if text.count(o) != 1:
        print(f"STOP anchor count={text.count(o)}")
        return 2
    out = text.replace(o, n).encode("utf-8")
    new = b"\xef\xbb\xbf" + out if bom else out
    if h16(new) != AFTER:
        print(f"STOP result {REL} got={h16(new)} want={AFTER}")
        return 2
    print(f"{'APPLY' if apply else 'DRY-OK'} {REL} {BEFORE} -> {AFTER}")
    if apply:
        shutil.copy2(p, p.with_name(p.name + ".bak_router_qmark"))
        p.write_bytes(new)
        print(f"WROTE {REL} {h16(p.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
