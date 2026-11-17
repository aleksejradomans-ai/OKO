"""test_knowledge_indexer_logic.py — логика knowledge_indexer без транспорта.

Закрывает пробелы аудита 27.10.2026:
  1. classify_content: порядок CATEGORY_PRIORITY, fallback, регистр.
  2. override_category в process_single: перекрывает классификатор.
  3. Дедуп по content_hash: тот же контент = duplicate, то же имя с другим
     контентом = не дубль; дубль не уходит в RAG повторно.
  4. Миграция setup_database: ALTER TABLE content_hash на старой схеме.
  5. ALLOWED_EXTENSIONS / index_all_files: .db, бинарники и поддиректории
     не сканируются.

Изоляция (ENGINE ISOLATION): Ollama и ChromaDB подменены. Сеть запрещена:
любой requests.post -> AssertionError. index_chunks_to_rag заменён
регистратором, поэтому боевая коллекция oko_rag не трогается.
monkeypatch.setattr(raising=True) по умолчанию: если имя в модуле
переименовано, тест падает, а не пишет в боевое хранилище.

Имя файла содержит "knowledge_indexer", поэтому `pytest -k knowledge_indexer`
выбирает этот модуль без регистрации маркеров в pytest.ini.

xfail(strict=True) — ЗАФИКСИРОВАННЫЕ ДЕФЕКТЫ подстрочного классификатора
[ПРЕДПОЛОЖЕНИЕ до прогона]. Ожидание в тесте = желаемое поведение.
Если классификатор исправят, strict-XPASS уронит прогон: снять xfail.
"""
from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------
# Фикстуры
# --------------------------------------------------------------------------

@pytest.fixture
def ki(tmp_path, monkeypatch):
    """knowledge_indexer, изолированный от боевых БД, Chroma и сети."""
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake_token_for_tests")
    import knowledge_indexer as mod

    db_file = tmp_path / "kb_test.db"
    input_dir = tmp_path / "knowledge"
    archive_dir = input_dir / "archived"
    input_dir.mkdir()

    def _connect(*_args, **_kwargs):
        return sqlite3.connect(db_file)

    if hasattr(mod, "db"):
        monkeypatch.setattr(mod.db, "get_connection", _connect)
    if hasattr(mod, "get_connection"):
        monkeypatch.setattr(mod, "get_connection", _connect)

    monkeypatch.setattr(mod, "INPUT_DIR", input_dir)
    monkeypatch.setattr(mod, "ARCHIVE_DIR", archive_dir)

    def _no_network(*_args, **_kwargs):
        raise AssertionError("сетевой вызов в юнит-тесте knowledge_indexer")

    if hasattr(mod, "requests"):
        monkeypatch.setattr(mod.requests, "post", _no_network)
    monkeypatch.setattr(mod, "_request_embedding_vector", lambda *_a, **_k: None)

    rag_calls: list[tuple] = []

    def _fake_rag(*args, **kwargs):
        rag_calls.append((*args, *kwargs.values()))
        return 1

    monkeypatch.setattr(mod, "index_chunks_to_rag", _fake_rag)

    return SimpleNamespace(
        mod=mod,
        db_file=db_file,
        input=input_dir,
        archive=archive_dir,
        rag_calls=rag_calls,
    )


@pytest.fixture
def kdb(ki):
    """ki + свежая схема через боевой setup_database()."""
    ki.mod.setup_database()
    return ki


def _ingest(env, name: str, text: str, override: str | None = None) -> str:
    path = env.input / name
    path.write_text(text, encoding="utf-8")
    conn = sqlite3.connect(env.db_file)
    try:
        if override is None:
            return env.mod.process_single(path, conn)
        return env.mod.process_single(path, conn, override_category=override)
    finally:
        conn.close()


