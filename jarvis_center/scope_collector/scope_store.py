"""
ScopeStore — persistência PostgreSQL para os alvos monitorizados (scope_targets)
e para as sugestões de melhoria geradas pelo Explorer Engine (exploration_tips).

Mesmo padrão de acesso do resto do Fates Engine (ver clotho/clotho_store.py):
psycopg2 simples, RealDictCursor, uma ligação por chamada.
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


class ScopeStore:

    # ── scope_targets ─────────────────────────────────────────────────────────

    def list_targets(self, enabled_only: bool = False) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            sql = "SELECT * FROM scope_targets"
            if enabled_only:
                sql += " WHERE enabled = TRUE"
            sql += " ORDER BY created_at DESC"
            cur.execute(sql)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_target(self, target_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM scope_targets WHERE id = %s", (target_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def due_targets(self) -> list[dict]:
        """Alvos activos em modo 'live' cuja cadência já expirou (ou nunca
        foram sondados). Alvos 'historical' não entram aqui — são
        analisados uma vez, sob pedido (ver /targets/{id}/backfill)."""
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT * FROM scope_targets
                WHERE enabled = TRUE
                  AND analysis_mode = 'live'
                  AND (
                        last_polled_at IS NULL
                        OR last_polled_at <= NOW() - (cadence_seconds || ' seconds')::interval
                      )
                ORDER BY last_polled_at NULLS FIRST
                """
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def create_target(self, data: dict) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO scope_targets (
                    integration_id, name, target_kind, target_ref,
                    category, category_source, frame_type, os_type,
                    agentless_fallback, cadence_seconds, host_key_hint,
                    enabled, created_by, analysis_mode, history_start, history_end
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    data.get("integration_id"),
                    data["name"],
                    data["target_kind"],
                    data["target_ref"],
                    data.get("category"),
                    data.get("category_source", "manual"),
                    data.get("frame_type", "metrics"),
                    data.get("os_type", "windows"),
                    bool(data.get("agentless_fallback", False)),
                    int(data.get("cadence_seconds", 60)),
                    data.get("host_key_hint") or data["name"],
                    bool(data.get("enabled", True)),
                    data.get("created_by"),
                    data.get("analysis_mode", "live"),
                    data.get("history_start"),
                    data.get("history_end"),
                ),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def update_category(self, target_id: int, category: str, source: str = "manual") -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE scope_targets
                SET category = %s, category_source = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (category, source, target_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_enabled(self, target_id: int, enabled: bool) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "UPDATE scope_targets SET enabled = %s, updated_at = NOW() WHERE id = %s",
                (enabled, target_id),
            )
        finally:
            conn.close()

    def record_poll_result(self, target_id: int, status: str) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE scope_targets
                SET last_polled_at = NOW(), last_status = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, target_id),
            )
        finally:
            conn.close()

    # ── host_status_signals ──────────────────────────────────────────────────

    def upsert_host_status_signals(self, signals: list, scope_target_id: int | None = None) -> None:
        """signals: list[host_status.HostStatusSignal]. Uma linha por
        (host, source) — só o sinal mais recente, sem histórico."""
        if not signals:
            return
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            for s in signals:
                cur.execute(
                    """
                    INSERT INTO host_status_signals (host, source, severity, message, raw, scope_target_id, observed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (host, source) DO UPDATE SET
                        severity = EXCLUDED.severity,
                        message = EXCLUDED.message,
                        raw = EXCLUDED.raw,
                        scope_target_id = EXCLUDED.scope_target_id,
                        observed_at = NOW()
                    """,
                    (s.host, s.source, s.severity, s.message,
                     json.dumps(s.raw, default=str) if s.raw is not None else None,
                     scope_target_id),
                )
        finally:
            conn.close()

    def due_explorations(self) -> list[dict]:
        """Alvos activos em modo 'live', com categoria confirmada, cuja
        cadência de exploração (muito mais espaçada que a de recolha de
        telemetria) já expirou. Alvos 'historical' são analisados uma vez,
        sob pedido (ver /targets/{id}/backfill), não entram nesta varredura."""
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT * FROM scope_targets
                WHERE enabled = TRUE
                  AND analysis_mode = 'live'
                  AND category IS NOT NULL
                  AND (
                        last_explored_at IS NULL
                        OR last_explored_at <= NOW() - (explore_cadence_seconds || ' seconds')::interval
                      )
                ORDER BY last_explored_at NULLS FIRST
                """
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def record_exploration(self, target_id: int) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "UPDATE scope_targets SET last_explored_at = NOW() WHERE id = %s",
                (target_id,),
            )
        finally:
            conn.close()

    def delete_target(self, target_id: int) -> bool:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DELETE FROM scope_targets WHERE id = %s", (target_id,))
            return cur.rowcount > 0
        finally:
            conn.close()

    # ── exploration_tips ──────────────────────────────────────────────────────

    def create_tip(self, data: dict) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO exploration_tips (
                    scope_target_id, category, title, summary, detail,
                    priority, evidence, needs_investigation, investigation_reason
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    data["scope_target_id"],
                    data["category"],
                    data["title"],
                    data["summary"],
                    data.get("detail"),
                    data.get("priority", "medium"),
                    json.dumps(data.get("evidence") or {}, default=str),
                    bool(data.get("needs_investigation", False)),
                    data.get("investigation_reason"),
                ),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def list_tips(
        self,
        category: str | None = None,
        status: str | None = None,
        scope_target_id: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            clauses, params = [], []
            if category:
                clauses.append("category = %s")
                params.append(category)
            if status:
                clauses.append("status = %s")
                params.append(status)
            if scope_target_id:
                clauses.append("scope_target_id = %s")
                params.append(scope_target_id)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(min(limit, 500))
            cur.execute(
                f"""
                SELECT * FROM exploration_tips
                {where}
                ORDER BY generated_at DESC
                LIMIT %s
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def update_tip_status(self, tip_id: int, status: str) -> dict | None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            resolved_at_sql = "NOW()" if status == "resolved" else "resolved_at"
            cur.execute(
                f"""
                UPDATE exploration_tips
                SET status = %s, resolved_at = {resolved_at_sql}
                WHERE id = %s
                RETURNING *
                """,
                (status, tip_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
