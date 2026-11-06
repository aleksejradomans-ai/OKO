"""ROUTER (этап B, 01.11.2026): патч src/bot.py -- общий маршрутизатор текста и голоса.

Добавляет:
  - import router;
  - _proof_record(): запись исхода модуля в реестр подтверждений (только при смене статуса
    или дня) + обновление каталога в governor;
  - _route_reply(): router.route (в потоке: модель смысла блокирующая) -> self / tv / garmin
    исполняются здесь, brain -> None. Ответ «что умеешь» пишется в историю: правдивый ответ
    вытесняет выдуманный (находка 30.10 16:10);
  - _brain_answer(): хвост handle_message для голоса (process_logic / discuss);
  - handle_message: маршрутизатор перед мозгом (не в /discuss);
  - handle_voice: заглушка «Голосом пока управляется телевизор» -> маршрутизатор, иначе мозг.
Сбой маршрутизатора -> WARNING [ROUTE], сообщение уходит в мозг (как до этапа B).

Предусловие: src/bot.py после этапа A -- байтовый SHA256[:16] 7C59FAAA65054534 (CONFIRMED
30.10 16:07) или то же содержимое без BOM/CRLF 3798B557307D5D8B.
Коды выхода: 0 -- OK/ALREADY; 2 -- предусловие; 3 -- якорь; 4 -- py_compile (откат).
"""
from __future__ import annotations

import hashlib
import os
import py_compile
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "bot.py"
EXPECTED_RAW = "7C59FAAA65054534"
EXPECTED_CONTENT = "3798B557307D5D8B"
MARKER = "ROUTER (этап B"

B1_OLD = "import proof_registry\n"
B1_NEW = B1_OLD + "import router            # ROUTER (этап B, 01.11): общий маршрутизатор текста и голоса\n"

B2_OLD = "async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):\n"
B2_NEW = r'''# ROUTER (этап B, 01.11): команда устройству / вопрос о себе -> модуль, мимо модели.
def _proof_record(app: Application, reg, fn: str, status: str) -> None:
    """Исход модуля -> реестр подтверждений (только смена статуса/дня) -> каталог в governor."""
    try:
        if router.should_record(reg.latest().get(fn), status, time.strftime("%Y-%m-%d")):
            reg.record(fn, status, src="router")
            app.bot_data["governor"].set_capabilities(capabilities.build_catalog(reg.latest()))
    except Exception as e:
        logger.warning("[PROOF] record failed fn=%s: %s: %s", fn, type(e).__name__, e)


async def _route_reply(app: Application, user_id: int, text: str, raw: str) -> Optional[str]:
    """Ответ модуля или None (-> мозг). Текст пользователя в журнал не пишется."""
    try:
        r = await asyncio.to_thread(router.route, text, raw)
        logger.info("[ROUTE] user=%s kind=%s reason=%s", user_id, r.kind, r.reason)
        if r.kind == router.BRAIN:
            return None
        reg = app.bot_data.get("proof_registry")
        if r.kind == router.SELF:
            answer = capabilities.human_text(reg.latest() if reg is not None else None)
            await asyncio.to_thread(_save_exchange, user_id, text, answer)
            return answer
        if r.kind == router.TV:
            answer, outcome = await asyncio.to_thread(
                router.run_tv, user_id, r, tv_bot_handler.handle, tv_bot_handler.make_factory())
            fn = "tv"
        else:
            answer, outcome = await asyncio.to_thread(router.run_garmin, r, garmin_report.run)
            fn = "garmin"
        logger.info("[ROUTE] user=%s fn=%s outcome=%s", user_id, fn, outcome)
        if outcome and reg is not None:
            await asyncio.to_thread(_proof_record, app, reg, fn, outcome)
        return answer
    except Exception as e:
        logger.warning("[ROUTE] failed, fallback to brain: %s: %s", type(e).__name__, e)
        return None


async def _brain_answer(app: Application, user_id: int, text: str, reserve: int = 0) -> str:
    """Хвост handle_message для голоса: мозг (или /discuss) -> текст ответа."""
    governor: AgentGovernor = app.bot_data["governor"]
    dispatcher = app.bot_data["dispatcher"]
    input_monitor: ComplianceMonitor = app.bot_data["input_monitor"]
    if user_id in discussion_sessions:
        raw_response = await process_discuss_logic(text, user_id, governor, dispatcher, input_monitor)
    else:
        raw_response = await process_logic(text, user_id, governor, dispatcher, input_monitor)
    if raw_response is None:
        return "[ERR: SYSTEM_FAULT]"
    clean = core.clean_output(_explain_for_user(governor, raw_response))
    return core.format_final(clean[:max(200, MAX_MSG_LEN - len(core.name) - 4 - reserve)])


''' + B2_OLD

