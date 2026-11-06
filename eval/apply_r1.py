"""R1, шаг 2: /forget (подтверждение), TTL журнала, физическое стирание, без sessions.
Всё или ничего по каждому файлу; py_compile после записи."""
import ast, py_compile, shutil, sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\oko\src")


def load(p):
    raw = p.read_bytes(); bom = raw.startswith(b"\xef\xbb\xbf")
    s = raw.decode("utf-8-sig"); nl = "\r\n" if "\r\n" in s else "\n"
    return s.replace("\r\n", "\n"), bom, nl


def save(p, s, bom, nl, tag):
    ast.parse(s)
    shutil.copy2(p, p.with_suffix(".py." + tag))
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + s.replace("\n", nl).encode("utf-8"))
    py_compile.compile(str(p), doraise=True)
    print(f"{p.name}: ok")


def need(s, anchor, name):
    if s.count(anchor) != 1:
        print(f"[ABORT] {name}: якорь не найден/не уникален: {anchor[:60]!r}"); sys.exit(1)


# ---------------------------------------------------------------- memory_core
mc = ROOT / "memory_core.py"
s, bom, nl = load(mc)
if "def apply_ttl" in s:
    print("memory_core.py: уже применено")
else:
    old_start = s.index("    def forget(self, subject_id: str) -> None:")
    old_end = s.index("    def snapshot(self)")
    s = s[:old_start] + '''    def forget(self, subject_id: str, purge_snapshots: bool = True) -> int:
        """Только по команде владельца. Удаляет всё о subject и стирает физически:
        строки после DELETE живут в -wal (до checkpoint) и в свободных страницах
        (до VACUUM); снапшоты - полные копии журнала, поэтому пересоздаются."""
        with self._tx() as c:
            n = c.execute("SELECT COUNT(*) FROM events WHERE subject_id=?",
                          (subject_id,)).fetchone()[0]
        self.index.delete_subject(subject_id)
        with self._tx() as c:
            c.execute("DELETE FROM subjects WHERE subject_id=?", (subject_id,))
        self._physical_wipe()
        if purge_snapshots:
            self._reset_snapshots()
        return n

    def apply_ttl(self, days: float) -> int:
        """Удаляет события старше days. Порог считается в Python как число:
        явно и без опоры на правила приведения типов SQLite."""
        cutoff = time.time() - days * 86400
        with self._tx() as c:
            n = c.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
        if n:
            self._physical_wipe()
            self._reset_snapshots()
        return n

    def _physical_wipe(self) -> None:
        c = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.execute("VACUUM")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()

    def _reset_snapshots(self) -> None:
        """Старые снапшоты содержат удалённое -> удалить и снять один чистый."""
        if not self.backup_dir or not self.backup_dir.exists():
            return
        for old in self.backup_dir.glob("memory_*.db"):
            old.unlink()
        self.snapshot()

''' + s[old_end:]
    save(mc, s, bom, nl, "prer1")

# ---------------------------------------------------------------------- bot
bp = ROOT / "bot.py"
s, bom, nl = load(bp)
if "def handle_forget" in s:
    print("bot.py: уже применено")
