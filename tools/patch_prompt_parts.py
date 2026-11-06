"""PROMPT_PARTS + CTX_LOAD_MS (02.11.2026).

bot.py: строка [PROMPT_PARTS] на КАЖДЫЙ запрос мозгу (sys/hist/layers/facts/q/budget/kn).
  [KNOWLEDGE_CUT] пишется только при обрезке -> по нему петлю
  «история съедает справку» не измерить (выборка budget смещена вниз).
governor.py: поле load_ms в конце строки [CTX] (load_duration ответа Ollama, нс -> мс).

Якоря + хэши «до/после» всего файла, all-or-nothing, идемпотентно (ALREADY),
BOM и переводы строк сохраняются, бэкап *.bak_prompt_parts.
Запуск из C:\\oko:  python tools\\patch_prompt_parts.py [--apply]   (без --apply = dry-run)
Коды: 0 OK/ALREADY/DRY-OK, 2 хэш/якорь не совпал (ничего не записано).
"""
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

BOT_OLD = """    budget = _knowledge_budget(_system_chars(governor), formatted_history, facts, sanitized_input)
    fitted = _fit_knowledge(knowledge, budget)
"""
BOT_NEW = """    sys_chars = _system_chars(governor)
    budget = _knowledge_budget(sys_chars, formatted_history, facts, sanitized_input)
    fitted = _fit_knowledge(knowledge, budget)
    # PROMPT_PARTS (02.11): размеры частей промпта на КАЖДЫЙ запрос. [KNOWLEDGE_CUT]
    # пишется только при обрезке -- по нему одному петлю «история съедает справку»
    # не измерить. Без user_id и текста; kn -- длина справки до->после бюджета.
    logger.info("[PROMPT_PARTS] sys=%d hist=%d layers=%d facts=%d q=%d budget=%d kn=%d->%d",
                sys_chars, len(formatted_history), len(history_list), len(facts or ""),
                len(sanitized_input), budget, len(knowledge), len(fitted))
"""

GOV_OLD_1 = """ctx_fields.default_nctx = 4096  # дефолт; _route_to_llm подставит config.OKO_RUNTIME_NUM_CTX
"""
GOV_NEW_1 = """ctx_fields.default_nctx = 4096  # дефолт; _route_to_llm подставит config.OKO_RUNTIME_NUM_CTX


def ctx_load_ms(data: dict) -> Optional[int]:
    \"\"\"CTX_LOAD_MS (02.11): load_duration (нс) из ответа /api/generate -> мс.
    Нет поля / не число / отрицательное -> None ('-' в логе). Заметное значение
    на тёплой модели = перезагрузка (вытеснение из VRAM, другие options).\"\"\"
    v = data.get("load_duration") if isinstance(data, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return None
    return int(round(v / 1_000_000))
"""

GOV_OLD_2 = """                    "[CTX] call=%s pe=%s ev=%s nctx=%s fill=%s chars=%d",
                    ctx_call_type.get(),
                    f["pe"] if f["pe"] is not None else "-",
                    f["ev"] if f["ev"] is not None else "-",
                    f["nctx"],
                    f'{f["fill"]:.3f}' if f["fill"] is not None else "-",
                    f["chars"],
                )
"""
GOV_NEW_2 = """                    "[CTX] call=%s pe=%s ev=%s nctx=%s fill=%s chars=%d load_ms=%s",
                    ctx_call_type.get(),
                    f["pe"] if f["pe"] is not None else "-",
                    f["ev"] if f["ev"] is not None else "-",
                    f["nctx"],
                    f'{f["fill"]:.3f}' if f["fill"] is not None else "-",
                    f["chars"],
                    lm if (lm := ctx_load_ms(data)) is not None else "-",
                )
"""

# rel path, hash16 до, hash16 после, [(old, new)]
TARGETS = [
    ("src\\bot.py", "9610CEAEE721CCB7", "6E9E6585C147AE2B", [(BOT_OLD, BOT_NEW)]),
    ("src\\governor.py", "E6B2F2777D9E7F2F", "D63A18994B4650C8",
     [(GOV_OLD_1, GOV_NEW_1), (GOV_OLD_2, GOV_NEW_2)]),
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
            return None, f"anchor count={c}: {old.splitlines()[0].strip()[:60]}"
        text = text.replace(o, n)
    out = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + out if bom else out), None


def main() -> int:
    apply = "--apply" in sys.argv
    plan, already = [], 0
    for rel, before, after, edits in TARGETS:
        p = ROOT.joinpath(*rel.split("\\"))
        if not p.is_file():
            print(f"STOP missing {rel}")
            return 2
        raw = p.read_bytes()
        cur = h16(raw)
        if cur == after:
            print(f"ALREADY {rel} {cur}")
            already += 1
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
        shutil.copy2(p, p.with_name(p.name + ".bak_prompt_parts"))
    for p, rel, new in plan:
        p.write_bytes(new)
        print(f"WROTE {rel} {h16(p.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
