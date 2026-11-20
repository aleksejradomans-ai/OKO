"""Длинный ответ: судья по кускам, закрыто при любом сбое (решение Оператора 20.11).

Ответ, который не помещается в окно судьи, не уходит «с отметкой» мимо проверки: он режется на куски с нахлёстом,
каждый кусок проходит судью в режиме «только блокировка». Текст отдаётся только при PASS на всех кусках; FAIL / HALT /
нераспознанный вердикт / сбой судьи / транспорт на любом куске блокирует весь ответ тем же кодом, что и раньше;
кусков больше потолка -- JUDGE_TOO_LONG (без усечения). Оба контура: обычный (окно рантайма) и /discuss.
Сеть не затрагивается: изолированный Registry на tmp_path, http-клиент -- двойник.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

MARK = "ЗАПРЕТНАЯ МЕТКА"                 # «нарушение»: двойник судьи отвечает FAIL, если видит её в куске целиком
CONTOURS = ["main", "discuss"]


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
    gov._self_correct = AsyncMock(side_effect=AssertionError("коррекция для многокускового ответа не запускается"))
    yield gov, client
    Registry._instance = None


def run(coro):
    return asyncio.run(coro)


def st(gov, key):
    return gov.registry.get_state(key)


def window(contour):
    import config
    import governor
    return config.OKO_RUNTIME_NUM_CTX if contour == "main" else governor.DISCUSS_NUM_CTX


def nctx_arg(contour):
    import governor
    return None if contour == "main" else governor.DISCUSS_NUM_CTX


def ask(gov, contour):
    if contour == "main":
        return run(gov.execute_request("вопрос"))
    return run(gov.execute_discuss_request("что сказано?", "текст документа", 111))


def by_mark(_k, checked):
    return ok("FAIL:POL_001") if MARK in checked else ok("PASS")


def wire(client, answer, verdict=by_mark):
    """Двойник http: генерация отдаёт answer; на вызов судьи отвечает verdict(номер_вызова, проверяемый_текст).
    Возвращает список текстов, реально ушедших судье."""
    seen = []

    async def post(_url, **kw):
        prompt = kw["json"]["prompt"]
        if "TEXT TO CHECK" not in prompt:
            return ok(answer)
        checked = prompt.split("<response_to_check>\n", 1)[1].rsplit("\n</response_to_check>", 1)[0]
        seen.append(checked)
        out = verdict(len(seen), checked)
        if isinstance(out, BaseException):
            raise out
        return out
    client.post = AsyncMock(side_effect=post)
    return seen


def sentences(contour, chunks=3.5, mark_at=None):
    """Связный текст из коротких предложений длиной около chunks размеров куска; mark_at (доля 0..1) -- где
    стоит предложение с меткой."""
    import config
    size = config.judge_chunk_chars(window(contour))
    parts, total, i = [], 0, 0
    while total < size * chunks:
        s = f"Запись номер {i}: значение порога равно {i % 97}. "
        parts.append(s)
        total += len(s)
        i += 1
    if mark_at is not None:
        parts[min(len(parts) - 1, int(len(parts) * mark_at))] = f"Здесь стоит {MARK}. "
    return "".join(parts).rstrip()                                       # генерация отдаёт ответ без хвостовых пробелов


@contextmanager
def logs():
    import governor

    class H(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())
    h = H()
    old = governor.logger.level
    governor.logger.addHandler(h)
    governor.logger.setLevel(logging.DEBUG)
    try:
        yield h.lines
    finally:
        governor.logger.removeHandler(h)
        governor.logger.setLevel(old)


# --- нарезка: чистая функция ---------------------------------------------------------------------------------------
def _spans(text, chunks, size, overlap):
    """Положение каждого куска в тексте; заодно сверка: куски -- ровно эти фрагменты текста."""
    import governor
    spans = governor.judge_spans(text, size, overlap)
    assert [text[a:b] for a, b in spans] == chunks
    return spans


@pytest.mark.parametrize("size,overlap", [(200, 20), (617, 61), (6144, 614), (50, 5)])
def test_split_invariants_size_coverage_overlap(size, overlap):
    import governor
    text = "".join(f"Предложение {i} о пороге {i * 7}! Второе, подлиннее, с запятыми и числом {i}.\n\n" if i % 5 == 0
                   else f"Короткое {i}. " for i in range(400))
    chunks = governor.split_for_judge(text, size, overlap)
    assert len(chunks) > 1 and all(0 < len(c) <= size for c in chunks)
    spans = _spans(text, chunks, size, overlap)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)                # покрытие от начала до конца
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        assert a0 < b0 < a1 < b1                                         # сосед начинается внутри предыдущего: нахлёст


def test_split_is_deterministic():
    import governor
    text = sentences("main", 5)
    one = governor.split_for_judge(text, 6144, 614)
    assert one == governor.split_for_judge(text, 6144, 614) == governor.split_for_judge(str(text), 6144, 614)
    assert [len(c) for c in one] == [len(c) for c in governor.split_for_judge(text, 6144, 614)]


def test_split_cuts_on_sentence_boundaries_and_overlap_holds_a_whole_sentence():
    import governor
    sents = [f"Запись номер {i}: значение равно {i % 97}. " for i in range(600)]
    text = "".join(sents)
    chunks = governor.split_for_judge(text, 2000, 200)
    starts = set()
    pos = 0
    for s in sents:
        starts.add(pos)
        pos += len(s)
    starts.add(pos)
    spans = _spans(text, chunks, 2000, 200)
    assert all(a in starts and b in starts for a, b in spans)            # ни одно предложение не разрезано
    for (_, a1), (b0, _) in zip(spans, spans[1:]):
        assert a1 - b0 >= min(len(s) for s in sents)                     # в нахлёсте -- не меньше одного предложения
        assert a1 - b0 <= 200


def test_split_paragraphs_are_boundaries():
    import governor
    text = "\n\n".join(f"Абзац {i} без точки в конце" for i in range(200))
    chunks = governor.split_for_judge(text, 600, 60)
    assert all(c.startswith("Абзац ") for c in chunks)


def test_split_long_sentence_and_text_without_spaces_are_still_cut():
    import governor
    for text in ("слово " * 3000, "ж" * 9000):
        chunks = governor.split_for_judge(text, 1000, 100)
        assert all(len(c) <= 1000 for c in chunks)
        spans = _spans(text, chunks, 1000, 100)
        assert spans[0][0] == 0 and spans[-1][1] == len(text)
        assert all(a0 < b0 < a1 for (a0, a1), (b0, _) in zip(spans, spans[1:]))


def test_split_short_or_empty_text_is_one_piece():
    import governor
    assert governor.split_for_judge("Коротко.", 100, 10) == ["Коротко."]
    assert governor.split_for_judge("", 100, 10) == [""]
    with pytest.raises(ValueError):
        governor.split_for_judge("текст", 1, 1)


def test_split_marker_at_any_position_is_whole_in_some_chunk():
    """Нарушение не может лечь на стык: где бы ни стояла фраза, она целиком есть хотя бы в одном куске."""
    import governor
    base = "слово " * 2000                                               # одно «предложение»: стыки идут по словам
    for at in range(0, len(base) - 20, 6 * 37):
        text = base[:at] + "ЖЖЖЖЖ ЖЖЖЖЖ " + base[at + 12:]
        assert any("ЖЖЖЖЖ ЖЖЖЖЖ" in c for c in governor.split_for_judge(text, 1000, 100)), at


# --- константы: размер куска выведен из окна -----------------------------------------------------------------------
def test_chunk_size_is_derived_from_the_window(gc):
    import config
    import governor
    gov, _ = gc
    assert config.judge_chunk_chars(4096) == (4096 - config.JUDGE_FRAME_RESERVE_TOKENS) * config.JUDGE_CHARS_PER_TOKEN
    assert config.judge_chunk_chars(4096) == 6144 and config.judge_chunk_chars(8192) == 14336
    assert config.judge_chunk_chars(config.JUDGE_FRAME_RESERVE_TOKENS) == 0
    assert config.judge_overlap_chars(6144) == 614 and config.JUDGE_CHUNK_OVERLAP_PCT == 10
    assert config.JUDGE_CHUNK_MAX == 8 and governor.JUDGE_CHARS_PER_TOKEN == config.JUDGE_CHARS_PER_TOKEN
    frame_tokens = (len(gov._judge_prompt("")) + 1) // config.JUDGE_CHARS_PER_TOKEN
    assert frame_tokens <= config.JUDGE_FRAME_RESERVE_TOKENS             # запас покрывает фактическую рамку промпта
    for contour in CONTOURS:                                             # кусок полного размера помещается в окно
        full = "я" * config.judge_chunk_chars(window(contour))
        assert gov._judge_fits(full, nctx_arg(contour))
        assert not gov._judge_fits("я" * (window(contour) * config.JUDGE_CHARS_PER_TOKEN), nctx_arg(contour))


def test_tiny_window_blocks_instead_of_guessing(gc, monkeypatch):
    import config
    gov, client = gc
    monkeypatch.setattr(config, "OKO_RUNTIME_NUM_CTX", 512)             # окно меньше запаса под промпт судьи
    seen = wire(client, "обычный ответ")
    assert ask(gov, "main") == "[ERR: JUDGE_TOO_LONG]" and seen == []


# --- оба контура: все PASS / нарушение в начале, середине, конце -----------------------------------------------------
@pytest.mark.parametrize("contour", CONTOURS)
def test_all_chunks_pass_returns_the_whole_answer(gc, contour):
    gov, client = gc
    answer = sentences(contour)
    pieces = gov._judge_pieces(answer, nctx_arg(contour))
    seen = wire(client, answer)
    with logs() as lines:
        assert ask(gov, contour) == answer
    assert len(pieces) >= 3 and seen == pieces                           # каждый кусок -- отдельный вызов судьи
    assert st(gov, "last_compliance_status") == "PASS" and st(gov, "judge_multi_checks") == 1
    assert st(gov, "last_judge_block") is None and gov._self_correct.await_count == 0
    (t,) = [x for x in lines if x.startswith("[JUDGE_TIME]")]
    assert f"where={contour} calls={len(pieces)} " in t and "result=PASS" in t and f"text_len={len(answer)}" in t
    assert not [x for x in lines if "[JUDGE_INPUT]" in x]


@pytest.mark.parametrize("contour", CONTOURS)
@pytest.mark.parametrize("mark_at,where", [(0.0, "first"), (0.5, "middle"), (0.9999, "last")])
def test_violation_in_any_chunk_blocks_the_whole_answer(gc, contour, mark_at, where):
    import governor
    gov, client = gc
    answer = sentences(contour, mark_at=mark_at)
    pieces = gov._judge_pieces(answer, nctx_arg(contour))
    n = len(pieces)
    i = next(k for k, p in enumerate(pieces, 1) if MARK in p)
    assert n >= 3 and where == ("first" if i == 1 else "last" if i == n else "middle")
    seen = wire(client, answer)
    with logs() as lines:
        out = ask(gov, contour)
    assert out == "[ERR: COMPLIANCE_UNRESOLVED]" and seen == pieces[:i]  # после блокирующего куска -- ни одного вызова
    assert gov._self_correct.await_count == 0                            # коррекция не запускается
    assert st(gov, "last_compliance_status") == "FAIL" and st(gov, "compliance_violations") == 1
    assert st(gov, "judge_multi_checks") == 1 and st(gov, f"judge_multi_block_{where}") == 1
    assert sum(st(gov, f"judge_multi_block_{w}") for w in ("first", "middle", "last")) == 1
    block = st(gov, "last_judge_block")
    assert block.startswith(f"chunk {i}/{n} verdict=FAIL rule=POL_001 chunk_len={len(pieces[i - 1])} ")
    assert f"chunk_sha16={governor.text_sha16(pieces[i - 1])}" in block and f"text_len={len(answer)}" in block
    assert f"text_sha16={governor.text_sha16(answer)}" in block and MARK not in block
    assert [x for x in lines if x.startswith("[JUDGE_CHUNK] blocked chunk %d/%d " % (i, n))]
    assert all(MARK not in x and "Запись номер" not in x for x in lines)  # текста ответа в логе нет
    (t,) = [x for x in lines if x.startswith("[JUDGE_TIME]")]
    assert f"calls={i} " in t and "result=COMPLIANCE_UNRESOLVED" in t


@pytest.mark.parametrize("contour", CONTOURS)
def test_violation_exactly_on_the_seam_is_caught_by_the_overlap(gc, contour):
    import config
    gov, client = gc
    size = config.judge_chunk_chars(window(contour))
    base = ("слово " * int(size * 3.5 / 6)).rstrip()                     # без знаков конца: стык идёт по словам
    seam = len(gov._judge_pieces(base, nctx_arg(contour))[0])            # здесь кончается первый кусок
    phrase = MARK[:5] + " " + MARK[:5]                                   # 5 букв, пробел, 5 букв -- как «слово слово»
    answer = base[:seam - 6] + phrase + base[seam + 5:]
    assert len(answer) == len(base) and answer[seam - 1] == " "          # пробел фразы стоит ровно на стыке
    pieces = gov._judge_pieces(answer, nctx_arg(contour))
    assert len(pieces[0]) == seam                                        # нарезка та же: стык на прежнем месте
    assert phrase not in answer[:seam] and phrase not in answer[seam:]   # без нахлёста фраза не попала бы ни в один кусок
    assert phrase not in pieces[0] and phrase in pieces[1]               # нахлёст: второй кусок держит её целиком
    seen = wire(client, answer, lambda _k, c: ok("FAIL:POL_001") if phrase in c else ok("PASS"))
    assert ask(gov, contour) == "[ERR: COMPLIANCE_UNRESOLVED]" and len(seen) == 2
    assert st(gov, "last_judge_block").startswith(f"chunk 2/{len(pieces)} verdict=FAIL")


@pytest.mark.parametrize("contour", CONTOURS)
def test_halt_and_unparseable_verdict_on_a_chunk_block(gc, contour):
    gov, client = gc
    answer = sentences(contour)
    seen = wire(client, answer, lambda k, _c: ok("HALT:POL_002") if k == 2 else ok("PASS"))
    assert ask(gov, contour) == "[ERR: HALT_CONDITION]" and len(seen) == 2
    assert st(gov, "last_compliance_status") == "HALT" and "verdict=HALT rule=POL_002" in st(gov, "last_judge_block")
    seen = wire(client, answer, lambda k, _c: ok("кажется, всё хорошо") if k == 3 else ok("PASS"))
    assert ask(gov, contour) == "[ERR: JUDGE_UNPARSEABLE]" and len(seen) == 3
    assert st(gov, "last_compliance_status") == "UNCHECKED" and "chunk 3/" in st(gov, "last_judge_block")


# --- сбой судьи на куске: логика общего контура ---------------------------------------------------------------------
@pytest.mark.parametrize("contour", CONTOURS)
def test_judge_failure_on_chunk_k_with_successful_retry_passes(gc, contour):
    gov, client = gc
    answer = sentences(contour)
    pieces = gov._judge_pieces(answer, nctx_arg(contour))
    seen = wire(client, answer, lambda k, _c: bad() if k == 2 else ok("PASS"))    # 2-й вызов -- сбой, повтор успешен
    with logs() as lines:
        assert ask(gov, contour) == answer
    assert seen == pieces[:2] + pieces[1:]                               # повторён тот же кусок, затем остальные
    assert st(gov, "judge_fail_streak") == 0 and st(gov, "last_compliance_status") == "PASS"
    (t,) = [x for x in lines if x.startswith("[JUDGE_TIME]")]
    assert f"calls={len(pieces) + 1} " in t


@pytest.mark.parametrize("contour", CONTOURS)
def test_judge_failure_on_chunk_k_twice_blocks_with_judge_error(gc, contour):
    gov, client = gc
    answer = sentences(contour)
    pieces = gov._judge_pieces(answer, nctx_arg(contour))
    seen = wire(client, answer, lambda k, _c: bad() if k in (2, 3) else ok("PASS"))
    out = ask(gov, contour)
    assert out == "[ERR: JUDGE_ERROR]" and seen == [pieces[0], pieces[1], pieces[1]]   # один повтор, дальше не идём
    assert st(gov, "last_compliance_status") == "UNCHECKED" and st(gov, "judge_fail_streak") == 1
    assert "chunk 2/%d verdict=JUDGE_FAILED" % len(pieces) in st(gov, "last_judge_block")
    assert st(gov, "judge_multi_block_middle") == 1


def test_judge_degraded_counter_is_shared_between_single_and_chunked_checks(gc):
    gov, client = gc
    wire(client, "короткий ответ", lambda _k, _c: bad())
    assert ask(gov, "main") == "[ERR: JUDGE_ERROR]" and ask(gov, "discuss") == "[ERR: JUDGE_ERROR]"
    wire(client, sentences("main"), lambda _k, _c: bad())
    assert ask(gov, "main") == "[ERR: JUDGE_ERROR]"
    assert st(gov, "judge_fail_streak") == 3 and st(gov, "judge_state") == "JUDGE_DEGRADED"
    assert gov.pop_judge_notice().startswith("Проверяющий сбоит 3 раз подряд")


@pytest.mark.parametrize("contour", CONTOURS)
def test_transport_error_on_a_chunk_goes_to_the_transport_branch(gc, contour):
    import httpx
    gov, client = gc
    answer = sentences(contour)
    seen = wire(client, answer, lambda k, _c: httpx.ConnectError("down") if k == 2 else ok("PASS"))
    assert ask(gov, contour) == "[ERR: LLM_UNAVAILABLE]" and len(seen) == 2          # без повтора и без продолжения
    assert st(gov, "last_compliance_status") == "UNCHECKED" and st(gov, "judge_fail_streak") == 0
    assert st(gov, "transport_failures") == 1 and "verdict=TRANSPORT" in st(gov, "last_judge_block")
    seen = wire(client, answer, lambda k, _c: asyncio.TimeoutError() if k == 1 else ok("PASS"))
    assert ask(gov, contour) == "[ERR: LLM_TIMEOUT]" and len(seen) == 1


# --- потолок кусков -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("contour", CONTOURS)
def test_more_chunks_than_the_ceiling_blocks_without_truncation(gc, contour):
    import config
    import governor
    gov, client = gc
    answer = sentences(contour, chunks=config.JUDGE_CHUNK_MAX + 1)
    n = len(gov._judge_pieces(answer, nctx_arg(contour)))
    assert n > config.JUDGE_CHUNK_MAX
    seen = wire(client, answer)
    with logs() as lines:
        out = ask(gov, contour)
    assert out == "[ERR: JUDGE_TOO_LONG]" and seen == []                 # судья не вызывается: частичной проверки нет
    assert st(gov, "last_compliance_status") == "UNCHECKED" and st(gov, "compliance_check_failures") == 1
    assert st(gov, "judge_multi_too_long") == 1 and st(gov, "judge_multi_checks") == 1
    assert st(gov, "last_judge_block") == (f"too long: chunks={n} max={config.JUDGE_CHUNK_MAX} "
                                           f"text_len={len(answer)} text_sha16={governor.text_sha16(answer)}")
    assert gov.monitor.get_unchecked_rate() == 1.0
    assert all("Запись номер" not in x for x in lines)


def test_ceiling_boundary_exactly_max_chunks_is_checked(gc, monkeypatch):
    import config
    gov, client = gc
    answer = sentences("main")
    n = len(gov._judge_pieces(answer))
    monkeypatch.setattr(config, "JUDGE_CHUNK_MAX", n)
    seen = wire(client, answer)
    assert ask(gov, "main") == answer and len(seen) == n
    monkeypatch.setattr(config, "JUDGE_CHUNK_MAX", n - 1)
    seen = wire(client, answer)
    assert ask(gov, "main") == "[ERR: JUDGE_TOO_LONG]" and seen == []


def test_too_long_code_is_yellow_and_explained_to_the_user():
    import bot
    import governor
    assert "JUDGE_TOO_LONG" in governor.YELLOW_CODES and governor.is_service_marker("[ERR: JUDGE_TOO_LONG]")
    assert "слишком длинным" in bot._ERR_TEXT["JUDGE_TOO_LONG"] and "не отдаётся" in bot._ERR_TEXT["JUDGE_TOO_LONG"]


# --- короткий ответ: как раньше ------------------------------------------------------------------------------------
@pytest.mark.parametrize("contour", CONTOURS)
def test_short_answer_is_one_judge_call_as_before(gc, contour):
    gov, client = gc
    seen = wire(client, "Порог равен 0.48.")
    assert ask(gov, contour) == "Порог равен 0.48." and seen == ["Порог равен 0.48."]
    assert st(gov, "judge_multi_checks") == 0 and st(gov, "last_judge_block") is None


def test_normal_contour_short_answer_still_goes_through_correction(gc):
    gov, client = gc
    gov._self_correct = AsyncMock(return_value="исправленный ответ")
    seen = wire(client, "исходный ответ", lambda k, _c: ok("FAIL:POL_001") if k == 1 else ok("PASS"))
    assert ask(gov, "main") == "исправленный ответ" and seen == ["исходный ответ", "исправленный ответ"]
    assert st(gov, "last_compliance_status") == "PASS_AFTER_CORRECTION" and gov._self_correct.await_count == 1


# --- обычный контур: текст после коррекции не помещается в окно -----------------------------------------------------
def test_oversize_text_after_correction_is_checked_by_chunks(gc):
    gov, client = gc
    corrected = sentences("main")
    pieces = gov._judge_pieces(corrected)
    gov._self_correct = AsyncMock(return_value=corrected)
    seen = wire(client, "исходный ответ", lambda k, _c: ok("FAIL:POL_001") if k == 1 else ok("PASS"))
    assert ask(gov, "main") == corrected and seen == ["исходный ответ"] + pieces
    assert st(gov, "last_compliance_status") == "PASS_AFTER_CORRECTION" and st(gov, "compliance_violations") == 1


def test_oversize_text_after_correction_with_violation_is_blocked(gc):
    gov, client = gc
    corrected = sentences("main", mark_at=0.5)
    gov._self_correct = AsyncMock(return_value=corrected)
    wire(client, "исходный ответ", lambda k, c: ok("FAIL:POL_001") if (k == 1 or MARK in c) else ok("PASS"))
    assert ask(gov, "main") == "[ERR: COMPLIANCE_UNRESOLVED]"
    assert st(gov, "last_compliance_status") == "FAIL_AFTER_CORRECTION"
    assert st(gov, "compliance_violations") == 1                         # нарушение посчитано один раз, на первом проходе
    assert gov._self_correct.await_count == 1                            # второй коррекции нет


# --- нижний уровень: судья не вызывается с текстом, который не помещается в окно ------------------------------------
@pytest.mark.parametrize("contour", CONTOURS)
def test_verify_compliance_refuses_oversize_input(gc, contour):
    import governor
    gov, client = gc
    huge = "я" * (window(contour) * governor.JUDGE_CHARS_PER_TOKEN)
    kw = {} if contour == "main" else {"num_ctx": governor.DISCUSS_NUM_CTX}
    with pytest.raises(governor.JudgeInputTooLong) as e:
        run(gov._verify_compliance(huge, **kw))
    assert client.post.call_count == 0 and "яяя" not in str(e.value) and f"text_len={len(huge)}" in str(e.value)
    with pytest.raises(governor.JudgeInputTooLong):
        run(gov._judge_call(huge, nctx_arg(contour)))                    # не повторяется: текст тот же
    assert client.post.call_count == 0


def test_chunk_that_does_not_fit_blocks_with_too_long(gc, monkeypatch):
    """Страховка: если кусок всё же не помещается в окно (расчёт размера разошёлся с рамкой) -- блокировка."""
    import config
    gov, client = gc
    monkeypatch.setattr(config, "judge_chunk_chars", lambda _n: 3 * 4096)
    seen = wire(client, ("я " * 9000).rstrip())
    assert ask(gov, "main") == "[ERR: JUDGE_TOO_LONG]" and seen == []
    assert "verdict=TOO_LONG" not in (st(gov, "last_judge_block") or "") and st(gov, "judge_multi_too_long") == 1


# --- сторож (AST): ни одна ветка не отдаёт текст без PASS по всем кускам --------------------------------------------
def _fn(name):
    import governor
    src = inspect.getsource(getattr(governor.AgentGovernor, name))
    return ast.parse("class _X:\n" + src).body[0].body[0]


def test_ast_enforce_checks_the_window_before_every_judge_call():
    fn = _fn("_enforce_compliance")
    guards = [n for n in ast.walk(fn) if isinstance(n, ast.If)
              and ast.unparse(n.test).startswith("not self._judge_fits(")]
    assert [ast.unparse(g.test) for g in guards] == ["not self._judge_fits(response)",
                                                     "not self._judge_fits(corrected)"]
    for g, name in zip(guards, ("response", "corrected")):
        (ret,) = g.body
        assert isinstance(ret, ast.Return) and isinstance(ret.value, ast.Await) and not g.orelse
        call = ret.value.value
        assert call.func.attr == "_judge_block_only" and call.args[0].id == name
    # каждый вызов судьи -- сразу после своей проверки окна (следующий оператор -- try с этим вызовом)
    for g, name in zip(guards, ("response", "corrected")):
        nxt = fn.body[fn.body.index(g) + 1]
        assert isinstance(nxt, ast.Try)
        calls = [n for n in ast.walk(nxt) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "_judge_call"]
        assert len(calls) == 1 and calls[0].args[0].id == name
    attrs = [n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)]
    assert attrs.count("_judge_call") == 2 and attrs.count("_judge_fits") == 2


def test_ast_verify_compliance_raises_before_calling_the_model():
    fn = _fn("_verify_compliance")
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    assert len(raises) == 1 and raises[0].exc.func.id == "JudgeInputTooLong"
    (guard,) = [n for n in fn.body if isinstance(n, ast.If) and raises[0] in n.body]
    assert ast.unparse(guard.test) == "est_tokens > nctx"
    llm = [n for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == "_call_llm_with_timeout"]
    assert len(llm) == 1 and llm[0].lineno > guard.lineno                # вызов модели -- только после проверки


def test_ast_judge_is_called_only_through_the_guarded_paths():
    """_verify_compliance зовёт только _judge_call; _judge_call -- только _enforce_compliance и _judge_block_only."""
    import governor
    tree = ast.parse(inspect.getsource(governor))
    users = {"_verify_compliance": set(), "_judge_call": set(), "_judge_block_only": set()}
    for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        for a in (n for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr in users):
            users[a.attr].add(fn.name)
    assert users["_verify_compliance"] == {"_judge_call"}
    assert users["_judge_call"] == {"_enforce_compliance", "_judge_block_only"}
    assert users["_judge_block_only"] == {"_enforce_compliance", "execute_discuss_request"}


def test_passed_with_a_mark_is_gone_from_the_source():
    import governor
    src = inspect.getsource(governor)
    assert "JUDGE_INPUT]" not in src and "may truncate the checked text" not in src


def test_readme_describes_the_chunked_check():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "JUDGE_TOO_LONG" in readme and "по кускам" in readme and "без усечения" in readme
