"""bot.py: распознавание голоса под пределом времени (30.10.2026). Разбор исходника — без импорта бота."""
import ast
import pathlib

SRC = pathlib.Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig")
TREE = ast.parse(SRC)


def _fn(name):
    return next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def test_timeout_constant_defined():
    assert any(isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "STT_TIMEOUT_S" for t in n.targets)
               for n in TREE.body)


def test_transcribe_wrapped_in_wait_for():
    body = ast.unparse(_fn("handle_voice"))
    assert "asyncio.wait_for(asyncio.to_thread(stt.transcribe, path), timeout=STT_TIMEOUT_S)" in body
    assert "asyncio.to_thread(stt.transcribe, path)\n" not in body.replace("timeout=STT_TIMEOUT_S)", "")


def test_timeout_answers_user_and_does_not_raise():
    fn = _fn("handle_voice")
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    names = [ast.unparse(h.type) for h in handlers if h.type is not None]
    assert "TimeoutError" in names
    h = next(h for h in handlers if h.type is not None and ast.unparse(h.type) == "TimeoutError")
    src = ast.unparse(h)
    assert "reply_text" in src and "return" in src
