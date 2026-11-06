import sys
sys.path.insert(0, "C:/oko/src")
sys.path.insert(0, "C:/oko")
import db
conn = db.get_connection()
conn.execute("DELETE FROM sessions WHERE user_id =8574889260") 
conn.commit()
print("История очищена")
