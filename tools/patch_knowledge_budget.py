"""KNOWLEDGE_BUDGET (01.11): справка RAG режется по остатку окна модели.

Причина: [CTX] 21:33:19 fill=0.842 -- фрагмент RAG 2530 симв. (knowledge_len=2530)
при поле ~0.61 без справки. retriever.build_context режет только по max_chars=3000
и только по границе чанка; остаток окна не учитывается.

Правит:
  src/bot.py       -- _knowledge_budget/_fit_knowledge/_system_chars + вызов в process_logic
  src/governor.py  -- публичный AgentGovernor.system_prompt_chars()
Создаёт:
  src/test_knowledge_budget.py

Применяется только к известным версиям (SHA256-префикс). Повторный запуск -- no-op.
Запуск из C:\\oko:  python tools\\patch_knowledge_budget.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "src" / "bot.py"
GOV = ROOT / "src" / "governor.py"
TEST = ROOT / "src" / "test_knowledge_budget.py"

EXPECTED = {BOT: "ECBFE63287F1", GOV: "E2DA03B48DA2"}
MARKER = "KNOWLEDGE_BUDGET"

# ---------------------------------------------------------------- bot.py
BOT_ANCHOR_FUNCS = r'''def _estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN_ESTIMATE)
'''

BOT_NEW_FUNCS = r'''def _estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN_ESTIMATE)


# KNOWLEDGE_BUDGET (01.11): справка RAG режется по остатку окна модели. Всплеск
# [CTX] fill=0.842 (21:33:19) -- один фрагмент RAG 2530 симв. поверх пола ~0.61;
# retriever режет только по max_chars=3000 и по границе чанка, окно не учитывает.
# Порядок частей промпта не меняется; режется только справка, с хвоста.
KNOWLEDGE_GEN_RESERVE_TOKENS = int(os.getenv("OKO_KNOWLEDGE_GEN_RESERVE", "1024"))
PROMPT_OVERHEAD_CHARS = 700   # XML-обвязка _build_user_prompt + заголовки payload
KNOWLEDGE_MIN_CHARS = 300     # меньше -- обрывок без смысла, справка не включается
KNOWLEDGE_CUT_MARK = "\n[…справка обрезана по размеру окна модели]"
KNOWLEDGE_NO_ROOM = "Справка не включена: окно модели занято историей и фактами."


def _system_chars(governor) -> int:
    """Длина system prompt основного контура. Нет метода (мок в тестах) -- 0."""
    fn = getattr(governor, "system_prompt_chars", None)
    try:
        return int(fn()) if callable(fn) else 0
    except Exception:
        return 0


def _knowledge_budget(system_chars: int, history: str, facts: str, user_input: str) -> int:
    """Сколько символов справки помещается: (nctx - резерв генерации) * симв/токен
    минус остальные части промпта. nctx -- config.OKO_RUNTIME_NUM_CTX (то же
    значение, по которому считается fill в [CTX])."""
    nctx = int(getattr(config, "OKO_RUNTIME_NUM_CTX", 4096))
    usable = (nctx - KNOWLEDGE_GEN_RESERVE_TOKENS) * CHARS_PER_TOKEN_ESTIMATE
    used = (system_chars + len(history or "") + len(facts or "")
            + len(user_input or "") + PROMPT_OVERHEAD_CHARS)
    return max(0, int(usable - used))


def _fit_knowledge(knowledge: str, budget: int) -> str:
    """Справка в бюджет: без изменений, если влезает; иначе голова (ближайший чанк
    идёт первым) по последнему переводу строки + пометка. Бюджет < KNOWLEDGE_MIN_CHARS --
    честная строка «не включена» вместо обрывка."""
    if len(knowledge) <= budget:
        return knowledge
    if budget < KNOWLEDGE_MIN_CHARS:
        return KNOWLEDGE_NO_ROOM
    head = knowledge[: budget - len(KNOWLEDGE_CUT_MARK)]
    nl = head.rfind("\n")
    if nl >= len(head) // 2:
        head = head[:nl]
    return head.rstrip() + KNOWLEDGE_CUT_MARK
'''

BOT_OLD_FACTS_RE = re.compile(r"^    facts = _home_facts\(\)[^\n]*\n", re.MULTILINE)
BOT_PAYLOAD_RE = re.compile(r"^    # Экранирование внутренних XML-тегов \(knowledge_base/user_input\) происходит\n",
                            re.MULTILINE)
BOT_NEW_BLOCK = (
    "    facts = _home_facts()               # EVENTS (этап C): факты дома -- в канале справочных данных\n"
    "    # KNOWLEDGE_BUDGET (01.11): справка по остатку окна; knowledge_len выше -- длина ДО обрезки.\n"
    "    budget = _knowledge_budget(_system_chars(governor), formatted_history, facts, sanitized_input)\n"
    "    fitted = _fit_knowledge(knowledge, budget)\n"
    "    if fitted != knowledge:\n"
    "        logger.info(\"[KNOWLEDGE_CUT] user=%s len=%d->%d budget=%d\",\n"
    "                    user_id, len(knowledge), len(fitted), budget)\n"
    "        knowledge = fitted\n"
    "\n"
)

# ---------------------------------------------------------------- governor.py
GOV_ANCHOR = r'''    @property
    def transport_gate_state(self) -> str:
'''
GOV_NEW = r'''    def system_prompt_chars(self) -> int:
        """KNOWLEDGE_BUDGET: длина system prompt основного контура (identity +
        каталог) -- для бюджета справки в bot.py. Только чтение из памяти."""
        return len(self._main_system() or "")

    @property
    def transport_gate_state(self) -> str:
'''

# ---------------------------------------------------------------- test
TEST_SRC = r'''"""KNOWLEDGE_BUDGET (01.11): справка RAG по остатку окна модели."""
from __future__ import annotations


def _bot():
    try:
        import bot
    except ImportError:
        from src import bot
    return bot


def test_fits_unchanged():
    b = _bot()
    assert b._fit_knowledge("короткая справка", 1000) == "короткая справка"


def test_cut_respects_budget_and_marks():
    b = _bot()
    text = ("строка справки\n" * 300).strip()
    out = b._fit_knowledge(text, 1200)
    assert len(out) <= 1200
    assert out.endswith(b.KNOWLEDGE_CUT_MARK)
    assert "строка справки" in out


def test_cut_on_line_boundary():
    b = _bot()
    text = "\n".join(f"line{i:04d}" for i in range(500))
    out = b._fit_knowledge(text, 1000)
    body = out[: -len(b.KNOWLEDGE_CUT_MARK)]
    assert body.split("\n")[-1].startswith("line") and len(body.split("\n")[-1]) == 8


def test_no_room_is_explicit():
    b = _bot()
    assert b._fit_knowledge("x" * 5000, b.KNOWLEDGE_MIN_CHARS - 1) == b.KNOWLEDGE_NO_ROOM


def test_budget_formula(monkeypatch):
    b = _bot()
    monkeypatch.setattr(b.config, "OKO_RUNTIME_NUM_CTX", 4096, raising=False)
    monkeypatch.setattr(b, "KNOWLEDGE_GEN_RESERVE_TOKENS", 1024)
    usable = int((4096 - 1024) * b.CHARS_PER_TOKEN_ESTIMATE)
    got = b._knowledge_budget(100, "h" * 200, "", "q" * 10)
    assert got == usable - (100 + 200 + 0 + 10 + b.PROMPT_OVERHEAD_CHARS)


def test_budget_never_negative(monkeypatch):
    b = _bot()
    monkeypatch.setattr(b.config, "OKO_RUNTIME_NUM_CTX", 2048, raising=False)
    assert b._knowledge_budget(10_000, "h" * 10_000, "", "q") == 0


def test_spike_case_lands_below_previous_fill(monkeypatch):
    """21:33:19: остальное ~6515 симв., справка 2530 -> после обрезки суммарно <= usable."""
    b = _bot()
    monkeypatch.setattr(b.config, "OKO_RUNTIME_NUM_CTX", 4096, raising=False)
    monkeypatch.setattr(b, "KNOWLEDGE_GEN_RESERVE_TOKENS", 1024)
    budget = b._knowledge_budget(2500, "h" * 3000, "f" * 300, "q" * 37)
    out = b._fit_knowledge("k\n" * 1265, budget)
    assert len(out) < 2530
    total = 2500 + 3000 + 300 + 37 + b.PROMPT_OVERHEAD_CHARS + len(out)
    assert total <= (4096 - 1024) * b.CHARS_PER_TOKEN_ESTIMATE


def test_system_chars_defensive():
    b = _bot()

    class NoMethod:
        pass

    class Boom:
        def system_prompt_chars(self):
            raise RuntimeError("x")

    class Ok:
        def system_prompt_chars(self):
            return 123

    assert b._system_chars(NoMethod()) == 0
    assert b._system_chars(Boom()) == 0
    assert b._system_chars(Ok()) == 123


def test_governor_system_prompt_chars():
    from governor import AgentGovernor
    gov = AgentGovernor.__new__(AgentGovernor)
    gov.identity_text = "identity"
    gov._main_system_prompt = "identity\n\ncatalog"
    assert gov.system_prompt_chars() == len("identity\n\ncatalog")
    gov._main_system_prompt = None
    assert gov.system_prompt_chars() == len("identity")
'''


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest().upper()[:12]


def _read(p: Path) -> tuple[str, str, bool]:
    raw = p.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), eol, bom


def _write(p: Path, text: str, eol: str, bom: bool) -> None:
    data = text.replace("\n", eol).encode("utf-8")
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)


def _replace_once(text: str, old: str, new: str, where: str) -> str:
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"FAIL anchor {where}: found {n}, expected 1")
    return text.replace(old, new, 1)


def main() -> int:
    for p in (BOT, GOV):
        if not p.exists():
            print(f"FAIL missing {p}")
            return 2
    bot_text, bot_eol, bot_bom = _read(BOT)
    gov_text, gov_eol, gov_bom = _read(GOV)

    if MARKER in bot_text and "def system_prompt_chars" in gov_text and TEST.exists():
        print("SKIP already applied")
        return 0

    for p, want in EXPECTED.items():
        got = _sha(p)
        if got != want:
            print(f"FAIL hash {p.name}: {got} != {want} (файл не той версии, патч не применён)")
            return 3

    bot_text = _replace_once(bot_text, BOT_ANCHOR_FUNCS, BOT_NEW_FUNCS, "bot:_estimate_tokens")
    if len(BOT_OLD_FACTS_RE.findall(bot_text)) != 1:
        print("FAIL anchor bot:facts line")
        return 4
    bot_text = BOT_OLD_FACTS_RE.sub("", bot_text, count=1)
    if len(BOT_PAYLOAD_RE.findall(bot_text)) != 1:
        print("FAIL anchor bot:payload comment")
        return 4
    bot_text = BOT_PAYLOAD_RE.sub(lambda m: BOT_NEW_BLOCK + m.group(0), bot_text, count=1)
    gov_text = _replace_once(gov_text, GOV_ANCHOR, GOV_NEW, "governor:transport_gate_state")

    for name, src in (("bot.py", bot_text), ("governor.py", gov_text), ("test", TEST_SRC)):
        try:
            compile(src, name, "exec")
        except SyntaxError as e:
            print(f"FAIL syntax {name}: {e}")
            return 5

    _write(BOT, bot_text, bot_eol, bot_bom)
    _write(GOV, gov_text, gov_eol, gov_bom)
    TEST.write_text(TEST_SRC, encoding="utf-8")
    print(f"OK bot.py {_sha(BOT)}  governor.py {_sha(GOV)}  test_knowledge_budget.py {_sha(TEST)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
