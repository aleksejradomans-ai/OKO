import sys
sys.path.insert(0, 'src')
import config
import sqlite3

conn = sqlite3.connect(str(config.DB_PATH))
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, filename, category, pulse_bpm, "
    "embedding IS NOT NULL AS has_embedding, timestamp "
    "FROM knowledge ORDER BY id DESC LIMIT 10"
).fetchall()

if not rows:
    print("Таблица knowledge пуста.")
else:
    for r in rows:
        print(dict(r))

conn.close()
