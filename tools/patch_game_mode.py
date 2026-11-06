"""GAME_MODE v2 (02.11.2026): бот не останавливается в игре, мозг спит.

config.py:   OKO_GAME_MODE, OKO_GAME_FLAG, OKO_GAME_FORCE_FLAG, OKO_GAME_FLAG_STALE_S,
             OKO_GAME_FORCE_MIN, OKO_GAME_FORCE_MAX_MIN, OKO_GAME_FORCE_NUM_GPU, OKO_LLM_KEEP_ALIVE.
governor.py: GameModeSleep (подвид LLMTransportError, до TransportGate); keep_alive и options.num_gpu
             (force) в payload /api/generate; VECTOR_S в игре не запускается.
bot.py:      заглушка в process_logic/process_discuss_logic (до истории/RAG); маршрутизатор в игре
             без модели смысла (intent=None); сон пропускается; /force_brain.

Якоря + хэши «до/после» всего файла, all-or-nothing, идемпотентно (ALREADY), BOM и переводы строк
сохраняются, бэкап *.bak_game_mode. Запуск из C:\\oko: python tools\\patch_game_mode.py [--apply]
Коды: 0 OK/ALREADY/DRY-OK, 2 хэш/якорь не совпал (ничего не записано).
"""
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- config.py
CFG_OLD = '''        body["options"] = {"num_gpu": OKO_EMBED_NUM_GPU}
    return body
'''
CFG_NEW = '''        body["options"] = {"num_gpu": OKO_EMBED_NUM_GPU}
    return body


# --- GAME_MODE v2 (02.11): игровой режим без остановки OkoBot (src/game_mode.py) ---
# game_watcher.ps1 пишет data\\game_mode.flag, пока идёт игра (сердцебиение = mtime каждые 12 с);
# бот в игре не зовёт LLM/RAG/эмбеддинги. OKO_GAME_MODE=0 -> флаг игнорируется (откат: вместе со
# старым game_watcher.ps1 из карантина -- иначе новый watcher будет выгружать модель каждый опрос).
OKO_GAME_MODE: bool = os.getenv("OKO_GAME_MODE", "1").strip().lower() not in ("0", "false", "no", "off")
OKO_GAME_FLAG: Path = Path(os.getenv("OKO_GAME_FLAG", "").strip() or (BASE_DIR / "game_mode.flag"))
OKO_GAME_FORCE_FLAG: Path = Path(os.getenv("OKO_GAME_FORCE_FLAG", "").strip() or (BASE_DIR / "brain_force.flag"))
# R1: флаг старше N с (watcher умер) -> fail-open, мозг работает.
_game_stale = _get_env_float("OKO_GAME_FLAG_STALE_S", 120.0)
OKO_GAME_FLAG_STALE_S: float = _game_stale if _game_stale > 0 else 120.0
# /force_brain: минуты по умолчанию и потолок.
OKO_GAME_FORCE_MIN: int = _get_env_pos_int("OKO_GAME_FORCE_MIN", 15)
OKO_GAME_FORCE_MAX_MIN: int = _get_env_pos_int("OKO_GAME_FORCE_MAX_MIN", 120)


# Где считать мозг при /force_brain. Пусто -> как обычно (GPU; FPS просядет, решение Оператора
# в момент команды). 0 -> CPU (VRAM игры не трогается, но 14b с промптом ~3000 т на CPU не
# укладывается в LLM_TIMEOUT=30 с без замера -- HYPOTHESIS). Битое/<0 -> пусто.
def _get_force_num_gpu() -> "int | None":
    raw = os.getenv("OKO_GAME_FORCE_NUM_GPU", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


OKO_GAME_FORCE_NUM_GPU: "int | None" = _get_force_num_gpu()
# R3: keep_alive основной модели в /api/generate (выгрузку в игре гарантирует watcher). Пусто -> не
# передаётся (дефолт рантайма 5 мин).
OKO_LLM_KEEP_ALIVE: str = os.getenv("OKO_LLM_KEEP_ALIVE", "60m").strip()
'''

# ---------------------------------------------------------------- governor.py
GOV_IMPORT_OLD = '''import config
import prompt_safety
'''
GOV_IMPORT_NEW = '''import config
import game_mode          # GAME_MODE v2 (02.11)
import prompt_safety
'''