def _rows(env) -> list[tuple]:
    conn = sqlite3.connect(env.db_file)
    try:
        return conn.execute(
            "SELECT filename, category, content_hash FROM knowledge ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


TEXT_TROUBLE = "Traceback (most recent call last): сервис упал, error при старте."
TEXT_PLAIN = "Погода в Риге сегодня облачная, ветер слабый, без осадков."


# --------------------------------------------------------------------------
# 1. classify_content
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("filename", "text", "expected"),
    [
        ("pulse_realtime.txt", "обычный текст", "biometric_log"),
        ("PULSE_REALTIME.TXT", "обычный текст", "biometric_log"),
        ("log.txt", "PULSE: 96 BPM\nTraceback (most recent call last)", "biometric_log"),
        ("notes.txt", "Traceback (most recent call last)\nimport os", "troubleshooting"),
        ("script.py", "print('hi')", "code_context"),
        ("notes.txt", "def build(): план проекта", "code_context"),
        ("notes.txt", "Идея для АРХИТЕКТУРЫ", "project_notes"),
        ("notes.txt", TEXT_PLAIN, "general_knowledge"),
        ("notes.txt", "", "general_knowledge"),
    ],
    ids=[
        "name_marker",
        "name_marker_case",
        "biometric_beats_trouble",
        "trouble_beats_code",
        "code_by_extension",
        "code_beats_notes",
        "notes_case_insensitive",
        "fallback_default",
        "empty_text_default",
    ],
)
def test_classify_priority(ki, filename, text, expected):
    assert ki.mod.classify_content(text, filename) == expected


@pytest.mark.parametrize(
    ("filename", "text", "expected"),
    [
        pytest.param(
            "worker.py",
            "try:\n    run()\nexcept ValueError:\n    pass\n",
            "code_context",
            marks=pytest.mark.xfail(
                strict=True,
                reason="'error' в ValueError -> troubleshooting: почти любой "
                       ".py с обработкой исключений не попадает в code_context",
            ),
            id="py_with_except_is_not_code",
        ),
        pytest.param(
            "process.txt",
            "Диаграмма BPMN для процесса закупок",
            "general_knowledge",
            marks=pytest.mark.xfail(
                strict=True,
                reason="подстрока 'BPM' в 'BPMN' -> biometric_log",
            ),
            id="bpmn_is_biometric",
        ),
        pytest.param(
            "astro.txt",
            "Планета Марс находится дальше Земли",
            "general_knowledge",
            marks=pytest.mark.xfail(
                strict=True,
                reason="подстрока 'план' в 'планета' -> project_notes",
            ),
            id="planeta_is_project_notes",
        ),
    ],
)
def test_classify_known_substring_defects(ki, filename, text, expected):
    assert ki.mod.classify_content(text, filename) == expected


def test_priority_list_matches_pattern_map(ki):
    """Категория вне CATEGORY_PRIORITY мертва; лишняя в списке -> KeyError."""
    mod = ki.mod
    assert set(mod.CATEGORY_PRIORITY) == set(mod.PATTERN_MAP)
    assert len(mod.CATEGORY_PRIORITY) == len(set(mod.CATEGORY_PRIORITY))
    assert mod.DEFAULT_CATEGORY not in mod.PATTERN_MAP


# --------------------------------------------------------------------------
# 2. override_category
# --------------------------------------------------------------------------

def test_override_beats_classifier(kdb):
    status = _ingest(kdb, "crash.txt", TEXT_TROUBLE, override="project_notes")
    assert status == "processed"
    assert [r[1] for r in _rows(kdb)] == ["project_notes"]
    assert len(kdb.rag_calls) == 1
    assert "project_notes" in kdb.rag_calls[0]
    assert "troubleshooting" not in kdb.rag_calls[0]


def test_no_override_uses_classifier(kdb):
    assert _ingest(kdb, "crash.txt", TEXT_TROUBLE) == "processed"
    assert [r[1] for r in _rows(kdb)] == ["troubleshooting"]


def test_empty_override_falls_back_to_classifier(kdb):
    """Контракт `if override_category:` — пустая строка = нет выбора."""
    assert _ingest(kdb, "crash.txt", TEXT_TROUBLE, override="") == "processed"
    assert [r[1] for r in _rows(kdb)] == ["troubleshooting"]


# --------------------------------------------------------------------------
# 3. Дедуп по content_hash
# --------------------------------------------------------------------------

def test_same_content_other_name_is_duplicate(kdb):
    assert _ingest(kdb, "a.txt", TEXT_PLAIN) == "processed"
    assert _ingest(kdb, "b.txt", TEXT_PLAIN) == "duplicate"

    rows = _rows(kdb)
    assert len(rows) == 1
    assert rows[0][0] == "a.txt"
    assert rows[0][2] == hashlib.sha256(TEXT_PLAIN.encode("utf-8")).hexdigest()
    # дубль не чанкуется повторно
    assert len(kdb.rag_calls) == 1
    # оба файла ушли из входа (дубль тоже архивируется, не висит)
    assert not (kdb.input / "a.txt").exists()
    assert not (kdb.input / "b.txt").exists()


