"""CAPS_BRIEF (02.11.2026): короткий каталог функций для system prompt; полный -- только для «что умеешь».

Замер 02.11: system prompt = identity v3 1501 + каталог 1323. Каталог в промпте нужен ради ПРАВИЛА
(«бот выполняет до тебя, не пиши "сделал"», без него мозг выдумывает функции -- caps_probe 30.10) и списка
того, что есть. Примеры фраз мозгу почти не нужны: команды исполняет маршрутизатор до мозга, а «что умеешь»
отвечается детерминированно (human_text, полный текст).
Правка: Capability.brief -- короткая строка; build_catalog() берёт brief, human_text() -- прежний text.
HEADER и RULE не меняются. Ожидаемо: каталог ~1323 -> ~880 симв. (с пометками проверки).

capabilities.py (24A8BF6F722BBC9A) + test_capabilities.py (6F6B4D40D0D3565E: строка каталога ищется по brief).
Якоря + хэши «до/после», all-or-nothing, идемпотентно, бэкап *.bak_caps_brief.
Запуск из C:\\oko: python tools\\patch_caps_brief.py [--apply]. Коды: 0 OK/ALREADY/DRY-OK, 2 STOP.
"""
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

BRIEFS = {
    "tv": ('ТВ Philips: «подсветка музыка», цвет, яркость, громкость, «выключи телевизор»; кнопки — /tv. '
           'Включить по сети и открыть приложения нельзя.'),
    "garmin": "Garmin: сон, шаги, /garmin; сводки 08:00 и 21:00.",
    "devices": "Стиральная и сушильная Samsung: уведомления и состояние; управлять нельзя.",
    "net": "Сеть: сводка проверки при смене сети ПК.",
    "docs": "Документы PDF/DOCX/XLSX: печать, в базу знаний, удалить.",
    "discuss": "/discuss, /enddiscuss — обсуждение документа.",
    "memory": "/memory, /forget, /newepoch, /status.",
    "self": "«что умеешь» — полный список без участия модели.",
}

CAP_EDITS = [
    ('''    checkable: bool = False                   # пометка проверки имеет смысл (устройство/внешний сервис)
''', '''    checkable: bool = False                   # пометка проверки имеет смысл (устройство/внешний сервис)
    brief: str = ""                           # CAPS_BRIEF (02.11): строка для system prompt; пусто -> text
'''),
    ('''def _lines(index: Optional[dict], net, now, max_age_s) -> list[str]:
    index = index or {}
    out = []
    for cap in DECLARED:
        rec = index.get(cap.key) if cap.checkable else None
        out.append(cap.text + status_suffix(rec, net=net, now=now, max_age_s=max_age_s))
    return out
''', '''def _lines(index: Optional[dict], net, now, max_age_s, brief: bool = False) -> list[str]:
    index = index or {}
    out = []
    for cap in DECLARED:
        rec = index.get(cap.key) if cap.checkable else None
        body = (cap.brief or cap.text) if brief else cap.text
        out.append(body + status_suffix(rec, net=net, now=now, max_age_s=max_age_s))
    return out
'''),
    ('''    return "\\n".join([HEADER, RULE] + ["- " + l for l in _lines(index, net, now, max_age_s)])
''', '''    # CAPS_BRIEF (02.11): в system prompt -- короткие строки; полный текст -- в human_text («что умеешь»).
    return "\\n".join([HEADER, RULE] + ["- " + l for l in _lines(index, net, now, max_age_s, brief=True)])
'''),
] + [(f'        key="{k}",\n', f'        key="{k}",\n        brief={v!r},\n') for k, v in BRIEFS.items()]

TEST_EDITS = [
    ('''        line = next(l for l in text.splitlines() if l == "- " + c.text or l.startswith("- " + c.text + " ["))
''', '''        body = c.brief or c.text      # CAPS_BRIEF (02.11): в каталоге короткая строка
        line = next(l for l in text.splitlines() if l == "- " + body or l.startswith("- " + body + " ["))
'''),
]

TARGETS = [
    ("src\\capabilities.py", "24A8BF6F722BBC9A", "96528AA901B45AC7", CAP_EDITS),
    ("src\\test_capabilities.py", "6F6B4D40D0D3565E", "22D21BFD4731B7A6", TEST_EDITS),
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
        shutil.copy2(p, p.with_name(p.name + ".bak_caps_brief"))
    for p, rel, new in plan:
        p.write_bytes(new)
        print(f"WROTE {rel} {h16(p.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
