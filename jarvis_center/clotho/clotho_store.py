"""
ClothoStore — persistência PostgreSQL para integrações do Jarvis Fates Engine (Clotho).
"""

import os
import json
import psycopg2
import psycopg2.extras


def _conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


class ClothoStore:

    def list_integrations(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT id, name, type, host, port, auth_type, notes,
                       analysis, status, created_by, created_at, updated_at,
                       (config IS NOT NULL AND config != '{}') AS has_credentials
                FROM clotho_integrations
                ORDER BY created_at DESC
                """
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_integration(self, integration_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM clotho_integrations WHERE id = %s",
                (integration_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_integration(self, data: dict) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO clotho_integrations (
                    name, host, port, auth_type, config, notes,
                    status, created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, name, type, host, port, auth_type, config, notes,
                          analysis, status, created_by, created_at, updated_at
                """,
                (
                    data["name"],
                    data.get("host"),
                    data.get("port"),
                    data.get("auth_type"),
                    json.dumps(data.get("config") or {}),
                    data.get("notes"),
                    data.get("status", "draft"),
                    data.get("created_by"),
                ),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def set_analysis(self, integration_id: int, type_: str, analysis: str) -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE clotho_integrations
                SET type = %s, analysis = %s, status = 'configured', updated_at = NOW()
                WHERE id = %s
                RETURNING id, name, type, host, port, auth_type, config, notes,
                          analysis, status, created_by, created_at, updated_at
                """,
                (type_, analysis, integration_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_tools(self, integration_id: int, tools: dict) -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE clotho_integrations
                SET tools = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING id, name, type, host, port, auth_type, config, notes,
                          analysis, tools, status, created_by, created_at, updated_at
                """,
                (json.dumps(tools), integration_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def delete_integration(self, integration_id: int) -> bool:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM clotho_integrations WHERE id = %s",
                (integration_id,),
            )
            return cur.rowcount > 0
        finally:
            conn.close()
