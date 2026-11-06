"""RAG_EXCLUDE (R2) + TV_HINT (R1), 02.11.2026, решение Оператора R3.

Живой случай 22:41: «как сделать подсветку под музыку?» -> мозг выдумал «/tv ambilight/mode expert»,
«через API». Причина (CONFIRMED 22:48): справка RAG -- tv_philips_api_v6.txt чанк 7 (таблица JSON API
Ambilight) вошла в промпт после сжатия system prompt (budget 0 -> 2210).

R2 config.py:   OKO_RAG_BRAIN_EXCLUDE (маски fnmatch через запятую, по умолчанию tv_philips_api_*).
   retriever.py: build_context(..., exclude=()) -- чанки из источников по маске не идут в справку.
   bot.py:       обычный чат передаёт exclude; /discuss читает документ напрямую и не затронут.
R1 router.py:   «как <сделать/включить/настроить…> …» + предмет ТВ (подсветка/свет/яркость или громкость/звук),
                без другого устройства -> Route(HINT) с готовыми фразами, без модели.
   bot.py:       Route HINT -> ответ подсказкой, в историю не пишется.

Манифест: если цель в security_tools\\manifest.sha256 -- STOP (нужен update_manifest).
Якоря + хэши «до/после», all-or-nothing, идемпотентно, бэкап *.bak_rag_hint.
Запуск из C:\\oko: python tools\\patch_rag_hint.py [--apply]. Коды: 0 OK/ALREADY/DRY-OK, 2 STOP.
"""
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

CFG = [('''OKO_GAME_FORCE_MODEL: str = os.getenv("OKO_GAME_FORCE_MODEL", "qwen2.5:3b-instruct").strip()
''', '''OKO_GAME_FORCE_MODEL: str = os.getenv("OKO_GAME_FORCE_MODEL", "qwen2.5:3b-instruct").strip()

# R2 (02.11): источники RAG, которые НЕ идут в справку обычного чата -- документация для разработчика
# (мозг пересказывал JSON API ТВ как команды бота, живой случай 22:41). /discuss видит документ целиком.
# Маски fnmatch по имени файла через запятую; пусто -> исключений нет.
OKO_RAG_BRAIN_EXCLUDE: tuple = tuple(
    p.strip() for p in os.getenv("OKO_RAG_BRAIN_EXCLUDE", "tv_philips_api_*").split(",") if p.strip())
''')]

RET = [
    ('''import logging
import os
import sys
''', '''import fnmatch
import logging
import os
import sys
'''),
    ('''def build_context(query: str, top_k: int = TOP_K, max_chars: int = 3000) -> tuple[str, list[dict]]:
''', '''def _excluded(source, patterns) -> bool:
    """R2 (02.11): источник по маске (имя файла, fnmatch) не идёт в справку чата."""
    name = os.path.basename(str(source or ""))
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def build_context(query: str, top_k: int = TOP_K, max_chars: int = 3000,
                  exclude: tuple = ()) -> tuple[str, list[dict]]:
'''),
    ('''    if max_d > 0:
        hits = [h for h in hits if h["distance"] <= max_d]
''', '''    if max_d > 0:
        hits = [h for h in hits if h["distance"] <= max_d]
    if exclude:
        kept = [h for h in hits if not _excluded(h.get("source"), exclude)]
        if len(kept) != len(hits):
            logger.info("[RAG_EXCLUDE] dropped=%d kept=%d", len(hits) - len(kept), len(kept))
        hits = kept
'''),
]

