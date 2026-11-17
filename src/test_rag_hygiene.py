"""M7.4: rag_resync.hygiene_counts — только числа, без имён/хэшей, без записи."""
import json

import rag_resync


def test_hygiene_counts_numbers_only(monkeypatch):
    missing = [{"id": 1, "filename": "secret_plan.pdf", "hash": "aaaa"}]
    partial = [{"id": 2, "filename": "b.txt", "hash": "bbbb"}, {"id": 3, "filename": "c", "hash": "c"}]
    orphans = [{"hash": "dddd", "chunks": 4, "sources": {"gone.txt"}, "no_category": 0},
               {"hash": "eeee", "chunks": 1, "sources": set(), "no_category": 1}]
    calls = []
    monkeypatch.setattr(rag_resync, "scan", lambda c, k: (calls.append("scan"), (missing, partial, 7))[1])
    monkeypatch.setattr(rag_resync, "scan_orphans",
                        lambda c, k: (calls.append("orph"), (orphans, 3))[1])
    out = rag_resync.hygiene_counts(object(), object())
    assert out == {"rows": 7, "missing": 1, "partial": 2, "orphans": 2,
                   "orphan_chunks": 5, "no_hash": 3}
    assert calls == ["scan", "orph"]
    blob = json.dumps(out)
    assert "secret_plan" not in blob and "dddd" not in blob and "gone.txt" not in blob


def test_hygiene_counts_empty(monkeypatch):
    monkeypatch.setattr(rag_resync, "scan", lambda c, k: ([], [], 0))
    monkeypatch.setattr(rag_resync, "scan_orphans", lambda c, k: ([], 0))
    assert rag_resync.hygiene_counts(None, None) == {
        "rows": 0, "missing": 0, "partial": 0, "orphans": 0, "orphan_chunks": 0, "no_hash": 0}
