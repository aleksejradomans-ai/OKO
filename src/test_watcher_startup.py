"""STARTUP_SCAN (17.11): наблюдатель knowledge\\ подбирает файлы, лежащие в папке до старта; один файл -- одна
постановка в staging; повторная индексация того же содержимого не пишет вторую запись; после approve файл в archived.
Только временные папки (tmp_path); сеть (Ollama / ChromaDB) подменена; служба не запускается.
B15: детерминированное ожидание -- постановка засчитывается ПОСЛЕ переноса файла (раньше счётчик рос до rename, и
проверка файла в staging под нагрузкой опережала перенос); ожидание -- опрос условия с таймаутом 10 с, не sleep."""
import sqlite3
import threading
import time

import pytest

import run_system as rs


class FakeStaging:
    """Как StagingManager.move_to_staging: файл уходит из корня; счётчик постановок по имени."""

    def __init__(self, staging_dir):
        self.staging_dir = staging_dir
        self.calls = []
        self.lock = threading.Lock()

    def move_to_staging(self, path):
        from pathlib import Path
        p = Path(path)
        if p.exists():
            p.rename(self.staging_dir / p.name)
        with self.lock:                      # засчитывается после переноса: наблюдатель видит итог целиком
            self.calls.append(p.name)
        return "uuid-" + p.name


def _wait(cond, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture
def watch(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "DEBOUNCE_SECONDS", 0.1)
    monkeypatch.setattr(rs, "BACKUP_DIR", str(tmp_path / "backups"))
    know = tmp_path / "knowledge"
    staging = tmp_path / "staging"
    know.mkdir()
    staging.mkdir()
    (know / "archived").mkdir()
    (know / "archived" / "old_20260101_000000_000000.txt").write_text("архив", encoding="utf-8")
    fs = FakeStaging(staging)
    return know, staging, fs, rs.KnowledgeHandler(fs)


def test_startup_scan_stages_preexisting_file(watch):
    know, staging, fs, h = watch
    (know / "doc_before_start (2).PDF").write_bytes(b"%PDF-1.4 synthetic")
    (know / "notes.md").write_text("не наблюдается", encoding="utf-8")          # расширение вне списка
    n = rs._schedule_existing(h, know)
    assert n == 1
    assert _wait(lambda: fs.calls == ["doc_before_start (2).PDF"] and (staging / "doc_before_start (2).PDF").exists())
    assert (know / "notes.md").exists() and (know / "archived" / "old_20260101_000000_000000.txt").exists()


def test_startup_scan_and_event_stage_once(watch):
    know, staging, fs, h = watch
    p = know / "fresh.txt"
    p.write_text("новый файл", encoding="utf-8")
    rs._schedule_existing(h, know)                                            # стартовый проход
    h.on_created(rs._StartupEvent(str(p)))                                    # и событие watchdog того же пути
    h.on_modified(rs._StartupEvent(str(p)))
    assert _wait(lambda: len(fs.calls) >= 1)
    time.sleep(0.4)
    assert fs.calls == ["fresh.txt"]


def test_startup_scan_empty_folder(watch):
    know, staging, fs, h = watch
    assert rs._schedule_existing(h, know) == 0
    time.sleep(0.2)
    assert fs.calls == []


def test_main_calls_startup_scan_after_observer_start():
    import inspect
    src = inspect.getsource(rs.main)
    assert src.index("observer.start()") < src.index("_schedule_existing(event_handler, watch_path)")
    assert "Startup scan: %d existing file(s)" in src


# --- идемпотентность и archived после approve (StagingManager + process_single, сеть подменена) ----------------
@pytest.fixture
def K(tmp_path, monkeypatch):
    import knowledge_indexer as mod
    monkeypatch.setattr(mod, "ARCHIVE_DIR", tmp_path / "knowledge" / "archived")
    monkeypatch.setattr(mod, "get_embedding", lambda text: None)
    calls = []
    monkeypatch.setattr(mod, "index_chunks_to_rag", lambda *a, **k: calls.append(a[1]) or 1)
    mod._rag_calls = calls
    return mod


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE knowledge (id INTEGER PRIMARY KEY, filename TEXT, content TEXT NOT NULL,
                 content_hash TEXT UNIQUE, source TEXT, category TEXT, pulse_bpm INTEGER, embedding BLOB,
                 timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    yield c
    c.close()


@pytest.fixture
def real_staging(tmp_path, monkeypatch):
    import config
    from src.staging_manager import StagingManager
    from src.staging_repository import StagingRepository
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "staging.db")
    repo = StagingRepository()
    repo.setup_database()
    know = tmp_path / "knowledge"
    (know / "archived").mkdir(parents=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    return StagingManager(repo, staging_dir=staging, input_dir=know), know, staging


def test_after_approve_file_goes_to_archived(K, conn, real_staging, monkeypatch):
    sm, know, staging = real_staging
    monkeypatch.setattr(rs, "DEBOUNCE_SECONDS", 0.1)
    monkeypatch.setattr(rs, "BACKUP_DIR", str(know.parent / "backups"))
    (know / "doc.txt").write_text("Текст документа для индексации.", encoding="utf-8")
    h = rs.KnowledgeHandler(sm)
    assert rs._schedule_existing(h, know) == 1
    assert _wait(lambda: len(sm.repo.get_all_tasks()) == 1)
    uuid = next(iter(sm.repo.get_all_tasks()))
    path = sm.approve(uuid)
    assert K.process_single(path, conn) == "processed"
    assert not (know / "doc.txt").exists() and not (staging / "doc.txt").exists()
    assert len(list((know / "archived").glob("doc_*.txt"))) == 1
    assert conn.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 1 and K._rag_calls == ["doc.txt"]


def test_same_content_not_indexed_twice(K, conn, tmp_path):
    know = tmp_path / "knowledge"
    (know / "archived").mkdir(parents=True)
    for name in ("a.txt", "a (2).txt"):
        p = know / name
        p.write_text("Один и тот же текст.", encoding="utf-8")
        K.process_single(p, conn)
    assert conn.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 1
    assert K._rag_calls == ["a.txt"]                                          # дубль в ChromaDB не пишется


@pytest.mark.xfail(strict=True, reason="по коду дубль определяется только по content_hash текста в knowledge_base.db; "
                   "копия того же файла в archived без записи БД (сирота) повторную индексацию не блокирует")
def test_archived_copy_without_db_row_blocks_reindex(K, conn, tmp_path):
    know = tmp_path / "knowledge"
    (know / "archived").mkdir(parents=True)
    (know / "archived" / "b_20260101_000000_000000.txt").write_text("Текст из архива.", encoding="utf-8")
    p = know / "b.txt"
    p.write_text("Текст из архива.", encoding="utf-8")
    assert K.process_single(p, conn) == "duplicate"
