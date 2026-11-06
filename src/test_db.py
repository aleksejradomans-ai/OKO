"""Тесты для db.py.

db.py состоит из одной функции-фабрики (get_connection), которая жёстко
ссылается на config.DB_PATH — не принимает путь как аргумент, в отличие
от Registry/SnapshotManager/StagingManager. Это значит, что monkeypatch
переменной окружения AI_BASE_DIR здесь НЕ работает: config.BASE_DIR (и
производный от него config.DB_PATH) вычисляется один раз при первом
импорте config.py и не пересчитывается — та же причина, по которой
Registry потребовал explicit filepath (см. историю решений). Здесь
единственный корректный способ изоляции — monkeypatch самого атрибута
config.DB_PATH напрямую, не переменной окружения.
"""
import sqlite3
from pathlib import Path

import pytest

import config
import db


@pytest.fixture
def isolated_db_path(tmp_path, monkeypatch):
    """Подменяет config.DB_PATH напрямую на изолированный временный путь.

    monkeypatch.setattr на сам атрибут модуля, не на переменную окружения —
    единственный надёжный способ для db.py, см. docstring модуля.
    monkeypatch откатывает атрибут автоматически после теста.
    """
    isolated_path = tmp_path / "test_knowledge_base.db"
    monkeypatch.setattr(config, "DB_PATH", isolated_path)
    yield isolated_path


def test_get_connection_uses_config_db_path(isolated_db_path):
    """get_connection() должен физически создать файл БД по текущему
    значению config.DB_PATH, не по какому-то другому, зашитому пути."""
    assert not isolated_db_path.exists()

    with db.get_connection() as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()

    assert isolated_db_path.exists()


def test_get_connection_returns_working_connection(isolated_db_path):
    """Базовая проверка, что соединение реально рабочее — не просто
    открывается без ошибки, а позволяет писать и читать данные."""
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()

    with db.get_connection() as conn:
        row = conn.execute("SELECT x FROM t").fetchone()
        assert row == (42,)


def test_get_connection_default_timeout(isolated_db_path):
    """DEFAULT_TIMEOUT_S (10.0) должен реально долетать до sqlite3 —
    проверяем через PRAGMA busy_timeout, которое sqlite3.connect(timeout=X)
    выставляет внутри себя (в миллисекундах)."""
    with db.get_connection() as conn:
        busy_timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert busy_timeout_ms == int(db.DEFAULT_TIMEOUT_S * 1000)


def test_get_connection_respects_custom_timeout(isolated_db_path):
    """Явно переданный timeout должен переопределять DEFAULT_TIMEOUT_S,
    не игнорироваться."""
    custom_timeout = 3.5
    with db.get_connection(timeout=custom_timeout) as conn:
        busy_timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert busy_timeout_ms == int(custom_timeout * 1000)