B3_OLD = ('    governor: AgentGovernor = context.application.bot_data["governor"]\n'
          '    dispatcher = context.application.bot_data["dispatcher"]\n'
          '    input_monitor: ComplianceMonitor = context.application.bot_data["input_monitor"]\n'
          '\n'
          '    if user_id in discussion_sessions:\n')
B3_NEW = ('    # ROUTER (этап B): не в /discuss -- там текст относится к документу.\n'
          '    if user_id not in discussion_sessions:\n'
          '        routed = await _route_reply(context.application, user_id, text, update.message.text.strip())\n'
          '        if routed is not None:\n'
          '            await _reply_with_retry(update.message, layout_note + routed, user_id)\n'
          '            return\n'
          '\n') + B3_OLD

B4_OLD = ('    await msg.reply_text(f"🎙 понял: «{text}»\\nГолосом пока управляется телевизор: '
          'отправь /tv, чтобы включить режим.")\n')
B4_NEW = ('    # ROUTER (этап B, 01.11): голос вне /tv -- тот же маршрут, что текст; не узнано -> мозг.\n'
          '    heard = f"🎙 понял: «{text}»\\n"\n'
          '    if user_id not in discussion_sessions:\n'
          '        routed = await _route_reply(context.application, user_id, text, text)\n'
          '        if routed is not None:\n'
          '            await msg.reply_text(heard + routed)\n'
          '            return\n'
          '    answer = await _brain_answer(context.application, user_id, text, reserve=len(heard))\n'
          '    await _reply_with_retry(msg, heard + answer, user_id)\n')

REPLACEMENTS = [
    ("B1 import", B1_OLD, B1_NEW),
    ("B2 helpers", B2_OLD, B2_NEW),
    ("B3 handle_message", B3_OLD, B3_NEW),
    ("B4 handle_voice", B4_OLD, B4_NEW),
]

BOM = b"\xef\xbb\xbf"


def h16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16].upper()


def main() -> int:
    if not TARGET.is_file():
        print(f"TARGET_NOT_FOUND {TARGET}")
        return 2
    raw = TARGET.read_bytes()
    has_bom = raw.startswith(BOM)
    text = (raw[len(BOM):] if has_bom else raw).decode("utf-8")
    crlf = "\r\n" in text
    norm = text.replace("\r\n", "\n")

    if MARKER in norm:
        print(f"ALREADY {MARKER} present | {h16(raw)}")
        return 0

    raw_hash, content_hash = h16(raw), h16(norm.encode("utf-8"))
    if raw_hash != EXPECTED_RAW and content_hash != EXPECTED_CONTENT:
        print(f"SRC_HASH_MISMATCH raw={raw_hash} content={content_hash} "
              f"expected raw={EXPECTED_RAW} or content={EXPECTED_CONTENT}")
        return 2

    for name, old, new in REPLACEMENTS:
        n = norm.count(old)
        if n != 1:
            print(f"ANCHOR_FAIL {name}: count={n}")
            return 3
        norm = norm.replace(old, new, 1)

    out = norm.replace("\n", "\r\n") if crlf else norm
    out_bytes = (BOM if has_bom else b"") + out.encode("utf-8")

    backup = TARGET.with_name(TARGET.name + ".before_router")
    if backup.exists():
        i = 2
        while TARGET.with_name(f"{TARGET.name}.before_router{i}").exists():
            i += 1
        backup = TARGET.with_name(f"{TARGET.name}.before_router{i}")
    shutil.copy2(TARGET, backup)

    tmp = TARGET.with_name(TARGET.name + ".router_tmp")
    tmp.write_bytes(out_bytes)
    os.replace(tmp, TARGET)

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(backup, TARGET)
        print(f"COMPILE_FAIL rolled back from {backup.name}: {e}")
        return 4

    print(f"PATCHED {TARGET.relative_to(ROOT)} | {raw_hash} -> {h16(out_bytes)} | "
          f"bom={has_bom} crlf={crlf} | backup={backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