GOV_EXC_OLD = '''class LLMContentError(RuntimeError):
'''
GOV_EXC_NEW = '''class GameModeSleep(LLMTransportError):
    """GAME_MODE v2 (02.11): идёт игра, мозг спит. Последний рубеж в _route_to_llm
    (основной путь отсекается в bot.process_logic). Подвид транспортного: жёлтый
    LLM_UNAVAILABLE, без рефлексии (F1); TransportGate не трогается (проверка до него)."""


class LLMContentError(RuntimeError):
'''

GOV_ROUTE_OLD = '''            raise RuntimeError("Governor not started — call start() before serving requests")

        endpoint = f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/api/generate"
        payload = {
            "model": config.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if options:
            payload["options"] = options
'''
GOV_ROUTE_NEW = '''            raise RuntimeError("Governor not started — call start() before serving requests")

        # GAME_MODE v2 (02.11): до TransportGate -- отказ из-за игры не считается сбоем узла.
        gm = game_mode.brain_mode(config)
        if gm == game_mode.SLEEP:
            raise GameModeSleep("game mode active: brain sleeps")

        endpoint = f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/api/generate"
        payload = {
            "model": config.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if options:
            payload["options"] = options
        force_gpu = getattr(config, "OKO_GAME_FORCE_NUM_GPU", None)
        if gm == game_mode.FORCE and force_gpu is not None:
            payload["options"] = {**(options or {}), "num_gpu": force_gpu}
        keep_alive = getattr(config, "OKO_LLM_KEEP_ALIVE", "")
        if keep_alive:
            payload["keep_alive"] = keep_alive
'''

GOV_VEC_OLD = '''        if not getattr(self, "_vector_s_on", False) or self._http is None or not text:
            return
        if self._transport_gate.state != "CLOSED":
            return
'''
GOV_VEC_NEW = '''        if not getattr(self, "_vector_s_on", False) or self._http is None or not text:
            return
        if self._transport_gate.state != "CLOSED":
            return
        if game_mode.in_game(config):   # GAME_MODE v2: эмбеддинг (CPU) -- не во время игры
            return
'''

# ---------------------------------------------------------------- bot.py
BOT_IMPORT_OLD = '''import event_log         # EVENTS (этап C, 01.11): события модулей -> факты для мозга
'''
BOT_IMPORT_NEW = '''import event_log         # EVENTS (этап C, 01.11): события модулей -> факты для мозга
import game_mode         # GAME_MODE v2 (02.11): в игре бот живёт, мозг спит
'''

GAME_GUARD = '''    if game_mode.brain_asleep(config):   # GAME_MODE v2 (02.11): до истории/RAG/LLM, обмен не пишется
        logger.info("[GAME_MODE] brain=sleep user=%s", user_id)
        return game_mode.SLEEP_TEXT
'''

BOT_PL_OLD = '''    т.к. в него попадали бы только случаи блокировки.
    """
    result = await asyncio.to_thread(dispatcher.dispatch, user_input)
'''
BOT_PL_NEW = '''    т.к. в него попадали бы только случаи блокировки.
    """
''' + GAME_GUARD + '''    result = await asyncio.to_thread(dispatcher.dispatch, user_input)
'''

BOT_PD_OLD = '''    самого документа.
    """
    result = await asyncio.to_thread(dispatcher.dispatch, user_input)
'''
BOT_PD_NEW = '''    самого документа.
    """
''' + GAME_GUARD + '''    result = await asyncio.to_thread(dispatcher.dispatch, user_input)
'''

BOT_ROUTE_OLD = '''        r = await asyncio.to_thread(router.route, text, raw)
'''
BOT_ROUTE_NEW = '''        if game_mode.brain_asleep(config):   # GAME_MODE v2: только правила, без модели смысла (LLM)
            r = await asyncio.to_thread(router.route, text, raw, None)
        else:
            r = await asyncio.to_thread(router.route, text, raw)
'''

BOT_SLEEP_OLD = '''        return
    governor = context.application.bot_data["governor"]
    try:
        db_last = await asyncio.to_thread(sleep_digest.last_sleep_ts, config.OPS_DB_PATH)
'''
BOT_SLEEP_NEW = '''        return
    if game_mode.in_game(config):   # GAME_MODE v2: сон (RAG_HYGIENE: Chroma/диск) -- после игры
        logger.debug("[SLEEP] skip: game_mode")
        return
    governor = context.application.bot_data["governor"]
    try:
        db_last = await asyncio.to_thread(sleep_digest.last_sleep_ts, config.OPS_DB_PATH)
'''

