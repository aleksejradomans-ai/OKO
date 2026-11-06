"""KNOWLEDGE_BUDGET (01.11): справка RAG по остатку окна модели."""
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
