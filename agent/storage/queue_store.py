import json
from datetime import datetime

from agent_core.config import MAX_RETRIES

MAX_QUEUE_SIZE = 5000  # descarta frames antigos se ultrapassar este limite


class QueueStore:

    def __init__(self, store):
        self.store = store
        self.conn = store.conn
        # Partilha o mesmo lock que SQLiteStore para serializar todo o acesso
        self.lock = store._lock

    def enqueue(self, payload: dict):

        now = datetime.utcnow().isoformat()

        with self.lock:
            try:
                cur = self.conn.cursor()

                # Limite de queue: apagar os mais antigos se necessário
                count = cur.execute(
                    "SELECT COUNT(*) FROM send_queue WHERE status='pending'"
                ).fetchone()[0]

                if count >= MAX_QUEUE_SIZE:
                    excess = count - MAX_QUEUE_SIZE + 1
                    cur.execute("""
                        DELETE FROM send_queue WHERE id IN (
                            SELECT id FROM send_queue
                            WHERE status='pending'
                            ORDER BY id ASC
                            LIMIT ?
                        )
                    """, (excess,))

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

            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise

    def get_pending(self, limit: int = 50):

        now = datetime.utcnow().isoformat()

        with self.lock:
            cur = self.conn.cursor()
            rows = cur.execute("""
            SELECT id, payload, attempts
            FROM send_queue
            WHERE status='pending'
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY id ASC
            LIMIT ?
            """, (now, limit)).fetchall()

        return rows

    def mark_sent(self, ids):

        with self.lock:
            try:
                cur = self.conn.cursor()
                cur.executemany(
                    "UPDATE send_queue SET status='sent' WHERE id=?",
                    [(i,) for i in ids]
                )
                self.conn.commit()
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise

    def mark_retry(self, ids, attempts_map, error):

        with self.lock:
            try:
                cur = self.conn.cursor()

                for i in ids:

                    attempts = attempts_map.get(i, 0) + 1

                    if attempts >= MAX_RETRIES:
                        cur.execute(
                            "UPDATE send_queue SET status='failed' WHERE id=?",
                            (i,)
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE send_queue
                            SET attempts=?, next_retry_at=datetime('now','+5 seconds')
                            WHERE id=?
                            """,
                            (attempts, i)
                        )

                self.conn.commit()

            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise
