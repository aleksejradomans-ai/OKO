"""bot.py: задача смены сети подключена (29.10.2026). Разбор исходника — без импорта бота и Telegram."""
import ast
import pathlib

SRC = pathlib.Path(__file__).with_name("bot.py").read_text(encoding="utf-8-sig")
TREE = ast.parse(SRC)


def _job():
    for node in TREE.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "net_context_job":
            return node
    raise AssertionError("net_context_job не найдена на уровне модуля")


def test_module_imports_net_context():
    assert any(isinstance(n, ast.Import) and any(a.name == "net_context" for a in n.names) for n in TREE.body)


def test_job_runs_in_thread_and_broadcasts():
    body = ast.unparse(_job())
    assert "asyncio.to_thread(net_context.poll_once)" in body
    assert "_broadcast_to_admins(context, text)" in body
    assert any(isinstance(n, ast.Try) for n in _job().body), "сбой опроса не должен ронять JobQueue"


def test_job_registered_once():
    calls = [n for n in ast.walk(TREE) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("run_repeating")
             and n.args and ast.unparse(n.args[0]) == "net_context_job"]
    assert len(calls) == 1
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw.get("interval") == "net_context.POLL_S"
