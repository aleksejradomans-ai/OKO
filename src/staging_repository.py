from contextlib import closing
from typing import Dict, Any, Optional
import db


class StagingRepository:
    def setup_database(self):
        """Идемпотентная инициализация таблицы pending_tasks."""
        with closing(db.get_connection()) as conn:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pending_tasks (
                        uuid TEXT PRIMARY KEY,
                        type TEXT NOT NULL,
                        original_path TEXT NOT NULL,
                        staging_path TEXT NOT NULL,
                        hash TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        category TEXT
                    );
                """)

    def add_task(self, uuid: str, data: Dict[str, Any]):
        with closing(db.get_connection()) as conn:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO pending_tasks 
                    (uuid, type, original_path, staging_path, hash, timestamp, category)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    uuid, data['type'], data['original_path'],
                    data['staging_path'], data['hash'], data['timestamp'],
                    data.get('category')
                ))

    def update_category(self, uuid: str, category: str):
        with closing(db.get_connection()) as conn:
            with conn:
                conn.execute("UPDATE pending_tasks SET category = ? WHERE uuid = ?", (category, uuid))

    def delete_task(self, uuid: str):
        with closing(db.get_connection()) as conn:
            with conn:
                conn.execute("DELETE FROM pending_tasks WHERE uuid = ?", (uuid,))

    def get_task(self, uuid: str) -> Optional[Dict[str, Any]]:
        with closing(db.get_connection()) as conn:
            cursor = conn.execute("SELECT * FROM pending_tasks WHERE uuid = ?", (uuid,))
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "type": row[1], "original_path": row[2],
                "staging_path": row[3], "hash": row[4],
                "timestamp": row[5], "category": row[6]
            }

    def get_all_tasks(self) -> Dict[str, Dict[str, Any]]:
        with closing(db.get_connection()) as conn:
            cursor = conn.execute("SELECT * FROM pending_tasks")
            return {
                row[0]: {
                    "type": row[1], "original_path": row[2],
                    "staging_path": row[3], "hash": row[4],
                    "timestamp": row[5], "category": row[6]
                } for row in cursor.fetchall()
            }
