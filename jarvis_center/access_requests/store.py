"""
AccessRequestsStore — persistência dos "tipos de pedido" definidos pelo
admin (access_request_types) e dos pedidos concretos (access_requests).

Ver access_requests/service.py para a execução: a acção de cada tipo de
pedido é sempre uma tool Clotho já criada/testada no Laboratório, nunca
código Python novo por tipo (ver storage/schema_init.sql para o porquê).
"""

import os

import psycopg2
import psycopg2.extras


def _conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


class AccessRequestsStore:

    # ── Tipos de pedido ──────────────────────────────────────────────────

    def create_type(
        self, slug: str, name: str, description: str | None,
        integration_name: str, tool_name: str, target_param: str,
        result_field: str | None, requires_approval: bool, created_by: str,
    ) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO access_request_types (
                    slug, name, description, integration_name, tool_name,
                    target_param, result_field, requires_approval, created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (slug, name, description, integration_name, tool_name,
                 target_param, result_field, requires_approval, created_by),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def list_types(self, active_only: bool = True) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            query = "SELECT * FROM access_request_types"
            if active_only:
                query += " WHERE active = TRUE"
            query += " ORDER BY name"
            cur.execute(query)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_type(self, type_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM access_request_types WHERE id = %s", (type_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_type_active(self, type_id: int, active: bool) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("UPDATE access_request_types SET active = %s WHERE id = %s", (active, type_id))
        finally:
            conn.close()

    # ── Pedidos ──────────────────────────────────────────────────────────

    def create_request(
        self, request_type_id: int, requested_by: str, target: str,
        requester_user_id: str | None = None, chat_id: str | None = None,
        message_id: str | None = None,
    ) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO access_requests (
                    request_type_id, requested_by, target, status,
                    requester_user_id, chat_id, message_id
                ) VALUES (%s,%s,%s,'pending_approval',%s,%s,%s)
                RETURNING *
                """,
                (request_type_id, requested_by, target, requester_user_id, chat_id, message_id),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def mark_processing(self, request_id: int, approved_by: str) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE access_requests
                SET status = 'processing', approved_by = %s, approved_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (approved_by, request_id),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def finish(self, request_id: int, status: str, error_message: str | None, approved_by: str) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE access_requests
                SET status = %s, error_message = %s, approved_by = %s, approved_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (status, error_message, approved_by, request_id),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def deny(self, request_id: int, denied_by: str, reason: str | None) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE access_requests
                SET status = 'denied', error_message = %s, approved_by = %s, approved_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (reason, denied_by, request_id),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def mark_delivered(self, request_id: int) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("UPDATE access_requests SET delivered = TRUE WHERE id = %s", (request_id,))
        finally:
            conn.close()

    def get(self, request_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM access_requests WHERE id = %s", (request_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_pending(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT ar.*, t.name AS type_name, t.slug AS type_slug
                FROM access_requests ar
                JOIN access_request_types t ON t.id = ar.request_type_id
                WHERE ar.status = 'pending_approval'
                ORDER BY ar.created_at ASC
                """
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def count_pending(self) -> int:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM access_requests WHERE status = 'pending_approval'")
            return cur.fetchone()[0]
        finally:
            conn.close()
