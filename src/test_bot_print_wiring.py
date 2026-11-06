"""Проводка документов и печати в bot.py (28.10.2026), статическая проверка исходника (бот не запускается).
Решение Оператора 15:42: документ сначала ждёт выбора (кнопки), потом — печать/база/удаление и отчёт."""
import ast
from pathlib import Path

SRC = (Path(__file__).resolve().parent / "bot.py").read_text(encoding="utf-8-sig")
TREE = ast.parse(SRC)


def _fn(name):
    return next(n for n in ast.walk(TREE) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)


def _src(name):
    return ast.get_source_segment(SRC, _fn(name))


def test_modules_imported():
    names = {a.name for n in TREE.body if isinstance(n, ast.Import) for a in n.names}
    assert {"doc_print", "doc_intake"} <= names


def test_upload_goes_to_choice_not_straight_to_knowledge():
    body = _src("handle_pdf_upload")
    assert body.index("config.ADMIN_USER_IDS") < body.index("doc_print.wants_print(update.message.caption)")
    assert body.index("doc_print.wants_print(") < body.index("await _offer_document(update, doc)")
    assert "download_to_drive" not in body and 'input_dir = config.BASE_DIR / "knowledge"' not in body


def test_offer_marks_duplicates_and_uses_incoming():
    body = _src("_offer_document")
    assert "doc_intake.known_in_knowledge" in body and "_doc_keyboard(doc_id, dup)" in body
    kb = _src("_doc_keyboard")
    assert '["print", "drop"] if dup' in kb                      # дубликат: без «в базу»
    assert "doc_intake.new_path(" in _src("_download_to_incoming")


def test_failed_print_keeps_file_success_reports_fate():
    body = _src("_run_print")
    assert "asyncio.to_thread(doc_print.print_file" in body
    i_fail = body.index("if not res.ok:")
    assert i_fail < body.index("doc_intake.to_knowledge") and i_fail < body.index("doc_intake.remove")
    assert "Файл удалён" in body and "отправлен в базу" in body


def test_callback_branch_before_staging_logic():
    body = _src("handle_staging_callback")
    i = body.index('if data.startswith("doc_")')
    assert body.index("config.ADMIN_USER_IDS") < i < body.index("CATEGORY_LABELS")
    choice = _src("_handle_doc_choice")
    assert "doc_intake.find(doc_id)" in choice and "_doc_busy" in choice


def test_followup_print_message_before_model():
    """«печать» отдельным сообщением перехватывается ДО модели и после LAYOUT_FIX."""
    body = _src("handle_message")
    i = body.index("doc_print.wants_print(text)")
    assert body.index("layout_fix.normalize(text)") < i < body.index("process_logic(")
    branch = body[i:body.index("process_logic(")]
    assert "_last_document.get(user_id)" in branch and "PRINT_FOLLOWUP_S" in branch and "return" in branch
