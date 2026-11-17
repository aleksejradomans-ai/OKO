"""Тесты для static_analyzer.py.

Включает регрессионный тест на ложноположительное срабатывание на ЧТЕНИЕ
sys.path (найдено и исправлено в ходе разработки — изначальная реализация
ловила любое упоминание атрибута, не только мутацию).
"""
from src.static_analyzer import StaticAnalyzer


def test_eval_call_is_forbidden():
    result = StaticAnalyzer().analyze("result = eval(user_input)")
    assert result.ok is False
    assert any("eval" in v for v in result.violations)


def test_legitimate_code_passes():
    code = "def add(a, b):\n    return a + b\nprint(add(1, 2))"
    result = StaticAnalyzer().analyze(code)
    assert result.ok is True
    assert result.violations == []


def test_reading_sys_path_is_not_a_false_positive():
    """Регрессия: print(sys.path) — чистое чтение, не мутация — раньше
    ложно блокировалось, исправлено."""
    result = StaticAnalyzer().analyze("import sys\nprint(sys.path)")
    assert result.ok is True


def test_sys_path_append_is_forbidden():
    result = StaticAnalyzer().analyze('import sys\nsys.path.append("/evil/path")')
    assert result.ok is False
    assert any("sys.path" in v for v in result.violations)


def test_sys_path_reassignment_is_forbidden():
    result = StaticAnalyzer().analyze('import sys\nsys.path = ["/evil"]')
    assert result.ok is False


def test_importlib_import_module_is_forbidden():
    result = StaticAnalyzer().analyze('import importlib\nm = importlib.import_module("os")')
    assert result.ok is False


def test_subprocess_outside_whitelist_is_forbidden():
    result = StaticAnalyzer().analyze('import subprocess\nsubprocess.run(["ls"])')
    assert result.ok is False


def test_subprocess_inside_whitelisted_file_is_allowed():
    result = StaticAnalyzer().analyze(
        'import subprocess\nsubprocess.run(["ls"])',
        filename="sandbox_environment.py",
    )
    assert result.ok is True


def test_syntax_error_is_a_violation_not_silently_ok():
    """Файл, который не парсится — нарушение, не 'нет нарушений найдено'."""
    result = StaticAnalyzer().analyze("def broken(:\n    pass")
    assert result.ok is False
    assert "SyntaxError" in result.violations[0]


def test_aliased_subprocess_bypass():
    """Тест на обход через import ... as ..."""
    result = StaticAnalyzer().analyze(
        'import subprocess as sp\nsp.run(["ls"])',
        filename="attacker_file.py"
    )
    assert result.ok is False
    assert any("not allowed" in v for v in result.violations)


def test_import_from_bypass():
    """Тест на обход через from ... import ..."""
    result = StaticAnalyzer().analyze(
        'from subprocess import run\nrun(["ls"])',
        filename="attacker_file.py"
    )
    assert result.ok is False
    assert any("not allowed" in v for v in result.violations)


def test_sys_path_append_allowed_in_whitelisted_file():
    """Регрессия: knowledge_indexer.py легитимно использует sys.path.append
    для доступа к oko_rag/ (см. историю решений, аудит блока №3) — без
    whitelist ЛЮБОЙ будущий патч на этот файл был бы гарантированно
    отклонён на этапе static analysis из-за уже существующей, документированной
    строки кода, не имеющей отношения к содержанию патча."""
    result = StaticAnalyzer().analyze(
        'import sys\nsys.path.append(str(OKO_RAG_DIR))',
        filename="knowledge_indexer.py",
    )
    assert result.ok is True


def test_sys_path_append_still_forbidden_outside_whitelist():
    """Whitelist не должен ослаблять защиту для файлов вне списка."""
    result = StaticAnalyzer().analyze(
        'import sys\nsys.path.append("/evil/path")',
        filename="some_other_file.py",
    )
    assert result.ok is False
