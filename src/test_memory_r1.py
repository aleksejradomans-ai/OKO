"""R1: забывание и TTL стирают данные физически и не трогают лишнего."""
import time

from src.memory_core import Envelope, MemoryCore


class _NullIndex:
    def upsert(self, *a): pass
    def delete_subject(self, *a): pass
    def search(self, *a): return []


SECRET = "секретная-фраза-r1-test"


def _core(tmp_path):
    return MemoryCore(tmp_path / "m.db", _NullIndex(), lambda t: (t, []),
                      backup_dir=tmp_path / "backups")


def _all_bytes(tmp_path):
    return b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


def test_forget_wipes_journal_wal_and_snapshots(tmp_path):
    core = _core(tmp_path)
    env = Envelope("telegram", "1", SECRET)
    core.record_exchange(env, SECRET, "ответ")
    core.snapshot()
    sid = core.resolve("telegram", "1")
    assert core.forget(sid) == 2
    assert SECRET.encode("utf-8") not in _all_bytes(tmp_path)
    assert len(list((tmp_path / "backups").glob("memory_*.db"))) == 1   # один чистый


def test_ttl_removes_only_old(tmp_path):
    core = _core(tmp_path)
    old = time.time() - 40 * 86400
    sid = core.resolve("telegram", "1")
    core.import_legacy("telegram", "1", [("user", SECRET, old), ("assistant", "старый", old)])
    core.record_exchange(Envelope("telegram", "1", "новый"), "новый", "свежий ответ")
    assert core.apply_ttl(30) == 2
    assert [c for _, c in core.history(sid)] == ["новый", "свежий ответ"]
    assert SECRET.encode("utf-8") not in _all_bytes(tmp_path)


def test_ttl_noop_keeps_snapshots(tmp_path):
    core = _core(tmp_path)
    core.record_exchange(Envelope("telegram", "1", "q"), "q", "a")
    snap = core.snapshot()
    assert core.apply_ttl(30) == 0
    assert snap.exists()
