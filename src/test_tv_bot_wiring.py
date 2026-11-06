"""Проверка подключения телевизора к боту по исходнику `bot.py`.

ПОЧЕМУ ПО ИСХОДНИКУ, А НЕ ИМПОРТОМ. `bot.py` тянет telegram-библиотеку,
конфигурацию, базы и сеть; импортировать его в сюите дорого и ненадёжно.
Поэтому проверка статическая — разбор AST. Этого достаточно для того, что
здесь нужно доказать: не поведение обработчика (оно покрыто в
`test_tv_bot_handler.py`), а именно СПОСОБ подключения.

ЗАЧЕМ ВООБЩЕ. 27.10 выяснилось, что расчёт внутри непокрытой функции
ломается молча, а согласованно устаревшая пара «модуль + его тесты» проходит
зелёной. `bot.py` тестами почти не затронут, и три правки подключения —
импорт, обёртка в поток, регистрация команды — ровно тот код, который может
тихо исчезнуть при следующей правке файла на 109 КБ. Этот модуль стоит
сторожем на каждой из них.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BOT_PATH = Path(__file__).with_name("bot.py")


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    if not BOT_PATH.exists():  # pragma: no cover - только вне каталога src
        pytest.skip(f"не найден {BOT_PATH}")
    return ast.parse(BOT_PATH.read_text(encoding="utf-8-sig"))


@pytest.fixture(scope="module")
def handle_tv(tree: ast.Module) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_tv":
            return node
    pytest.fail("в bot.py нет async def handle_tv")


def _calls(node: ast.AST) -> list[str]:
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            out.append(ast.unparse(sub.func))
    return out


def test_module_imported(tree: ast.Module) -> None:
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "tv_bot_handler" in names


def test_command_registered(tree: ast.Module) -> None:
    found = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("CommandHandler")
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "tv"
    ]
    assert found, "CommandHandler(\"tv\", ...) не зарегистрирован"
    assert "handle_tv" in found[0]


def test_handler_is_admin_gated(handle_tv: ast.AsyncFunctionDef) -> None:
    body = ast.unparse(handle_tv)
    assert "config.ADMIN_USER_IDS" in body, "нет проверки прав"
    assert "not in config.ADMIN_USER_IDS" in body, "проверка прав должна быть fail-closed"


def test_handler_does_not_block_event_loop(handle_tv: ast.AsyncFunctionDef) -> None:
    """Синхронный HTTPS в событийном цикле останавливает ВСЕХ пользователей.

    Таймаут клиента 5 с; без выноса в поток на это время встаёт весь бот.
    """
    calls = _calls(handle_tv)
    assert "asyncio.to_thread" in calls, "вызов обработчика должен уходить в поток"
    direct = [c for c in calls if c == "tv_bot_handler.handle"]
    assert not direct, "tv_bot_handler.handle вызван напрямую, минуя поток"


def test_handler_catches_exceptions(handle_tv: ast.AsyncFunctionDef) -> None:
    """Сбой контура телевизора не должен ронять обработку сообщений."""
    assert any(isinstance(n, ast.Try) for n in ast.walk(handle_tv))


def test_tv_not_wired_into_plain_text_flow(tree: ast.Module) -> None:
    """Команды телевизора идут ТОЛЬКО по префиксу.

    Подключение разбора к общему потоку сообщений делает бота глухим: любой
    вопрос не про телевизор получает отказ разбора, а висящее ожидание
    подтверждения перехватывает следующую реплику.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not ast.unparse(node.func).endswith("MessageHandler"):
            continue
        rendered = ast.unparse(node)
        assert "tv_bot_handler" not in rendered and "handle_tv" not in rendered, (
            f"разбор телевизора подключён к потоку сообщений: {rendered}"
        )


def test_payload_passed_whole(handle_tv: ast.AsyncFunctionDef) -> None:
    """Аргументы команды склеиваются обратно в текст.

    `context.args` режет строку по пробелам; без склейки «подсветка красная»
    пришло бы в разбор одним словом.
    """
    body = ast.unparse(handle_tv)
    assert "context.args" in body
    assert "join" in body
