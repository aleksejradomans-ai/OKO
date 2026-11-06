"""Пакет исправлений по аудиту 06.10: /status, обработчик ошибок, понятные ошибки,
подпись [Око], /memory, опечатка в просьбе о языке. Всё или ничего по файлу."""
import ast, py_compile, shutil, sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\oko\src")


def load(p):
    raw = p.read_bytes(); bom = raw.startswith(b"\xef\xbb\xbf")
    s = raw.decode("utf-8-sig"); nl = "\r\n" if "\r\n" in s else "\n"
    return s.replace("\r\n", "\n"), bom, nl


def save(p, s, bom, nl):
    ast.parse(s)
    shutil.copy2(p, p.with_suffix(".py.prefix"))
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + s.replace("\n", nl).encode("utf-8"))
    py_compile.compile(str(p), doraise=True)
    print(f"{p.name}: ok")


def rep(s, old, new, name, count=1):
    if s.count(old) != count:
        print(f"[ABORT] {name}: якорь найден {s.count(old)} раз (ожидалось {count}): {old[:60]!r}")
        sys.exit(1)
    return s.replace(old, new)


# ------------------------------------------------------------------ bot.py
bp = ROOT / "bot.py"
s, bom, nl = load(bp)
if "def handle_memory" in s:
    print("bot.py: уже применено")
else:
    s = rep(s, 'self.name = "Алексей"', 'self.name = "Око"', "bot.py")
    s = rep(s, '        f"state: {governor.breaker_state}\\n"',
            '        f"state: {getattr(governor, \'breaker_state\', \'не подключён к governor\')}\\n"', "bot.py")
    s = rep(s, "def _with_notice(response: str, incidents) -> str:", '''_ERR_TEXT = {
    "LLM_UNAVAILABLE": "Модель не ответила за отведённое время или недоступна. "
                       "Запрос не сохранён в память. Помогает более короткий запрос или повтор через минуту.",
    "SYSTEM_DEGRADED": "Контур в режиме деградации: проверка ответов временно отключена. Повторите позже.",
    "HALT_CONDITION": "Ответ модели заблокирован проверкой (логическое противоречие).",
    "RETRY_LIMIT_EXCEEDED": "Не удалось получить корректный ответ после нескольких попыток.",
    "RECOVERY_FAILED": "Сбой при попытке восстановления ответа.",
}


def _explain_error(response: str) -> str:
    """Служебный маркер [ERR: X] -> код + понятное объяснение. Код сохраняется
    для диагностики, объяснение - для Оператора. Остальной текст не трогается."""
    m = re.search(r"\\[ERR: (\\w+)\\]", response or "")
    if not m or m.group(1) not in _ERR_TEXT:
        return response
    return response.replace(m.group(0), f"{m.group(0)} {_ERR_TEXT[m.group(1)]}", 1)


def _with_notice(response: str, incidents) -> str:''', "bot.py")
    s = rep(s, "    clean_response = core.clean_output(raw_response)\n",
            "    clean_response = core.clean_output(_explain_error(raw_response))\n", "bot.py")
    s = rep(s, "async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:",
            '''async def handle_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Детерминированный ответ на «что ты обо мне хранишь» - из memory.db, без LLM.
    Аудит 06.10: модель на этот вопрос уверенно отвечала «личные данные не
    хранятся», хотя журнал хранит всю переписку."""
    if update.message is None or update.effective_user is None:
        return
    from datetime import datetime as _dt
    core = _get_memory()
    inv = core.inventory(core.resolve("telegram", str(update.effective_user.id)))
    f = lambda t: _dt.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"
    ttl = globals().get("MEMORY_TTL_DAYS")
    lines = [
        "ПАМЯТЬ (прямо из memory.db, без участия модели)",
        f"записей: {inv['events']} - ваши вопросы и ответы Око, после маскирования ключей и паролей",
        f"период: {f(inv['first_ts'])} - {f(inv['last_ts'])}",
        f"каналы: {', '.join(ch for ch, _ in inv['aliases']) or '-'}",
        f"производных ярлыков и выводов о вас: {inv['derived_labels']}",
        f"срок хранения: {f'{ttl:g} дн.' if ttl else 'не ограничен'}",
        "также: снапшоты базы (копии журнала), логи службы (без текста сообщений)",
        "удаление: /forget" if "handle_forget" in globals() else "удаление: команда /forget ещё не подключена",
    ]
    await update.message.reply_text("\\n".join(lines))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Без обработчика исключение в команде терялось молча (аудит 06.10: /status
    падал на каждом вызове, Оператор не получал ответа вообще)."""
    logger.error("Unhandled exception in handler: %r", context.error, exc_info=context.error)
    msg = update.effective_message if isinstance(update, Update) else None
    if msg is not None:
        try:
            await msg.reply_text("[ERR: INTERNAL] Внутренняя ошибка обработчика, подробности в логе службы.")
        except Exception:
            pass


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:''', "bot.py")
    s = rep(s, '    app.add_handler(CommandHandler("status", handle_status))\n',
            '    app.add_handler(CommandHandler("status", handle_status))\n'
            '    app.add_handler(CommandHandler("memory", handle_memory))\n'
            '    app.add_error_handler(on_error)\n', "bot.py")
    save(bp, s, bom, nl)

# ------------------------------------------------------------- test_bot.py
tb = ROOT / "test_bot.py"
if tb.exists():
    s, bom, nl = load(tb)
    if "[Алексей]" in s:
        s = s.replace("[Алексей]", "[Око]")
        save(tb, s, bom, nl)
    else:
        print("test_bot.py: уже применено или нет ожиданий подписи")

# ---------------------------------------------------- structural_check.py
sc = ROOT / "structural_check.py"
s, bom, nl = load(sc)
if "на [ае]нгл" in s:
    print("structural_check.py: уже применено")
else:
    s = rep(s, 'r"на англ\\w*|по-англ\\w*|', 'r"на [ае]нгл\\w*|по-[ае]нгл\\w*|', "structural_check.py")
    save(sc, s, bom, nl)

ts = ROOT / "test_structural_check.py"
s, bom, nl = load(ts)
if "енглийском" not in s:
    s = s.rstrip("\n") + '''


def test_language_request_with_typo():
    """Аудит 06.10: «на енглийском» не распознавалось как просьба."""
    q = "Повтори тот же запрос, только ответ на енглийском"
    assert check(EN, sources=[IDENTITY], question=q) == []
'''
    save(ts, s, bom, nl)