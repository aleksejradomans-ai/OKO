"""R1, шаг 1: закрыть хвосты миграции и копии журнала. Сухой прогон по умолчанию."""
import sqlite3, sys
from pathlib import Path
APPLY = "--apply" in sys.argv
DATA = Path(r"C:\oko\data")
MEM = DATA / "memory"

def wipe_sqlite_table(db, table):
    if not db.exists():
        return
    c = sqlite3.connect(db)
    has = c.execute("select count(*) from sqlite_master where type='table' and name=?", (table,)).fetchone()[0]
    rows = c.execute(f"select count(*) from {table}").fetchone()[0] if has else 0
    print(f"{db.name}: таблица {table} - {'есть' if has else 'нет'}, строк {rows}")
    if APPLY and has:
        c.execute(f"DROP TABLE {table}")
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
        print(f"  удалена + wal_checkpoint + VACUUM")
    c.close()

wipe_sqlite_table(DATA / "knowledge_base.db", "sessions")

files = sorted(list((MEM / "backups").glob("memory_*.db")) +
               list(MEM.glob("memory_pre_purge_*.db")) +
               list(MEM.glob("quarantine_loop*.json")))
for f in files:
    print(f"{'удалён' if APPLY else 'будет удалён'}: {f.relative_to(DATA)} ({f.stat().st_size} байт)")
    if APPLY:
        f.unlink()
if not APPLY:
    print("\nСухой прогон. Для выполнения: --apply (при ОСТАНОВЛЕННОЙ службе OkoBot)")