BOT_FORCE_OLD = '''async def memory_ttl_job(context: ContextTypes.DEFAULT_TYPE) -> None:
'''
BOT_FORCE_NEW = '''async def handle_force_brain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/force_brain [минуты|off] (GAME_MODE v2, 02.11): аварийный клапан -- мозг в игре на время.
    Только админ (посторонний не может просадить FPS). Флаг brain_force.flag читает
    game_watcher.ps1: пока он действует, модель из памяти не выгружается."""
    if not update.message or not update.message.from_user:
        return
    user_id = update.message.from_user.id
    if user_id not in config.ADMIN_USER_IDS:
        logger.warning("Unauthorized /force_brain attempt from user_id=%s", user_id)
        return
    arg = context.args[0].strip().lower() if context.args else ""
    now = time.time()
    if arg in ("off", "0", "стоп", "выкл"):
        removed = await asyncio.to_thread(game_mode.clear_force, config.OKO_GAME_FORCE_FLAG)
        logger.info("[GAME_MODE] force=off removed=%d", int(removed))
        await update.message.reply_text(
            "🧠 Принудительное пробуждение снято." if removed else "🧠 Принудительное пробуждение не было включено.")
        return
    if not game_mode.in_game(config, now):
        await update.message.reply_text("🧠 Игровой режим не активен — мозг и так работает.")
        return
    minutes = config.OKO_GAME_FORCE_MIN
    if arg:
        try:
            minutes = int(arg)
        except ValueError:
            await update.message.reply_text("Формат: /force_brain [минуты] или /force_brain off")
            return
    minutes = max(1, min(minutes, config.OKO_GAME_FORCE_MAX_MIN))
    until = now + minutes * 60
    try:
        await asyncio.to_thread(game_mode.write_force, config.OKO_GAME_FORCE_FLAG, until)
    except OSError as e:
        logger.warning("[GAME_MODE] force=fail %s: %s", type(e).__name__, e)
        await update.message.reply_text("⚠️ Мозг не включён: не удалось записать флаг.")
        return
    gpu = config.OKO_GAME_FORCE_NUM_GPU
    where = ("на видеокарте — FPS просядет" if gpu is None
             else "на процессоре — медленно" if gpu == 0 else "частично на видеокарте")
    logger.info("[GAME_MODE] force=on min=%d num_gpu=%s", minutes, "-" if gpu is None else gpu)
    await update.message.reply_text(
        f"🧠 Мозг включён до {time.strftime('%H:%M', time.localtime(until))} ({where}). "
        f"Досрочно: /force_brain off")


async def memory_ttl_job(context: ContextTypes.DEFAULT_TYPE) -> None:
'''

BOT_REG_OLD = '''    app.add_handler(CommandHandler("garmin", handle_garmin))
'''
BOT_REG_NEW = '''    app.add_handler(CommandHandler("garmin", handle_garmin))
    app.add_handler(CommandHandler("force_brain", handle_force_brain))   # GAME_MODE v2
'''

# rel path, hash16 до, hash16 после, [(old, new)]
TARGETS = [
    ("src\\config.py", "1BF4D377081735A5", "BD34B392D2FDC994", [(CFG_OLD, CFG_NEW)]),
    ("src\\governor.py", "D63A18994B4650C8", "EE1D7390F6C0B519",
     [(GOV_IMPORT_OLD, GOV_IMPORT_NEW), (GOV_EXC_OLD, GOV_EXC_NEW),
      (GOV_ROUTE_OLD, GOV_ROUTE_NEW), (GOV_VEC_OLD, GOV_VEC_NEW)]),
    ("src\\bot.py", "6E9E6585C147AE2B", "45FF0EB51B50AEAA",
     [(BOT_IMPORT_OLD, BOT_IMPORT_NEW), (BOT_PL_OLD, BOT_PL_NEW), (BOT_PD_OLD, BOT_PD_NEW),
      (BOT_ROUTE_OLD, BOT_ROUTE_NEW), (BOT_SLEEP_OLD, BOT_SLEEP_NEW),
      (BOT_FORCE_OLD, BOT_FORCE_NEW), (BOT_REG_OLD, BOT_REG_NEW)]),
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
        shutil.copy2(p, p.with_name(p.name + ".bak_game_mode"))
    for p, rel, new in plan:
        p.write_bytes(new)
        print(f"WROTE {rel} {h16(p.read_bytes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