else:
    tree = ast.parse(s)
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "setup_sessions_table"), None)
    if fn is None:
        print("[ABORT] setup_sessions_table не найдена"); sys.exit(1)
    lines = s.split("\n")
    del lines[fn.lineno - 1:fn.end_lineno]
    s = "\n".join(lines)
    CALL = "    setup_sessions_table()\n"
    need(s, CALL, "bot.py"); s = s.replace(CALL, "")

    HB = "async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:"
    need(s, HB, "bot.py")
    s = s.replace(HB, '''MEMORY_TTL_DAYS = float(os.getenv("OKO_MEMORY_TTL_DAYS", "0"))  # 0 = TTL выключен; по сроку удаляет только при явном значении


async def handle_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R1: /forget показывает, что будет удалено; /forget confirm удаляет.
    Удаляется вся память ЭТОГО пользователя: журнал, следы в файлах SQLite
    (WAL, свободные страницы), снапшоты. Необратимо, поэтому два шага."""
    if update.message is None or update.effective_user is None:
        return
    user_id = update.effective_user.id
    core = _get_memory()
    sid = core.resolve("telegram", str(user_id))
    telegram_note = ("Переписку в самом Telegram это не затрагивает: "
                     "её можно удалить только в приложении Telegram.")
    if not context.args or context.args[0].lower() != "confirm":
        n = core.inventory(sid)["events"]
        await update.message.reply_text(
            f"Будет удалено безвозвратно: {n} записей памяти, их копии в снапшотах "
            f"и следы в файлах базы.\\n{telegram_note}\\nПодтвердить: /forget confirm"
        )
        return
    n = await asyncio.to_thread(core.forget, sid)
    logger.warning("R1 /forget executed: user=%s events=%s", user_id, n)
    await update.message.reply_text(
        f"Удалено записей: {n}. Журнал, снапшоты и файлы базы очищены.\\n{telegram_note}"
    )


async def memory_ttl_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """R1: события старше MEMORY_TTL_DAYS удаляются с физическим стиранием."""
    if MEMORY_TTL_DAYS <= 0:
        return
    try:
        n = await asyncio.to_thread(_get_memory().apply_ttl, MEMORY_TTL_DAYS)
        if n:
            logger.info("R1 TTL: removed %d events older than %g days", n, MEMORY_TTL_DAYS)
    except Exception as e:
        logger.error("R1 TTL job failed: %s", e)


''' + HB)

    REG = '    app.add_handler(CommandHandler("enddiscuss", handle_enddiscuss))\n'
    need(s, REG, "bot.py")
    s = s.replace(REG, REG + '    app.add_handler(CommandHandler("forget", handle_forget))\n')
    JOB = "    app.job_queue.run_repeating(heartbeat_job, interval=30.0, first=0.0)\n"
    need(s, JOB, "bot.py")
    s = s.replace(JOB, JOB + "    app.job_queue.run_repeating(memory_ttl_job, interval=6 * 3600, first=60.0)\n")
    save(bp, s, bom, nl, "prer1")

# -------------------------------------------------------------------- tests
tp = ROOT / "test_memory_r1.py"
if tp.exists():
    print("test_memory_r1.py: уже есть")
else:
    tp.write_text('''"""R1: забывание и TTL стирают данные физически и не трогают лишнего."""
import time

from src.memory_core import Envelope, MemoryCore


class _NullIndex:
    def upsert(self, *a): pass
    def delete_subject(self, *a): pass
    def search(self, *a): return []


SECRET = "секретная-фраза-r1-test"


def _core(tmp_path):
    return MemoryCore(tmp_path / "m.db", _NullIndex(), lambda t: (t, []),
                      backup_dir=tmp_path / "backups")


def _all_bytes(tmp_path):
    return b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


def test_forget_wipes_journal_wal_and_snapshots(tmp_path):
    core = _core(tmp_path)
    env = Envelope("telegram", "1", SECRET)
    core.record_exchange(env, SECRET, "ответ")
    core.snapshot()
    sid = core.resolve("telegram", "1")
    assert core.forget(sid) == 2
    assert SECRET.encode("utf-8") not in _all_bytes(tmp_path)
    assert len(list((tmp_path / "backups").glob("memory_*.db"))) == 1   # один чистый


def test_ttl_removes_only_old(tmp_path):
    core = _core(tmp_path)
    old = time.time() - 40 * 86400
    sid = core.resolve("telegram", "1")
    core.import_legacy("telegram", "1", [("user", SECRET, old), ("assistant", "старый", old)])
    core.record_exchange(Envelope("telegram", "1", "новый"), "новый", "свежий ответ")
    assert core.apply_ttl(30) == 2
    assert [c for _, c in core.history(sid)] == ["новый", "свежий ответ"]
    assert SECRET.encode("utf-8") not in _all_bytes(tmp_path)


def test_ttl_noop_keeps_snapshots(tmp_path):
    core = _core(tmp_path)
    core.record_exchange(Envelope("telegram", "1", "q"), "q", "a")
    snap = core.snapshot()
    assert core.apply_ttl(30) == 0
    assert snap.exists()
''', encoding="utf-8")
    print("test_memory_r1.py: ok")