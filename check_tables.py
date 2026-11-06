import sys
sys.path.insert(0, 'src')
import config
import sqlite3

conn = sqlite3.connect(str(config.DB_PATH))
rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("Tables:", rows)
conn.close()
