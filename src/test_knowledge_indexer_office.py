"""OFFICE_INGEST (28.10.2026): .docx/.xlsx проходят process_single как PDF — текст в knowledge, файл в архив;
мусор с расширением .docx уходит в archived/rejected, а не роняет обработку. Сеть (Ollama/ChromaDB) подменена."""
import sqlite3

import pytest

docx = pytest.importorskip("docx")
openpyxl = pytest.importorskip("openpyxl")


@pytest.fixture
def K(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake_token_for_tests")
    monkeypatch.setenv("AI_BASE_DIR", str(tmp_path))
    import knowledge_indexer as mod
    monkeypatch.setattr(mod, "ARCHIVE_DIR", tmp_path / "archived")
    monkeypatch.setattr(mod, "get_embedding", lambda text: None)
    monkeypatch.setattr(mod, "index_chunks_to_rag", lambda *a, **k: 0)
    return mod


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE knowledge (id INTEGER PRIMARY KEY, filename TEXT, content TEXT NOT NULL,
                 content_hash TEXT UNIQUE, source TEXT, category TEXT, pulse_bpm INTEGER, embedding BLOB,
                 timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    yield c
    c.close()


def test_office_extensions_allowed(K):
    assert {".docx", ".xlsx"} <= K.ALLOWED_EXTENSIONS
    assert K.OFFICE_AVAILABLE is True


def test_docx_indexed_and_archived(K, conn, tmp_path):
    p = tmp_path / "letter.docx"
    d = docx.Document()
    d.add_paragraph("Valsts ieņēmumu dienests")
    d.add_paragraph("Lūdzu apstipriniet apliecinajumus")
    d.save(p)
    assert K.process_single(p, conn) == "processed"
    content = conn.execute("SELECT content FROM knowledge WHERE filename='letter.docx'").fetchone()[0]
    assert content == "Valsts ieņēmumu dienests\nLūdzu apstipriniet apliecinajumus"
    assert not p.exists() and list((tmp_path / "archived").glob("letter_*.docx"))


def test_xlsx_indexed(K, conn, tmp_path):
    p = tmp_path / "t.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["Дата", "Сумма"])
    wb.save(p)
    assert K.process_single(p, conn) == "processed"
    assert "Дата\tСумма" in conn.execute("SELECT content FROM knowledge").fetchone()[0]


def test_garbage_docx_rejected_not_crash(K, conn, tmp_path):
    p = tmp_path / "fake.docx"
    p.write_bytes(b"not a zip at all")
    assert K.process_single(p, conn) == "unreadable"
    assert conn.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 0
    assert list((tmp_path / "archived" / "rejected").glob("fake_*.docx"))


def test_empty_docx_rejected_as_empty(K, conn, tmp_path):
    p = tmp_path / "blank.docx"
    docx.Document().save(p)
    assert K.process_single(p, conn) == "empty"
