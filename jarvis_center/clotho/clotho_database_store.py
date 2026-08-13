"""
ClothoDatabaseStore — persistência PostgreSQL para o Conector de Bases de Dados (Clotho).
"""

import os
import json
import psycopg2
import psycopg2.extras

from clotho.clotho_database_engine import slugify_name


def _conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


class ClothoDatabaseStore:

    def list_databases(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT id, name, source, engine, host, port, db_name, table_name,
                       schema_cache, notes, status, created_by, created_at, updated_at,
                       (config IS NOT NULL AND config != '{}') AS has_credentials
                FROM clotho_databases
                ORDER BY created_at DESC
                """
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_database(self, database_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM clotho_databases WHERE id = %s", (database_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_database_by_name(self, name: str) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM clotho_databases WHERE name ILIKE %s OR table_name ILIKE %s",
                (name, name),
            )
            row = cur.fetchone()
            if row:
                return dict(row)

            # Fallback: $db.nome$ usa um slug sem espaços (ver clotho_mentions),
            # que pode não corresponder directamente a name/table_name.
            cur.execute("SELECT * FROM clotho_databases")
            for row in cur.fetchall():
                if slugify_name(row["table_name"] or row["name"]).lower() == name.lower():
                    return dict(row)
            return None
        finally:
            conn.close()

    def create_database(self, data: dict) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO clotho_databases (
                    name, source, engine, host, port, db_name, table_name,
                    config, notes, status, created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, name, source, engine, host, port, db_name, table_name,
                          schema_cache, notes, status, created_by, created_at, updated_at
                """,
                (
                    data["name"],
                    data.get("source", "connection"),
                    data.get("engine", "postgres"),
                    data.get("host"),
                    data.get("port"),
                    data.get("db_name"),
                    data.get("table_name"),
                    json.dumps(data.get("config") or {}),
                    data.get("notes"),
                    data.get("status", "draft"),
                    data.get("created_by"),
                ),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def update_database(self, database_id: int, data: dict) -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            fields, params = [], []
            for key in ("name", "engine", "host", "port", "db_name", "table_name", "notes"):
                if key in data:
                    fields.append(f"{key} = %s")
                    params.append(data[key])
            if "config" in data:
                fields.append("config = %s")
                params.append(json.dumps(data["config"] or {}))
            if not fields:
                return self.get_database(database_id)
            fields.append("updated_at = NOW()")
            params.append(database_id)
            cur.execute(
                f"""
                UPDATE clotho_databases SET {', '.join(fields)}
                WHERE id = %s
                RETURNING id, name, source, engine, host, port, db_name, table_name,
                          schema_cache, notes, status, created_by, created_at, updated_at
                """,
                params,
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_schema_cache(self, database_id: int, schema_cache: dict, status: str = "ready") -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE clotho_databases
                SET schema_cache = %s, status = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING id, name, source, engine, host, port, db_name, table_name,
                          schema_cache, notes, status, created_by, created_at, updated_at
                """,
                (json.dumps(schema_cache), status, database_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_status(self, database_id: int, status: str) -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE clotho_databases
                SET status = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING id, name, source, engine, host, port, db_name, table_name,
                          schema_cache, notes, status, created_by, created_at, updated_at
                """,
                (status, database_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def delete_database(self, database_id: int) -> bool:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DELETE FROM clotho_databases WHERE id = %s", (database_id,))
            return cur.rowcount > 0
        finally:
            conn.close()