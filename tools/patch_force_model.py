"""FORCE_SMALL_MODEL (02.11.2026, решение Оператора R7=F1): /force_brain в игре -- малая модель, короткий промпт.

Живой случай 15:36:52 (Riftbreaker ~2.8 ГБ + qwen2.5:14b ~6.1 ГБ > 8 ГБ VRAM): частичный офлоад -> [ERR: LLM_TIMEOUT],
пользователь видел сырой код, а таймаут засчитался в TransportGate как сбой исправного узла.
Замер 02.11 16:00 (вне игры): qwen2.5:3b-instruct -- 2059 МБ VRAM, холодный ответ 2.8 с, тёплый 0.4 с.

config.py:   OKO_GAME_FORCE_MODEL (по умолчанию qwen2.5:3b-instruct; пусто -> основная модель).
governor.py: в force в payload идёт OKO_GAME_FORCE_MODEL; сбой вызова в force НЕ пишется в TransportGate.
bot.py:      в force -- без истории и RAG (факты дома остаются); таймаут/недоступность в force -> человеческий
             текст вместо кода; обмен в force в историю не пишется (ответ малой модели без контекста).

Якоря + хэши «до/после», all-or-nothing, идемпотентно, бэкап *.bak_force_model.
Запуск из C:\\oko: python tools\\patch_force_model.py [--apply]. Коды: 0 OK/ALREADY/DRY-OK, 2 STOP.
"""
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

CFG_OLD = '''OKO_LLM_KEEP_ALIVE: str = os.getenv("OKO_LLM_KEEP_ALIVE", "60m").strip()
'''
CFG_NEW = '''OKO_LLM_KEEP_ALIVE: str = os.getenv("OKO_LLM_KEEP_ALIVE", "60m").strip()
# R7=F1 (02.11): модель для /force_brain в игре. 14b рядом с игрой не помещается в 8 ГБ VRAM -> LLM_TIMEOUT
# (живой случай 15:36). Замер 02.11: qwen2.5:3b-instruct 2059 МБ VRAM, ответ 2.8 с холодный / 0.4 с тёплый.
# Пусто -> основная модель (OLLAMA_MODEL).
OKO_GAME_FORCE_MODEL: str = os.getenv("OKO_GAME_FORCE_MODEL", "qwen2.5:3b-instruct").strip()
'''

GOV_MODEL_OLD = '''        endpoint = f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/api/generate"
        payload = {
            "model": config.OLLAMA_MODEL,
'''
GOV_MODEL_NEW = '''        endpoint = f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/api/generate"
        force_model = getattr(config, "OKO_GAME_FORCE_MODEL", "") if gm == game_mode.FORCE else ""
        payload = {
            "model": force_model or config.OLLAMA_MODEL,
'''

GOV_GATE_OLD = '''        gate = self._transport_gate
        if not await gate.allow(self._probe_ollama):
'''
GOV_GATE_NEW = '''        gate = self._transport_gate
        # R7 (02.11): в force отказ -- следствие занятой игрой VRAM, а не сбой узла: TransportGate не трогается,
        # иначе три таймаута в игре открыли бы предохранитель и после игры.
        record_failure = gate.record_failure if gm != game_mode.FORCE else (lambda *_a, **_k: None)
        if not await gate.allow(self._probe_ollama):
'''

GOV_FAILS = [
    ('                    gate.record_failure(f"HTTP {status}")\n',
     '                    record_failure(f"HTTP {status}")\n'),
    ('                gate.record_failure("timeout")\n',
     '                record_failure("timeout")\n'),
    ('                gate.record_failure(type(e).__name__)\n',
     '                record_failure(type(e).__name__)\n'),
    ('                gate.record_failure("timed out in flight")\n',
     '                record_failure("timed out in flight")\n'),
]

BOT_HIST_OLD = '''    history_list = await asyncio.to_thread(_fetch_history, user_id, HISTORY_LIMIT)
    knowledge = await retrieve_context(sanitized_input)
'''
BOT_HIST_NEW = '''    # R7=F1 (02.11): /force_brain в игре -- малая модель и короткий промпт: без истории и RAG (факты дома остаются).
    forced = game_mode.brain_mode(config) == game_mode.FORCE
    if forced:
        history_list, knowledge = [], ""
    else:
        history_list = await asyncio.to_thread(_fetch_history, user_id, HISTORY_LIMIT)
        knowledge = await retrieve_context(sanitized_input)
'''

BOT_FAIL_OLD = '''    persisted = None  # P1_NOTE: False -> пометка Оператору; None -> без пометки
    if _is_error_marker(raw_response):
'''
BOT_FAIL_NEW = '''    if forced and isinstance(raw_response, str) and raw_response.strip() in FORCE_FAIL_MARKERS:
        logger.warning("[HISTORY_SKIPPED] [GAME_MODE] force=fail %s user=%s — exchange not persisted",
                       raw_response.strip(), user_id)
        return FORCE_FAIL_TEXT

    persisted = None  # P1_NOTE: False -> пометка Оператору; None -> без пометки
    if _is_error_marker(raw_response):
'''

BOT_SAVE_OLD = '''        persisted = await asyncio.to_thread(_save_exchange, user_id, sanitized_input, raw_response)
        await asyncio.to_thread(_log_depth, user_id)
    return _with_p1_note(_with_notice(raw_response, getattr(result, "incidents", None)), persisted)
'''
BOT_SAVE_NEW = '''        if forced:   # R7: ответ малой модели без истории -- в историю не пишется
            logger.info("[HISTORY_SKIPPED] [GAME_MODE] force user=%s — exchange not persisted", user_id)
        else:
            persisted = await asyncio.to_thread(_save_exchange, user_id, sanitized_input, raw_response)
            await asyncio.to_thread(_log_depth, user_id)
    return _with_p1_note(_with_notice(raw_response, getattr(result, "incidents", None)), persisted)
'''

BOT_CONST_OLD = '''async def process_logic(
'''
BOT_CONST_NEW = '''# R7 (02.11): сбой /force_brain в игре -- человеческий текст вместо служебного кода.
FORCE_FAIL_MARKERS = ("[ERR: LLM_TIMEOUT]", "[ERR: LLM_UNAVAILABLE]")
FORCE_FAIL_TEXT = ("⏱ Модель не успела ответить: видеокарта занята игрой. "
                   "Попробуй ещё раз или после игры.")


async def process_logic(
'''

TARGETS = [
    ("src\\config.py", "BD34B392D2FDC994", "43AA971BC6B4803A", [(CFG_OLD, CFG_NEW)]),
    ("src\\governor.py", "EE1D7390F6C0B519", "9290EE889AB18FDB",
     [(GOV_MODEL_OLD, GOV_MODEL_NEW), (GOV_GATE_OLD, GOV_GATE_NEW)] + GOV_FAILS),
    ("src\\bot.py", "45FF0EB51B50AEAA", "8342A26DDE78272E",
     [(BOT_CONST_OLD, BOT_CONST_NEW), (BOT_HIST_OLD, BOT_HIST_NEW), (BOT_FAIL_OLD, BOT_FAIL_NEW),
      (BOT_SAVE_OLD, BOT_SAVE_NEW)]),
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
        shutil.copy2(p, p.with_name(p.name + ".bak_force_model"))
    for p, rel, new in plan:
        p.write_bytes(new)
        print(f"WROTE {rel} {h16(p.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