ROUTER = [
    ('''SELF, GARMIN, DEVICES, TV, BRAIN = "self", "garmin", "devices", "tv", "brain"
''', '''SELF, GARMIN, DEVICES, TV, BRAIN = "self", "garmin", "devices", "tv", "brain"
HINT = "hint"   # R1 (02.11): «как …» про ТВ -> готовые фразы, без модели
'''),
    ('''def default_intent(t: str) -> Optional[str]:
''', '''# R1 (02.11): вопрос «как сделать/включить/настроить …» про ТВ -> подсказка фраз маршрутизатора, без модели.
# Живой случай 22:41: мозг на такой вопрос пересказал JSON API ТВ. Фразы в «» обязаны уходить в ТВ (тест).
HOW_RE = re.compile(_B + r"(как|каким образом)" + _E)
HINT_LIGHT_RE = re.compile(_B + r"свет\\w*" + _E)   # «света», «светом» -- только для подсказки
HOW_VERB_RE = re.compile(
    _B + r"(сделать|сделай|включить|выключить|поставить|настроить|изменить|поменять|сменить|менять|"
    r"убавить|прибавить|убрать|регулировать|управлять|переключить)" + _E)
TV_HINTS = {
    "ambilight": ("💡 Подсветка ТВ — напиши фразу (без /tv):\\n"
                  "• «подсветка музыка» — под звук\\n"
                  "• «подсветка красная» — один цвет\\n"
                  "• «подсветка кино» — за картинкой\\n"
                  "• «яркость минус 3» — ярче или тусклее\\n"
                  "• «подсветка выключи»"),
    "volume": ("🔊 Звук ТВ — напиши фразу (без /tv):\\n"
               "• «громкость 20»\\n"
               "• «громкость плюс 5» или «громкость минус 5»\\n"
               "• «без звука» или «включи звук»"),
}


def tv_hint_key(t: str) -> Optional[str]:
    """t -- нормализованный текст. «ambilight» / «volume» или None."""
    if not (t and HOW_RE.search(t) and HOW_VERB_RE.search(t)) or OTHER_DEVICE_RE.search(t):
        return None
    if AMB_RE.search(t) or LIGHT_RE.search(t) or HINT_LIGHT_RE.search(t):
        return "ambilight"
    if VOL_RE.search(t) or SOUND_RE.search(t):
        return "volume"
    return None


def default_intent(t: str) -> Optional[str]:
'''),
    ('''        if APPLIANCE_RE.search(t) and APPLIANCE_STATE_RE.search(t):
            return Route(DEVICES, reason="devices")
''', '''        if APPLIANCE_RE.search(t) and APPLIANCE_STATE_RE.search(t):
            return Route(DEVICES, reason="devices")
        key = tv_hint_key(t)              # R1 (02.11)
        if key:
            return Route(HINT, TV_HINTS[key], reason=f"hint:{key}")
'''),
]

BOT = [
    ('''    context, _hits = _rag_build_context(query)
''', '''    context, _hits = _rag_build_context(query, exclude=getattr(config, "OKO_RAG_BRAIN_EXCLUDE", ()))  # R2 (02.11)
'''),
    ('''        if r.kind == router.DEVICES:      # EVENTS (этап C)
''', '''        if r.kind == router.HINT:         # R1 (02.11): подсказка фраз ТВ, без модели и без истории
            return r.text
        if r.kind == router.DEVICES:      # EVENTS (этап C)
'''),
]

TARGETS = [
    ("src\\config.py", "43AA971BC6B4803A", "9A6DAD5144E9AD4C", CFG),
    ("oko_rag\\retriever.py", "72F0307D46FA148D", "C9E083431463DD31", RET),
    ("src\\router.py", "0E0FDC7F31411099", "F9DCDDEC26C31B0A", ROUTER),
    ("src\\bot.py", "8342A26DDE78272E", "1F01C558B9C4A5F5", BOT),
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


def manifest_hits():
    m = ROOT / "security_tools" / "manifest.sha256"
    if not m.is_file():
        return []
    paths = set()
    for line in m.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            paths.add(parts[1].strip().lstrip("*").replace("\\\\", "\\").replace("/", "\\").lower())
    return [rel for rel, *_ in TARGETS if rel.lower() in paths]


def main() -> int:
    apply = "--apply" in sys.argv
    hits = manifest_hits()
    if hits:
        print(f"STOP in manifest: {hits} (update_manifest required)")
        return 2
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
        shutil.copy2(p, p.with_name(p.name + ".bak_rag_hint"))
    for p, rel, new in plan:
        p.write_bytes(new)
        print(f"WROTE {rel} {h16(p.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
