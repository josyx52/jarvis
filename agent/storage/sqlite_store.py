import os
import sqlite3
import threading
from typing import Dict, Any


class SQLiteStore:

    def __init__(self, db_path: str = "data/agent.db"):

        base_dir = os.path.dirname(os.path.abspath(__file__))

        db_full_path = os.path.abspath(
            os.path.join(base_dir, "..", db_path)
        )

        os.makedirs(os.path.dirname(db_full_path), exist_ok=True)

        self.conn = sqlite3.connect(
            db_full_path,
            check_same_thread=False,
            timeout=30
        )

        # WAL mode: leitores e escritores não se bloqueiam mutuamente
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.row_factory = sqlite3.Row

        # Lock único partilhado por todos que acedem a esta conexão
        self._lock = threading.Lock()

        self._init_db()

    # --------------------------------------------------
    # DATABASE INITIALIZATION
    # --------------------------------------------------

    def _init_db(self):

        cur = self.conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS send_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0
        )
        """)

        self.conn.commit()

        # migrations automáticas
        self._ensure_column("send_queue", "last_attempt", "TEXT")
        self._ensure_column("send_queue", "next_retry_at", "TEXT")
        self._ensure_column("send_queue", "last_error", "TEXT")

    # --------------------------------------------------
    # SCHEMA MIGRATIONS
    # --------------------------------------------------

    def _ensure_column(self, table: str, column: str, column_type: str):

        cur = self.conn.cursor()
        cols = cur.execute(f"PRAGMA table_info({table})").fetchall()
        col_names = [c[1] for c in cols]

        if column not in col_names:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            self.conn.commit()

    # --------------------------------------------------
    # QUEUE OPERATIONS
    # --------------------------------------------------

    def count_pending(self) -> int:
        with self._lock:
            cur = self.conn.cursor()
            row = cur.execute(
                "SELECT COUNT(*) as count FROM send_queue WHERE status='pending'"
            ).fetchone()
            return row["count"] if row else 0

    def enqueue(self, payload: Dict[str, Any], now: str):

        import json

        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
            INSERT INTO send_queue (
                created_at,
                payload,
                status,
                attempts,
                next_retry_at
            )
            VALUES (?, ?, 'pending', 0, ?)
            """, (now, json.dumps(payload), now))
            self.conn.commit()

    # --------------------------------------------------

    def get_pending(self, limit: int):

        from datetime import datetime

        now = datetime.utcnow().isoformat()

        with self._lock:
            cur = self.conn.cursor()
            rows = cur.execute("""
            SELECT id, payload, attempts
            FROM send_queue
            WHERE status='pending'
            AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY id
            LIMIT ?
            """, (now, limit)).fetchall()

        return rows

    # --------------------------------------------------

    def mark_sent(self, row_id: int):

        with self._lock:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM send_queue WHERE id = ?", (row_id,))
            self.conn.commit()

    # --------------------------------------------------

    def mark_retry(self, row_id: int, attempts: int, error: str):

        from datetime import datetime, timedelta

        delay = min(2 ** attempts, 600)

        next_retry = (
            datetime.utcnow() +
            timedelta(seconds=delay)
        ).isoformat()

        now = datetime.utcnow().isoformat()

        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
            UPDATE send_queue
            SET
                attempts = ?,
                last_attempt = ?,
                next_retry_at = ?,
                last_error = ?
            WHERE id = ?
            """, (
                attempts + 1,
                now,
                next_retry,
                error,
                row_id
            ))
            self.conn.commit()

    # --------------------------------------------------

    def close(self):
        self.conn.close()
