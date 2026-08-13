"""
SolutionStore — persistência para o Solution Driver.

Tabelas geridas:
  notification_webhooks  — destinos de notificação configurados
  agent_commands         — comandos PowerShell pendentes/executados
  conversations          — threads de conversa analista <-> Jarvis
  conversation_messages  — mensagens de cada thread
"""

import json
import os
import uuid

import psycopg2
import psycopg2.extras


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        dbname=os.getenv("POSTGRES_DB", "jarvis"),
        user=os.getenv("POSTGRES_USER", "jarvis"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
    )


class SolutionStore:

    # ── Webhooks ──────────────────────────────────────────────────────────────

    def get_active_webhooks(self, severity: str) -> list[dict]:
        """
        Devolve webhooks activos cujo min_severity <= severity dado.
        Inclui também webhooks configurados via env (JARVIS_NOTIFICATION_WEBHOOKS).
        """
        sev_rank = _SEVERITY_ORDER.get(severity, 99)
        results  = []

        # 1. Webhooks da DB
        try:
            with _conn() as cx, cx.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(
                    "SELECT * FROM notification_webhooks WHERE active = TRUE"
                )
                for row in cur.fetchall():
                    row_rank = _SEVERITY_ORDER.get(row["min_severity"], 99)
                    if sev_rank <= row_rank:
                        results.append(dict(row))
        except Exception as e:
            print(f"[SolutionStore] get_active_webhooks DB: {e}")

        # 2. Webhooks de env (JARVIS_NOTIFICATION_WEBHOOKS=url1,url2)
        env_urls = [
            u.strip()
            for u in os.getenv("JARVIS_NOTIFICATION_WEBHOOKS", "").split(",")
            if u.strip()
        ]
        existing_urls = {r["url"] for r in results}
        for url in env_urls:
            if url not in existing_urls:
                results.append({"url": url, "name": "env", "host_filter": None})

        return results

    # ── Agent commands ────────────────────────────────────────────────────────

    def save_command(
        self,
        host: str,
        script: str,
        risk_level: str,
        problem_id: str = "",
        conv_id: str = "",
    ) -> int | None:
        sql = """
            INSERT INTO agent_commands
                (host, problem_id, conv_id, script, risk_level, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
            RETURNING id
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (host, problem_id, conv_id, script, risk_level))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            print(f"[SolutionStore] save_command erro: {e}")
            return None

    def get_pending_commands(self, host: str) -> list[dict]:
        sql = """
            SELECT id, script, risk_level, problem_id, conv_id
            FROM agent_commands
            WHERE host = %s AND status = 'pending'
            ORDER BY created_at ASC
            LIMIT 10
        """
        try:
            with _conn() as cx, cx.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(sql, (host,))
                rows = cur.fetchall()
                if rows:
                    ids = [r["id"] for r in rows]
                    cur.execute(
                        "UPDATE agent_commands SET status='running', executed_at=NOW()"
                        " WHERE id = ANY(%s)",
                        (ids,),
                    )
                return [dict(r) for r in rows]
        except Exception as e:
            print(f"[SolutionStore] get_pending_commands erro: {e}")
            return []

    def save_command_result(
        self,
        cmd_id: int,
        stdout: str,
        stderr: str,
        exit_code: int,
    ):
        status = "done" if exit_code == 0 else "failed"
        sql = """
            UPDATE agent_commands
            SET status=%s, stdout=%s, stderr=%s, exit_code=%s, completed_at=NOW()
            WHERE id=%s
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (status, stdout[:500_000], stderr[:100_000], exit_code, cmd_id))
        except Exception as e:
            print(f"[SolutionStore] save_command_result erro: {e}")

    def get_command(self, cmd_id: int) -> dict | None:
        try:
            with _conn() as cx, cx.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(
                    "SELECT * FROM agent_commands WHERE id=%s", (cmd_id,)
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            print(f"[SolutionStore] get_command erro: {e}")
            return None

    # ── Conversations ─────────────────────────────────────────────────────────

    def has_active_conversation(self, problem_id: str) -> bool:
        sql = """
            SELECT 1 FROM conversations
            WHERE problem_id = %s
              AND status NOT IN ('resolved','expired','action_rejected')
              AND (expires_at IS NULL OR expires_at > NOW())
            LIMIT 1
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (problem_id,))
                return cur.fetchone() is not None
        except Exception as e:
            print(f"[SolutionStore] has_active_conversation erro: {e}")
            return False

    def create_conversation(
        self,
        problem_id: str,
        host: str,
        solution_summary: str,
        proposed_script: str,
        risk_level: str,
        snapshot: dict,
        webhook_urls: list[str],
        timeout_minutes: int = 60,
    ) -> str:
        conv_id = str(uuid.uuid4())
        sql = """
            INSERT INTO conversations
                (id, problem_id, host, status, solution_summary,
                 proposed_script, risk_level, snapshot_at_detection,
                 webhook_urls, expires_at)
            VALUES (%s,%s,%s,'awaiting_analyst',%s,%s,%s,%s,%s,
                    NOW() + INTERVAL '1 minute' * %s)
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (
                    conv_id, problem_id, host,
                    solution_summary, proposed_script, risk_level,
                    psycopg2.extras.Json(snapshot),
                    webhook_urls,
                    timeout_minutes,
                ))
            return conv_id
        except Exception as e:
            print(f"[SolutionStore] create_conversation erro: {e}")
            return conv_id

    def get_conversation(self, conv_id: str) -> dict | None:
        try:
            with _conn() as cx, cx.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(
                    "SELECT * FROM conversations WHERE id=%s", (conv_id,)
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            print(f"[SolutionStore] get_conversation erro: {e}")
            return None

    def update_conversation_status(self, conv_id: str, status: str):
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(
                    "UPDATE conversations SET status=%s, updated_at=NOW() WHERE id=%s",
                    (status, conv_id),
                )
        except Exception as e:
            print(f"[SolutionStore] update_conversation_status erro: {e}")

    # ── Messages ──────────────────────────────────────────────────────────────

    def add_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ):
        sql = """
            INSERT INTO conversation_messages (conv_id, role, content, metadata)
            VALUES (%s, %s, %s, %s)
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (
                    conv_id, role, content,
                    psycopg2.extras.Json(metadata or {}),
                ))
        except Exception as e:
            print(f"[SolutionStore] add_message erro: {e}")

    def get_messages(self, conv_id: str) -> list[dict]:
        sql = """
            SELECT role, content, created_at
            FROM conversation_messages
            WHERE conv_id = %s
            ORDER BY created_at ASC
        """
        try:
            with _conn() as cx, cx.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(sql, (conv_id,))
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"[SolutionStore] get_messages erro: {e}")
            return []

    # ── Machine state ─────────────────────────────────────────────────────────

    def get_latest_snapshot(self, host: str) -> dict:
        """Estado mais recente da máquina — usado nas respostas da conversa."""
        sql = """
            SELECT raw_json, snapshot_json, cpu_percent, memory_percent, disk_percent,
                   created_at
            FROM snapshots
            WHERE host = %s
            ORDER BY created_at DESC
            LIMIT 1
        """
        try:
            with _conn() as cx, cx.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(sql, (host,))
                row = cur.fetchone()
                if not row:
                    return {}
                snap = row.get("snapshot_json") or row.get("raw_json") or {}
                if isinstance(snap, str):
                    snap = json.loads(snap)
                return {
                    "cpu_percent":    row["cpu_percent"],
                    "memory_percent": row["memory_percent"],
                    "disk_percent":   row["disk_percent"],
                    "as_of":          str(row["created_at"]),
                    "detail":         snap,
                }
        except Exception as e:
            print(f"[SolutionStore] get_latest_snapshot erro: {e}")
            return {}
