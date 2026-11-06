import sys
sys.path.insert(0, "C:/oko/src")
sys.path.insert(0, "C:/oko")
import db

conn = db.get_connection()
row = conn.execute(
    "SELECT content FROM knowledge WHERE filename=? ORDER BY timestamp DESC LIMIT 1",
    ("test1.pdf",),
).fetchone()
text = row[0] if row else ""
print(repr(text))
