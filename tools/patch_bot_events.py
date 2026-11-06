"""EVENTS (этап C, 01.11.2026): патч src/bot.py -- события модулей -> журнал событий -> факты для мозга.

Добавляет:
  - import event_log;
  - _event(), _home_facts(), _devices_answer();
  - process_logic: блок фактов (состояние техники + события за сутки) в контекст запроса;
  - _route_reply: маршрут devices; исполненные команды ТВ/Garmin -> событие;
  - device_watch_job / net_context_job / garmin_job: каждое отправленное сообщение -> событие;
  - post_init: event_log.init() (хвост файла, в потоке).
Без init (тесты) журнал молчит, блок фактов пуст -> контекст мозга прежний.

Предусловие: src/bot.py после этапа B -- байтовый SHA256[:16] 4D65471EE000D836 (CONFIRMED 01.11 16:16)
или то же содержимое без BOM/CRLF 6B26451656D76414.
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
EXPECTED_RAW = "4D65471EE000D836"
EXPECTED_CONTENT = "6B26451656D76414"
MARKER = "EVENTS (этап C"

R = []

R.append(("C1 import",
          "import router            # ROUTER (этап B, 01.11): общий маршрутизатор текста и голоса\n",
          "import router            # ROUTER (этап B, 01.11): общий маршрутизатор текста и голоса\n"
          "import event_log         # EVENTS (этап C, 01.11): события модулей -> факты для мозга\n"))

R.append(("C2 helpers",
          "# ROUTER (этап B, 01.11): команда устройству / вопрос о себе -> модуль, мимо модели.\n",
          r'''# EVENTS (этап C, 01.11): события модулей -> журнал событий -> факты в контексте мозга.
def _event(src: str, text: str) -> None:
    event_log.record(src, text)          # не бросает; до init() -- no-op


def _home_facts() -> str:
    """Состояние техники (память детектора, без сети) + события за сутки. Пусто -- контекст прежний."""
    try:
        return event_log.facts_block(device_watch.state_text())
    except Exception as e:
        logger.warning("[EVENTS] facts failed: %s: %s", type(e).__name__, e)
        return ""


def _devices_answer() -> str:
    state = device_watch.state_text()
    last = event_log.recent(n=1, src="devices", within_s=7 * 86400)
    if not state:
        state = ("Данных о технике пока нет: облако SmartThings ещё не опрошено после запуска "
                 "или наблюдение выключено.")
    tail = f"\nПоследнее событие: {event_log.line(last[0])}" if last else ""
    return "🧺 " + state + tail


''' + "# ROUTER (этап B, 01.11): команда устройству / вопрос о себе -> модуль, мимо модели.\n"))

R.append(("C3 process_logic facts",
          r'''    full_context_payload = (
        f"История диалога:\n{formatted_history}\n\n"
        f"Дополнительные справочные данные:\n{knowledge}"
    )
''',
          r'''    full_context_payload = (
        f"История диалога:\n{formatted_history}\n\n"
        f"Дополнительные справочные данные:\n{knowledge}"
    )
    facts = _home_facts()               # EVENTS (этап C): факты дома -- в канале справочных данных
    if facts:
        full_context_payload += f"\n\n{facts}"
'''))

R.append(("C4 route devices",
          "        if r.kind == router.TV:\n",
          "        if r.kind == router.DEVICES:      # EVENTS (этап C)\n"
          "            return _devices_answer()\n"
          "        if r.kind == router.TV:\n"))

R.append(("C5 route event",
          '        logger.info("[ROUTE] user=%s fn=%s outcome=%s", user_id, fn, outcome)\n',
          '        logger.info("[ROUTE] user=%s fn=%s outcome=%s", user_id, fn, outcome)\n'
          '        if outcome:                       # EVENTS (этап C): исполненное -- факт для мозга\n'
          '            _event(fn, answer)\n'))

R.append(("C6 device job",
          '        logger.info("[DEVICES] event: %s", text)\n',
          '        logger.info("[DEVICES] event: %s", text)\n'
          '        _event("devices", text)             # EVENTS (этап C)\n'))

R.append(("C7 net job",
          '        logger.info("[NET] summary: %s", text.splitlines()[0])\n',
          '        logger.info("[NET] summary: %s", text.splitlines()[0])\n'
          '        _event("net", text)                 # EVENTS (этап C)\n'))

R.append(("C8 garmin job",
          '    logger.info("[GARMIN] сводка %s отправлена, символов %d", kind, len(text))\n',
          '    logger.info("[GARMIN] сводка %s отправлена, символов %d", kind, len(text))\n'
          '    _event("garmin", text)                  # EVENTS (этап C)\n'))

R.append(("C9 post_init",
          "    await asyncio.to_thread(_caps_apply, app)  # CAPS_CATALOG\n",
          "    await asyncio.to_thread(_caps_apply, app)  # CAPS_CATALOG\n"
          "    await asyncio.to_thread(event_log.init)    # EVENTS (этап C): хвост журнала событий\n"))

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
    for name, old, new in R:
        n = norm.count(old)
        if n != 1:
            print(f"ANCHOR_FAIL {name}: count={n}")
            return 3
        norm = norm.replace(old, new, 1)
    out = norm.replace("\n", "\r\n") if crlf else norm
    out_bytes = (BOM if has_bom else b"") + out.encode("utf-8")
    backup = TARGET.with_name(TARGET.name + ".before_events")
    if backup.exists():
        i = 2
        while TARGET.with_name(f"{TARGET.name}.before_events{i}").exists():
            i += 1
        backup = TARGET.with_name(f"{TARGET.name}.before_events{i}")
    shutil.copy2(TARGET, backup)
    tmp = TARGET.with_name(TARGET.name + ".events_tmp")
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
