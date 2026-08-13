"""
DashboardWidgetsStore — persistência PostgreSQL para os widgets de dados
cruzados da página inicial (Fase 3 do redesign da UI).
"""

import json
import os

import psycopg2
import psycopg2.extras


def _conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",      "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


class DashboardWidgetsStore:

    def list_widgets(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM dashboard_widgets ORDER BY position, created_at")
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_widget(self, widget_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM dashboard_widgets WHERE id = %s", (widget_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_widget(self, prompt: str, created_by: str | None = None) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO dashboard_widgets (prompt, created_by, position)
                VALUES (%s, %s, (SELECT COALESCE(MAX(position), 0) + 1 FROM dashboard_widgets))
                RETURNING *
                """,
                (prompt, created_by),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def set_result(
        self,
        widget_id: int,
        title: str | None,
        widget_type: str | None,
        data: dict | None,
        error: str | None,
    ) -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE dashboard_widgets
                SET title = %s, widget_type = %s, last_data = %s,
                    last_error = %s, last_refreshed_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (title, widget_type, json.dumps(data) if data is not None else None, error, widget_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_prompt(self, widget_id: int, prompt: str) -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "UPDATE dashboard_widgets SET prompt = %s WHERE id = %s RETURNING *",
                (prompt, widget_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_layout(self, widget_id: int, col_span: int | None, row_span: int | None) -> dict | None:
        """Ajuste puro de layout (tamanho) — não mexe em prompt/dados, não
        chama a LLM. Só atualiza os campos que vierem preenchidos."""
        fields = {}
        if col_span is not None:
            fields["col_span"] = max(2, min(12, col_span))
        if row_span is not None:
            fields["row_span"] = max(2, row_span)
        if not fields:
            return self.get_widget(widget_id)

        set_clause = ", ".join(f"{k} = %s" for k in fields)
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                f"UPDATE dashboard_widgets SET {set_clause} WHERE id = %s RETURNING *",
                (*fields.values(), widget_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def reorder(self, ordered_ids: list[int]) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.executemany(
                "UPDATE dashboard_widgets SET position = %s WHERE id = %s",
                [(i, widget_id) for i, widget_id in enumerate(ordered_ids)],
            )
        finally:
            conn.close()

    def delete_widget(self, widget_id: int) -> bool:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DELETE FROM dashboard_widgets WHERE id = %s", (widget_id,))
            deleted = cur.rowcount > 0
            return deleted
        finally:
            conn.close()
