"""doc_intake (28.10.2026): файл из Telegram ждёт выбора в incoming/, дубликаты видны ДО выбора,
«в базу» = копия в knowledge/ (watcher видит создание), удаление убирает и картинки страниц."""
import os
import sqlite3
import time

import pytest

docx = pytest.importorskip("docx")

import doc_intake as I


def _docx(path, *pars):
    d = docx.Document()
    for p in pars:
        d.add_paragraph(p)
    d.save(path)
    return path


def test_new_find_display(tmp_path):
    doc_id, p = I.new_path("Отчёт 1.docx", tmp_path)
    p.write_bytes(b"x")
    assert len(doc_id) == 8 and I.find(doc_id, tmp_path) == p
    assert I.display_name(p) == "Отчёт 1.docx"
    assert I.find("zzzzzzzz", tmp_path) is None and I.find("../x", tmp_path) is None and I.find("", tmp_path) is None


def test_to_knowledge_copies_and_dedups_name(tmp_path):
    inc, kn = tmp_path / "in", tmp_path / "kn"
    kn.mkdir()
    (kn / "a.docx").write_bytes(b"old")
    _, p = I.new_path("a.docx", inc)
    p.write_bytes(b"new")
    dest = I.to_knowledge(p, kn)
    assert dest.name == "a (2).docx" and dest.read_bytes() == b"new" and not p.exists()
    assert (kn / "a.docx").read_bytes() == b"old"


def test_remove_deletes_pages(tmp_path):
    _, p = I.new_path("a.docx", tmp_path)
    p.write_bytes(b"x")
    pd = I.pages_dir(p)
    pd.mkdir(parents=True)
    (pd / "a_p1.jpg").write_bytes(b"j")
    I.remove(p)
    assert not p.exists() and not pd.exists()


def test_purge_old(tmp_path):
    _, old = I.new_path("old.docx", tmp_path)
    _, new = I.new_path("new.docx", tmp_path)
    old.write_bytes(b"o")
    new.write_bytes(b"n")
    t = time.time() - I.MAX_AGE_S - 60
    os.utime(old, (t, t))
    assert I.purge_old(tmp_path) == 1 and not old.exists() and new.exists()


def _db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE knowledge (id INTEGER PRIMARY KEY, filename TEXT, content TEXT NOT NULL, "
              "content_hash TEXT UNIQUE, source TEXT, category TEXT, pulse_bpm INTEGER, embedding BLOB, "
              "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")
    c.commit()
    return c


def test_duplicate_detected_with_indexer_rules(tmp_path, monkeypatch):
    """Хэш doc_intake == хэш, который реально пишет process_single: индексируем один файл
    настоящим process_single, затем ту же копию проверяем doc_intake.known_in_knowledge."""
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake_token_for_tests")
    import knowledge_indexer as K
    monkeypatch.setattr(K, "ARCHIVE_DIR", tmp_path / "archived")
    monkeypatch.setattr(K, "get_embedding", lambda text: None)
    monkeypatch.setattr(K, "index_chunks_to_rag", lambda *a, **k: 0)
    dbp = tmp_path / "k.db"
    conn = _db(dbp)
    src = _docx(tmp_path / "letter.docx", "Valsts ieņēmumu dienests", "Ar cieņu")
    copy = tmp_path / "copy.docx"
    copy.write_bytes(src.read_bytes())
    other = _docx(tmp_path / "other.docx", "Совсем другой текст")
    assert K.process_single(src, conn) == "processed"
    conn.close()
    connect = lambda: sqlite3.connect(dbp)
    assert I.known_in_knowledge(copy, connect) is True
    assert I.known_in_knowledge(other, connect) is False


def test_unreadable_is_unknown_not_crash(tmp_path):
    p = tmp_path / "bad.docx"
    p.write_bytes(b"zzz")
    assert I.known_in_knowledge(p, lambda: sqlite3.connect(":memory:")) is None
