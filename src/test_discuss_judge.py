"""/discuss через судью «только блокировка» (решение Оператора 20.11: единая проверяемость ответов).

execute_discuss_request: после генерации -- один проход проверки ВСЕГО ответа тем же судьёй, без коррекции:
  PASS -> ответ как есть; FAIL -> COMPLIANCE_UNRESOLVED; HALT -> HALT_CONDITION; нераспознанный вердикт ->
  JUDGE_UNPARSEABLE; сбой судьи -> один повтор -> JUDGE_ERROR (счётчик и JUDGE_DEGRADED общие с обычным контуром);
  транспортный сбой судьи -> обычная транспортная ветка. Текст модели без вердикта PASS не отдаётся.
Сеть не затрагивается: изолированный Registry на tmp_path, http-клиент -- двойник.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


def ok(text):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"response": text})
    return resp


def bad():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"error_judge": "model not loaded"})
    return resp


@pytest.fixture
def gc(tmp_path):
    import httpx
    from src.registry import Registry
    Registry._instance = None
    Registry(filepath=str(tmp_path / "registry_state.json"))
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    import governor
    gov = governor.AgentGovernor(http_client=client)
    yield gov, client
    Registry._instance = None


def run(coro):
    return asyncio.run(coro)


def st(gov, key):
    return gov.registry.get_state(key)


def discuss(gov, q="что сказано?", doc="текст документа"):
    return run(gov.execute_discuss_request(q, doc, 111))


def prompts(client):
    return [c.kwargs["json"]["prompt"] for c in client.post.call_args_list]


ANSWER = "В документе сказано: порог 0.48."


# --- вердикты ---------------------------------------------------------------------------------------------------
def test_pass_returns_answer_and_counts_as_checked(gc):
    gov, client = gc
    client.post.side_effect = [ok(ANSWER), ok("PASS")]
    assert discuss(gov) == ANSWER
    assert client.post.call_count == 2                                   # генерация + судья, не больше
    assert st(gov, "last_compliance_status") == "PASS" and gov.monitor.get_unchecked_rate() == 0.0


def test_fail_blocks_without_correction_pass(gc):
    gov, client = gc
    gov._self_correct = AsyncMock(side_effect=AssertionError("коррекция в /discuss не вызывается"))
    client.post.side_effect = [ok(ANSWER), ok("FAIL:POL_001")]
    out = discuss(gov)
    assert out == "[ERR: COMPLIANCE_UNRESOLVED]" and ANSWER not in out
    assert client.post.call_count == 2 and gov._self_correct.await_count == 0
    assert st(gov, "last_compliance_status") == "FAIL" and st(gov, "compliance_violations") == 1
    assert gov.monitor.get_error_rate() == 1.0


def test_halt_blocks_with_halt_condition(gc):
    gov, client = gc
    client.post.side_effect = [ok(ANSWER), ok("HALT:POL_002")]
    out = discuss(gov)
    assert out == "[ERR: HALT_CONDITION]" and client.post.call_count == 2
    assert st(gov, "last_compliance_status") == "HALT"


def test_unparseable_verdict_blocks(gc):
    gov, client = gc
    client.post.side_effect = [ok(ANSWER), ok("не могу оценить")]
    assert discuss(gov) == "[ERR: JUDGE_UNPARSEABLE]"
    assert st(gov, "last_compliance_status") == "UNCHECKED" and st(gov, "compliance_check_failures") == 1


# --- сбой судьи: логика общего контура -----------------------------------------------------------------------------
def test_judge_error_retried_once_then_judge_error(gc):
    gov, client = gc
    client.post.side_effect = [ok(ANSWER), bad(), bad()]
    out = discuss(gov)
    assert out == "[ERR: JUDGE_ERROR]" and ANSWER not in out
    assert client.post.call_count == 3                                   # генерация + судья + ОДИН повтор
    assert st(gov, "last_compliance_status") == "UNCHECKED" and st(gov, "judge_fail_streak") == 1


def test_judge_error_then_retry_pass_returns_answer(gc):
    gov, client = gc
    client.post.side_effect = [ok(ANSWER), bad(), ok("PASS")]
    assert discuss(gov) == ANSWER and st(gov, "judge_fail_streak") == 0


def test_streak_and_degraded_are_shared_with_the_normal_contour(gc):
    gov, client = gc
    client.post.side_effect = [ok("обычный ответ"), bad(), bad()]          # обычный контур: сбой судьи №1
    assert run(gov.execute_request("вопрос")) == "[ERR: JUDGE_ERROR]"
    client.post.side_effect = [ok(ANSWER), bad(), bad()]                   # /discuss: №2
    assert discuss(gov) == "[ERR: JUDGE_ERROR]"
    client.post.side_effect = [ok(ANSWER), bad(), bad()]                   # /discuss: №3 -> порог
    assert discuss(gov) == "[ERR: JUDGE_ERROR]"
    assert st(gov, "judge_state") == "JUDGE_DEGRADED" and st(gov, "judge_fail_streak") == 3
    assert gov.pop_judge_notice().startswith("Проверяющий сбоит 3 раз подряд")
    client.post.side_effect = [ok(ANSWER), ok("PASS")]                     # восстановление через /discuss
    assert discuss(gov) == ANSWER and st(gov, "judge_state") == "OK"
    assert gov.pop_judge_notice() == "Проверяющий снова работает."


# --- транспорт ---------------------------------------------------------------------------------------------------------
def test_judge_transport_error_goes_to_transport_branch_not_retried(gc):
    import httpx
    gov, client = gc
    client.post.side_effect = [ok(ANSWER), httpx.ConnectError("down")]
    out = discuss(gov)
    assert out == "[ERR: LLM_UNAVAILABLE]" and client.post.call_count == 2
    assert st(gov, "last_compliance_status") == "UNCHECKED" and st(gov, "judge_fail_streak") == 0
    assert st(gov, "transport_failures") == 1


def test_judge_timeout_blocks_with_llm_timeout(gc):
    gov, client = gc
    client.post.side_effect = [ok(ANSWER), asyncio.TimeoutError()]
    assert discuss(gov) == "[ERR: LLM_TIMEOUT]"


def test_generation_failures_keep_their_codes_and_do_not_call_the_judge(gc):
    import httpx
    gov, client = gc
    client.post.side_effect = [httpx.ConnectError("down")]
    assert discuss(gov) == "[ERR: DISCUSS_FAULT]" and client.post.call_count == 1
    client.post.reset_mock()
    client.post.side_effect = [asyncio.TimeoutError()]
    assert discuss(gov) == "[ERR: DISCUSS_TIMEOUT]" and client.post.call_count == 1


# --- весь ответ, контекст судьи, отметка о возможном усечении ---------------------------------------------------------
def test_long_answer_goes_to_the_judge_whole(gc):
    import governor
    gov, client = gc
    long_answer = "НАЧАЛО " + ("слово " * 1300) + "КОНЕЦ-МЕТКА"            # ~7,8 тыс. символов
    client.post.side_effect = [ok(long_answer), ok("PASS")]
    assert discuss(gov) == long_answer
    judge_prompt = prompts(client)[1]
    assert "TEXT TO CHECK" in judge_prompt and long_answer in judge_prompt   # целиком, не хвост и не начало
    opts = client.post.call_args_list[1].kwargs["json"]["options"]
    assert opts["num_ctx"] == governor.DISCUSS_NUM_CTX and opts["temperature"] == 0 and opts["seed"] == 0


def test_normal_contour_judge_options_unchanged(gc):
    gov, client = gc
    client.post.side_effect = [ok("ответ"), ok("PASS")]
    assert run(gov.execute_request("вопрос")) == "ответ"
    opts = client.post.call_args_list[1].kwargs["json"]["options"]
    assert opts == {"temperature": 0, "seed": 0}                          # num_ctx судьи обычного контура не задан


def test_answer_over_the_window_is_cut_into_chunks_not_passed_with_a_mark(gc):
    """Ответ, не помещающийся в окно судьи: «прошёл с отметкой» больше нет -- проверка по кускам, без текста в логе."""
    import governor
    gov, client = gc
    huge = "секретная фраза. " + ("я " * (governor.DISCUSS_NUM_CTX * governor.JUDGE_CHARS_PER_TOKEN)).rstrip()
    pieces = gov._judge_pieces(huge, governor.DISCUSS_NUM_CTX)
    assert len(pieces) > 1
    client.post.side_effect = [ok(huge)] + [ok("PASS")] * len(pieces)

    class H(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.records = []

        def emit(self, record):
            self.records.append(record)
    h = H()
    governor.logger.addHandler(h)
    old = governor.logger.level
    governor.logger.setLevel(logging.DEBUG)
    try:
        assert discuss(gov) == huge
    finally:
        governor.logger.removeHandler(h)
        governor.logger.setLevel(old)
    assert not [r for r in h.records if "[JUDGE_INPUT]" in r.getMessage()]
    assert all("секретная фраза" not in r.getMessage() for r in h.records)
    judged = prompts(client)[1:]
    assert len(judged) == len(pieces) and all(huge not in p for p in judged)   # целиком в один вызов не уходит
    assert all(piece in p for piece, p in zip(pieces, judged))
    assert "JUDGE_INPUT" not in inspect.getsource(governor)


def test_short_answer_is_one_judge_call(gc):
    gov, client = gc
    client.post.side_effect = [ok(ANSWER), ok("PASS")]
    assert discuss(gov) == ANSWER and client.post.call_count == 2
    assert st(gov, "judge_multi_checks") == 0


# --- бот: пояснение кода и строки лога без текста ------------------------------------------------------------------------
def test_bot_log_text_is_length_and_fingerprint(monkeypatch):
    import bot
    import config
    monkeypatch.setattr(config, "DIAG_LOG_TEXT", False)
    s = bot._log_text("  [ERR: JUDGE_ERROR]  ")
    assert s == f"len=18 sha16={bot._sha16('[ERR: JUDGE_ERROR]')}" and "ERR" not in s
    monkeypatch.setattr(config, "DIAG_LOG_TEXT", True)
    assert bot._log_text("[ERR: JUDGE_ERROR]") == "[OKO_DIAG_LOG_TEXT=1] '[ERR: JUDGE_ERROR]'"


def test_bot_history_skipped_lines_use_log_text():
    import bot
    src = inspect.getsource(bot)
    assert src.count("_log_text(raw_response), user_id)") == 2 and "raw_response.strip()[:40]" not in src


def test_bot_explains_unresolved_for_discuss_too():
    import bot
    assert "/discuss" in bot._ERR_TEXT["COMPLIANCE_UNRESOLVED"]


# --- сторож (AST): текст модели -- только после PASS ---------------------------------------------------------------------
def _fn(name):
    import governor
    src = inspect.getsource(getattr(governor.AgentGovernor, name))
    return ast.parse("class _X:\n" + src).body[0].body[0]


def test_ast_discuss_returns_only_service_codes_or_the_judged_result():
    fn = _fn("execute_discuss_request")
    rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    in_handlers = {id(r) for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)
                   for r in ast.walk(h) if isinstance(r, ast.Return)}
    assert len(in_handlers) == 3
    for r in rets:
        names = {n.id for n in ast.walk(r) if isinstance(n, ast.Name)}
        if id(r) in in_handlers:
            assert isinstance(r.value, ast.Call) and getattr(r.value.func, "id", "") == "log_service_code"
            assert "answer" not in names
        else:                                                          # единственный выход с текстом -- через судью
            assert isinstance(r.value, ast.Name) and r.value.id == "checked"
    (assign,) = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "checked" for t in n.targets)]
    assert isinstance(assign.value, ast.Await) and assign.value.value.func.attr == "_judge_block_only"
    assert assign.value.value.args[0].id == "answer"
    assert len(rets) == 4
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "_self_correct" not in attrs and "_enforce_compliance" not in attrs


def test_ast_block_only_returns_text_only_after_pass_on_every_chunk_and_never_corrects():
    fn = _fn("_judge_block_only")
    attrs = [n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)]
    assert "_self_correct" not in attrs and attrs.count("_judge_call") == 1 and "_verify_compliance" not in attrs
    # единственный возврат текста -- последний оператор функции, сразу после цикла по кускам
    text_returns = [r for r in ast.walk(fn) if isinstance(r, ast.Return) and isinstance(r.value, ast.Name)]
    assert [r.value.id for r in text_returns] == ["response"] and fn.body[-1] is text_returns[0]
    (loop,) = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    assert loop in fn.body and not loop.orelse
    assert ast.unparse(loop.iter) == "enumerate(pieces, 1)"
    after = fn.body[fn.body.index(loop) + 1:]
    assert all(not isinstance(n, (ast.If, ast.For, ast.While, ast.Try)) for n in after)
    # из цикла к возврату текста ведёт только исчерпание кусков: break нет, continue один и только под PASS,
    # тело цикла заканчивается возвратом служебного кода
    assert not [n for n in ast.walk(loop) if isinstance(n, ast.Break)]
    conts = [n for n in ast.walk(loop) if isinstance(n, ast.Continue)]
    assert len(conts) == 1
    (pass_if,) = [n for n in loop.body if isinstance(n, ast.If) and conts[0] in n.body]
    assert ast.unparse(pass_if.test) == "result.verdict == ComplianceVerdict.PASS" and not pass_if.orelse
    assert isinstance(loop.body[-1], ast.Return) and isinstance(loop.body[-1].value, ast.Call)
    # все прочие возвраты -- служебные коды, без текста ответа
    for r in (n for n in ast.walk(fn) if isinstance(n, ast.Return) and n is not text_returns[0]):
        call = r.value.value if isinstance(r.value, ast.Await) else r.value
        assert isinstance(call, ast.Call)
        f = call.func
        assert (f.attr if isinstance(f, ast.Attribute) else f.id) in {
            "_transport_failure", "_judge_failure", "_judge_too_long", "_handle_halt_condition", "log_service_code"}
    # обработчики исключений: каждый заканчивается возвратом
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 3 and all(isinstance(h.body[-1], ast.Return) for h in handlers)


def test_docstring_no_longer_says_bypass():
    import governor
    doc = governor.AgentGovernor.execute_discuss_request.__doc__
    assert "только блокировка" in doc and "compliance-judge пайплайна" not in doc
