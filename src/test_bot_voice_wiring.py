"""Проводка режима телевизора и голосовых в bot.py (28.10.2026), статическая проверка (бот не запускается)."""
import ast
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent / "bot.py").read_text(encoding="utf-8-sig")
TREE = ast.parse(SRC)


def _src(name):
    node = next(n for n in ast.walk(TREE) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)
    return ast.get_source_segment(SRC, node)


def test_stt_imported_and_voice_registered_before_text():
    assert any(isinstance(n, ast.Import) and any(a.name == "stt" for a in n.names) for n in TREE.body)
    i_voice = SRC.index("MessageHandler(filters.VOICE, handle_voice)")
    assert i_voice < SRC.index("MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)")


def test_voice_admin_only_threaded_temp_removed_text_not_logged():
    body = _src("handle_voice")
    assert body.index("config.ADMIN_USER_IDS") < body.index("_download_voice(")
    assert "asyncio.to_thread(stt.transcribe" in body
    assert "finally:" in body and "path.unlink(missing_ok=True)" in body
    log_lines = [ln for ln in body.splitlines() if "logger.info" in ln]
    # текст распознавания как аргумент журнала запрещён (len(text) — можно)
    assert log_lines and not any(re.search(r",\s*text\s*[,)]", ln) for ln in log_lines)
    assert body.index("_tv_mode_active(user_id)") < body.index("await _tv_answer(user_id, text)")
    assert "понял" in body


def test_tv_command_toggles_mode():
    body = _src("handle_tv")
    assert "_tv_mode[user_id] = time.time() + TV_MODE_S" in body and "_tv_exit_word(tv_nav.canonical(payload))" in body
    assert body.index("config.ADMIN_USER_IDS") < body.index("_tv_mode[user_id]")


def test_text_in_tv_mode_goes_to_tv_before_print_and_model():
    body = _src("handle_message")
    i = body.index("if _tv_mode_active(user_id):")
    assert body.index("layout_fix.normalize(text)") < i < body.index("doc_print.wants_print(text)")
    assert i < body.index("process_logic(")
    branch = body[i:body.index("doc_print.wants_print(text)")]
    # 21:56: телевизору — ИСХОДНЫЙ текст (layout_fix портит латиницу команд), выход — через tv_nav.canonical
    assert "await _tv_answer(user_id, raw_text)" in branch and "update.message.text" in branch
    assert "_tv_exit_word(tv_nav.canonical(raw_text))" in branch


def test_mode_expires_and_has_exit():
    active = _src("_tv_mode_active")
    assert "time.time() > deadline" in active and "_tv_mode.pop(user_id, None)" in active
    assert "хватит" in SRC[SRC.index("TV_MODE_EXIT ="):SRC.index("TV_MODE_EXIT =") + 200]
    assert "TV_MODE_S = 300" in SRC


def test_download_separate_from_recognition_with_transport_retry():
    """21:29: TimedOut скачивания был показан как «не получилось распознать». Транспорт — отдельно."""
    body = _src("handle_voice")
    i_dl = body.index("await _download_voice(msg.voice, path)")
    i_stt = body.index("asyncio.to_thread(stt.transcribe")
    assert i_dl < body.index("Не скачал голосовое") < i_stt
    dl = _src("_download_voice")
    assert "_is_tg_transport_error(e)" in dl and "raise" in dl and "VOICE_DL_ATTEMPTS" in dl
    assert "read_timeout=" in dl


def test_stt_warmup_scheduled():
    assert "app.job_queue.run_once(_stt_warmup" in SRC
    assert "asyncio.to_thread(stt.load)" in _src("_stt_warmup")
