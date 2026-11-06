"""Проводка сводок Garmin в bot.py (29.10.2026), статическая проверка: 08:00 и 21:00 с поясом, сеть в потоке,
только при токенах, /garmin только для админа."""
import ast
from pathlib import Path

SRC = (Path(__file__).resolve().parent / "bot.py").read_text(encoding="utf-8-sig")
TREE = ast.parse(SRC)


def _src(name):
    node = next(n for n in ast.walk(TREE) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)
    return ast.get_source_segment(SRC, node)


def test_schedule_8_and_21_with_zone_only_with_tokens():
    i = SRC.index("if garmin_report.enabled():")
    block = SRC[i:i + 500]
    assert '((8, "morning"), (21, "evening"))' in block
    assert "app.job_queue.run_daily(garmin_job, time=garmin_report.local_time(_hh)" in block


def test_job_threads_and_broadcasts():
    body = _src("garmin_job")
    assert "asyncio.to_thread(garmin_report.run, kind)" in body and "_broadcast_to_admins(context, text)" in body
    assert "except Exception" in body


def test_command_admin_only():
    body = _src("handle_garmin")
    assert body.index("config.ADMIN_USER_IDS") < body.index("garmin_report.run")
    assert 'CommandHandler("garmin", handle_garmin)' in SRC