def test_same_name_other_content_is_not_duplicate(kdb):
    assert _ingest(kdb, "pulse_realtime.txt", "PULSE: 70 BPM") == "processed"
    assert _ingest(kdb, "pulse_realtime.txt", "PULSE: 96 BPM") == "processed"

    rows = _rows(kdb)
    assert len(rows) == 2
    assert rows[0][2] != rows[1][2]
    assert len(kdb.rag_calls) == 2


def test_duplicate_ignores_new_override(kdb):
    """Повторная загрузка с другой кнопкой категории не меняет первую запись."""
    assert _ingest(kdb, "a.txt", TEXT_TROUBLE) == "processed"
    assert _ingest(kdb, "b.txt", TEXT_TROUBLE, override="project_notes") == "duplicate"
    assert [r[1] for r in _rows(kdb)] == ["troubleshooting"]


def test_unique_index_blocks_raw_duplicate(kdb):
    conn = sqlite3.connect(kdb.db_file)
    try:
        sql = ("INSERT INTO knowledge (content, content_hash, source, category) "
               "VALUES (?, ?, 'file', 'general_knowledge')")
        conn.execute(sql, ("x", "h1"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, ("y", "h1"))
        # несколько NULL в UNIQUE-индексе SQLite допустимы (легаси-строки)
        conn.execute(sql, ("z1", None))
        conn.execute(sql, ("z2", None))
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 4. Миграция setup_database
# --------------------------------------------------------------------------

def test_migration_adds_content_hash_to_old_schema(ki):
    conn = sqlite3.connect(ki.db_file)
    conn.execute("""
        CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            pulse_bpm INTEGER,
            embedding BLOB,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO knowledge (filename, content, source, category) "
        "VALUES ('old.txt', 'старая запись', 'file', 'general_knowledge')"
    )
    conn.commit()
    conn.close()

    ki.mod.setup_database()
    ki.mod.setup_database()  # идемпотентность: второй ALTER не должен падать

    conn = sqlite3.connect(ki.db_file)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge)")}
        assert "content_hash" in cols

        unique_hash_index = False
        for _seq, name, unique, *_ in conn.execute("PRAGMA index_list(knowledge)"):
            idx_cols = [r[2] for r in conn.execute(f"PRAGMA index_info('{name}')")]
            if unique and idx_cols == ["content_hash"]:
                unique_hash_index = True
        assert unique_hash_index

        old = conn.execute(
            "SELECT filename, content, content_hash FROM knowledge"
        ).fetchall()
        assert old == [("old.txt", "старая запись", None)]
    finally:
        conn.close()


def test_migrated_db_accepts_new_ingest(ki):
    """После миграции process_single пишет рядом с легаси-строкой."""
    conn = sqlite3.connect(ki.db_file)
    conn.execute("""
        CREATE TABLE knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT, content TEXT NOT NULL, source TEXT NOT NULL,
            category TEXT NOT NULL, pulse_bpm INTEGER, embedding BLOB,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

    ki.mod.setup_database()
    assert _ingest(ki, "new.txt", TEXT_PLAIN) == "processed"
    assert len(_rows(ki)) == 1


# --------------------------------------------------------------------------
# 5. ALLOWED_EXTENSIONS / index_all_files
# --------------------------------------------------------------------------

def test_allowed_extensions_shape(ki):
    exts = ki.mod.ALLOWED_EXTENSIONS
    assert ".db" not in exts
    assert all(e.startswith(".") and e == e.lower() for e in exts)


def test_index_all_files_skips_db_binaries_and_subdirs(kdb):
    (kdb.input / "note.txt").write_text(TEXT_PLAIN, encoding="utf-8")
    (kdb.input / "knowledge_base.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    (kdb.input / "tool.exe").write_bytes(b"MZ" + b"\x00" * 64)
    sub = kdb.input / "nested"
    sub.mkdir()
    (sub / "inner.txt").write_text("вложенный файл не сканируется", encoding="utf-8")

    kdb.mod.index_all_files()

    assert not (kdb.input / "note.txt").exists()
    assert (kdb.input / "knowledge_base.db").exists()
    assert (kdb.input / "tool.exe").exists()
    assert (sub / "inner.txt").exists()
    assert [r[0] for r in _rows(kdb)] == ["note.txt"]
