"""
RootResetStore — persistência dos pedidos de reset root, com portão de
aprovação humana (status pending_approval -> ok/error/denied).
"""

import os
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

INVESTIGATION_DELAY_HOURS = int(os.getenv("ROOT_RESET_INVESTIGATION_DELAY_HOURS", "24"))


def _conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


class RootResetStore:

    def find_existing(self, requested_by: str, hostname: str, motivo_slug: str, statuses: tuple[str, ...]) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT * FROM root_reset_requests
                WHERE requested_by = %s AND hostname = %s AND motivo_slug = %s
                  AND status = ANY(%s)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (requested_by, hostname, motivo_slug, list(statuses)),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_pending(
        self,
        requested_by: str,
        motivo_slug: str,
        motivo_text: str,
        hostname: str,
        requester_user_id: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
    ) -> dict:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                INSERT INTO root_reset_requests (
                    requested_by, motivo_slug, motivo_text, hostname, status,
                    requester_user_id, chat_id, message_id
                ) VALUES (%s,%s,%s,%s,'pending_approval',%s,%s,%s)
                RETURNING *
                """,
                (requested_by, motivo_slug, motivo_text, hostname, requester_user_id, chat_id, message_id),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def update_chat_ref(
        self,
        request_id: int,
        requester_user_id: str | None,
        chat_id: str | None,
        message_id: str | None,
    ) -> None:
        """Actualiza para onde a entrega automática (push) deve apontar, sem
        tocar em mais nada — chamado quando o utilizador repete a pergunta
        para um pedido ainda pendente, possivelmente noutro chat."""
        if not (chat_id and message_id):
            return
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE root_reset_requests
                SET requester_user_id = %s, chat_id = %s, message_id = %s
                WHERE id = %s
                """,
                (requester_user_id, chat_id, message_id, request_id),
            )
        finally:
            conn.close()

    def mark_processing(self, request_id: int, approved_by: str) -> dict:
        """Aprovado, mas o reset (WinRM/LAPS) corre em thread de fundo — pode
        demorar até ao watchdog do agentless (~5 min). Sai já de
        'pending_approval' para não ficar na lista de por-aprovar nem
        permitir uma segunda aprovação em paralelo."""
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE root_reset_requests
                SET status = 'processing', approved_by = %s, approved_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (approved_by, request_id),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def mark_delivered(self, request_id: int) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("UPDATE root_reset_requests SET delivered = TRUE WHERE id = %s", (request_id,))
        finally:
            conn.close()

    def list_pending(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM root_reset_requests WHERE status = 'pending_approval' ORDER BY created_at ASC"
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def count_pending(self) -> int:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM root_reset_requests WHERE status = 'pending_approval'")
            return cur.fetchone()[0]
        finally:
            conn.close()

    def get(self, request_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM root_reset_requests WHERE id = %s", (request_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def finish(
        self,
        request_id: int,
        status: str,
        mechanism: str | None,
        error_message: str | None,
        approved_by: str,
        schedule_investigation: bool,
    ) -> dict:
        investigate_at = (
            datetime.now(timezone.utc) + timedelta(hours=INVESTIGATION_DELAY_HOURS)
            if schedule_investigation else None
        )
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                UPDATE root_reset_requests
                SET status = %s, mechanism = %s, error_message = %s,
                    approved_by = %s, approved_at = NOW(), investigate_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (status, mechanism, error_message, approved_by, investigate_at, request_id),
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
                UPDATE root_reset_requests
                SET status = 'denied', error_message = %s, approved_by = %s, approved_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (reason, denied_by, request_id),
            )
            return dict(cur.fetchone())
        finally:
            conn.close()

    def due_for_investigation(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT * FROM root_reset_requests
                WHERE status = 'ok' AND investigated_at IS NULL
                  AND investigate_at IS NOT NULL AND investigate_at <= NOW()
                """
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def mark_investigated(self, request_id: int, investigation_id: str) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE root_reset_requests
                SET investigated_at = NOW(), investigation_id = %s
                WHERE id = %s
                """,
                (investigation_id, request_id),
            )
        finally:
            conn.close()

    # ── Auto-aprovação por motivo (gerido pelo admin) ──────────────────────────

    def is_auto_approved(self, motivo_slug: str) -> bool:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM root_reset_auto_approve WHERE motivo_slug = %s", (motivo_slug,))
            return cur.fetchone() is not None
        finally:
            conn.close()

    def list_auto_approved(self) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM root_reset_auto_approve ORDER BY motivo_text NULLS LAST, motivo_slug")
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def set_auto_approved(self, motivo_slug: str, motivo_text: str | None, updated_by: str) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO root_reset_auto_approve (motivo_slug, motivo_text, updated_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (motivo_slug) DO UPDATE SET
                    motivo_text = EXCLUDED.motivo_text,
                    updated_by  = EXCLUDED.updated_by,
                    updated_at  = NOW()
                """,
                (motivo_slug, motivo_text, updated_by),
            )
        finally:
            conn.close()

    def unset_auto_approved(self, motivo_slug: str) -> None:
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DELETE FROM root_reset_auto_approve WHERE motivo_slug = %s", (motivo_slug,))
        finally:
            conn.close()

    def distinct_motivos_used(self) -> list[dict]:
        """Motivos (slug/texto) já usados em pedidos reais — inclui os
        "custom_*" livres, para o admin também os poder marcar como
        auto-aprovados depois de os ver aparecer pelo menos uma vez."""
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT DISTINCT ON (motivo_slug) motivo_slug, motivo_text
                FROM root_reset_requests
                ORDER BY motivo_slug, created_at DESC
                """
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    # ── Card "Auditoria de Root" (agregação por máquina) ───────────────────────

    def list_machines(self) -> list[dict]:
        """Um registo por máquina (agrega todos os pedidos dessa máquina),
        com o veredicto da investigação mais recente já feita (se houver) e
        se essa investigação já foi vista por um analista (root_reset_alert_acks).
        Ordenado pelo pedido mais recente primeiro.

        Agrupa por UPPER(hostname) — nomes de máquina Windows não distinguem
        maiúsculas/minúsculas, mas os pedidos guardam o texto tal como foi
        escrito (ex: "WKSLPTAK72" e "wkslptak72" são a mesma máquina) —
        sem isto, a mesma máquina apareceria duplicada no card."""
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                WITH ranked AS (
                    SELECT r.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY UPPER(r.hostname)
                               ORDER BY r.investigated_at DESC NULLS LAST
                           ) AS rn_investigated,
                           ROW_NUMBER() OVER (
                               PARTITION BY UPPER(r.hostname)
                               ORDER BY r.created_at DESC
                           ) AS rn_latest
                    FROM root_reset_requests r
                ),
                machine_counts AS (
                    SELECT UPPER(hostname) AS hostname_key,
                           COUNT(*)                                                            AS total_requests,
                           MAX(created_at)                                                      AS last_request_at,
                           COUNT(*) FILTER (WHERE status = 'ok' AND investigated_at IS NULL)    AS pending_investigation_count
                    FROM root_reset_requests
                    GROUP BY UPPER(hostname)
                ),
                latest_hostname AS (
                    SELECT UPPER(hostname) AS hostname_key, hostname AS display_hostname
                    FROM ranked WHERE rn_latest = 1
                ),
                latest_investigated AS (
                    SELECT UPPER(hostname) AS hostname_key, investigation_id, investigated_at
                    FROM ranked
                    WHERE rn_investigated = 1 AND investigation_id IS NOT NULL
                )
                SELECT lh.display_hostname AS hostname, mc.total_requests, mc.last_request_at, mc.pending_investigation_count,
                       li.investigation_id, li.investigated_at,
                       ai.verdict,
                       aa.last_seen_investigation_id
                FROM machine_counts mc
                JOIN latest_hostname lh ON lh.hostname_key = mc.hostname_key
                LEFT JOIN latest_investigated li ON li.hostname_key = mc.hostname_key
                LEFT JOIN agentless_security_investigations ai ON ai.investigation_id = li.investigation_id
                LEFT JOIN root_reset_alert_acks aa ON aa.machine = mc.hostname_key
                ORDER BY mc.last_request_at DESC
                """
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def list_requests_for_machine(self, hostname: str, on_date: str | None = None) -> list[dict]:
        """Todos os pedidos de uma máquina (case-insensitive), mais recente
        primeiro. `on_date` (formato YYYY-MM-DD) filtra pela data de criação."""
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            query = """
                SELECT r.*, ai.verdict
                FROM root_reset_requests r
                LEFT JOIN agentless_security_investigations ai ON ai.investigation_id = r.investigation_id
                WHERE UPPER(r.hostname) = UPPER(%s)
            """
            params: list = [hostname]
            if on_date:
                query += " AND r.created_at::date = %s"
                params.append(on_date)
            query += " ORDER BY r.created_at DESC"
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_investigation_for_request(self, request_id: int) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT ai.*
                FROM root_reset_requests r
                JOIN agentless_security_investigations ai ON ai.investigation_id = r.investigation_id
                WHERE r.id = %s
                """,
                (request_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def ack_machine_alert(self, machine: str, investigation_id: str | None, acknowledged_by: str) -> None:
        """Marca a investigação mais recente desta máquina como vista — chamado
        quando um analista abre "Ver detalhes". Se não houver investigação
        nenhuma ainda, não há nada para marcar como visto."""
        if not investigation_id:
            return
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO root_reset_alert_acks (machine, last_seen_investigation_id, acknowledged_by)
                VALUES (UPPER(%s), %s, %s)
                ON CONFLICT (machine) DO UPDATE SET
                    last_seen_investigation_id = EXCLUDED.last_seen_investigation_id,
                    acknowledged_by = EXCLUDED.acknowledged_by,
                    acknowledged_at = NOW()
                """,
                (machine, investigation_id, acknowledged_by),
            )
        finally:
            conn.close()
