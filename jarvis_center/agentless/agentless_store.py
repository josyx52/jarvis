"""
AgentlessStore — persistência PostgreSQL para investigações agentless.
"""

import hashlib
import logging
import os
import json
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

log = logging.getLogger("jarvis.agentless_store")

# Janela de inactividade que define os limites de uma "sessão de investigação"
# por máquina (ver agentless_investigation_sessions). Enquanto houver
# execuções agentless à mesma máquina dentro desta janela, contam como a
# mesma investigação; passada a janela sem actividade, a sessão fecha e o
# contexto consolidado fica em agentless_machine_profile.known_state.
_SESSION_WINDOW_MINUTES = int(os.getenv("AGENTLESS_SESSION_WINDOW_MINUTES", "15"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execution_summary(success: bool, error_summary: str | None, snapshot: dict) -> str:
    """Resumo curto (não a auditoria completa — isso é agentless_execution_log)
    de uma execução, para caber no array `executions` da sessão."""
    if not success:
        return (error_summary or "falhou sem detalhe")[:200]
    if snapshot.get("raw_stdout"):
        return snapshot["raw_stdout"][:200]
    if snapshot:
        return json.dumps(snapshot)[:200]
    return "sucesso, sem output"

# Assinaturas de erro reconhecidas para agrupar known_issues por tipo em vez
# de por mensagem exacta (que varia). Ordem importa — a primeira que bater
# no texto do erro é usada.
_ERROR_SIGNATURES = [
    ("access_denied",  ("access is denied", "access denied", "permission denied")),
    ("unreachable",    ("cannot connect", "the winrm client cannot", "no such host",
                        "timed out", "network path was not found", "target machine actively refused")),
    ("cert_or_auth",   ("the server certificate", "authentication failed", "unauthorized")),
]


def _classify_error(error: str | None) -> str | None:
    if not error:
        return None
    low = error.lower()
    for signature, needles in _ERROR_SIGNATURES:
        if any(n in low for n in needles):
            return signature
    return "other"


def _conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


class AgentlessStore:

    def save_security_investigation(self, result: dict) -> int:
        """
        Persiste o resultado de uma investigação de segurança.
        Devolve o id gerado.
        """
        conn = _conn()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO agentless_security_investigations (
                    investigation_id, machine, username, access_method,
                    credential_time, stated_reason, verdict, report,
                    evidence_summary, error, investigated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    result["investigation_id"],
                    result["machine"],
                    result["username"],
                    result["access_method"],
                    result["credential_time"],
                    result["stated_reason"],
                    result["verdict"],
                    result["report"],
                    json.dumps(result.get("evidence_summary", {})),
                    result.get("error"),
                    result["investigated_at"],
                ),
            )
            row = cur.fetchone()
            return row[0] if row else -1
        finally:
            conn.close()

    def get_security_investigation(self, investigation_id: str) -> dict | None:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT * FROM agentless_security_investigations
                WHERE investigation_id = %s
                """,
                (investigation_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_security_investigations(
        self,
        machine: str | None = None,
        username: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            filters = []
            params  = []
            if machine:
                filters.append("machine ILIKE %s")
                params.append(f"%{machine}%")
            if username:
                filters.append("username ILIKE %s")
                params.append(f"%{username}%")
            where = ("WHERE " + " AND ".join(filters)) if filters else ""
            params.append(limit)
            cur.execute(
                f"""
                SELECT investigation_id, machine, username, access_method,
                       credential_time, stated_reason, verdict, error, investigated_at
                FROM agentless_security_investigations
                {where}
                ORDER BY investigated_at DESC
                LIMIT %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    # ── Auditoria de execução + perfil agregado da máquina ──────────────────

    def record_execution(
        self,
        host: str,
        machine_type: str,
        script: str,
        success: bool,
        stdout: str = "",
        exit_code: int | None = None,
        error_summary: str | None = None,
        transport: str | None = None,
        duration_ms: int | None = None,
        operator: str = "system",
        source: str = "chat",
        context_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Grava uma linha de auditoria em agentless_execution_log e actualiza
        o agentless_machine_profile agregado dessa máquina.

        Chamado SEMPRE depois do resultado já ter sido devolvido a quem
        pediu a execução — nunca deve bloquear nem falhar essa entrega.
        Qualquer erro aqui é apanhado e ignorado pelo chamador (ver
        execution_broker.py), mas por segurança também protegemos aqui.
        """
        if not host:
            return
        host = host.strip().upper()

        script_hash = hashlib.sha256((script or "").encode("utf-8")).hexdigest()
        snippet     = (script or "")[:300]

        snapshot: dict = {}
        stdout_stripped = (stdout or "").strip()
        if stdout_stripped:
            try:
                parsed = json.loads(stdout_stripped)
                if isinstance(parsed, dict):
                    snapshot = parsed
                else:
                    snapshot = {"raw_stdout": stdout_stripped[:1000]}
            except (json.JSONDecodeError, ValueError):
                snapshot = {"raw_stdout": stdout_stripped[:1000]}

        conn = _conn()
        try:
            # autocommit=False de propósito: o SELECT ... FOR UPDATE dentro de
            # _upsert_machine_profile só protege o padrão ler-calcular-escrever
            # (contadores, merge de known_state/known_issues) se o lock durar
            # até ao commit desta transacção — com autocommit, cada execute()
            # commitava logo a seguir e o lock era libertado antes do INSERT
            # seguinte correr, permitindo "lost updates" sob concorrência real
            # (encontrado pelo platform-architect, ver COORDINATION.md).
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO agentless_execution_log (
                    host, machine_type, operator, source, context_id,
                    script_hash, script_snippet, transport, success,
                    exit_code, error_summary, snapshot, duration_ms
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    host, machine_type, operator, source, context_id,
                    script_hash, snippet, transport, success,
                    exit_code, error_summary, json.dumps(snapshot), duration_ms,
                ),
            )
            self._upsert_machine_profile(
                cur, host=host, machine_type=machine_type, success=success,
                error_summary=error_summary, duration_ms=duration_ms,
            )
            self._upsert_session(
                cur, host=host, script=script, success=success,
                error_summary=error_summary, snapshot=snapshot, reason=reason,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _upsert_machine_profile(
        cur, host: str, machine_type: str, success: bool,
        error_summary: str | None, duration_ms: int | None,
    ) -> None:
        """Contadores/known_issues agregados. known_state NÃO é tocado aqui —
        só _finalize_session_row o escreve, quando uma sessão de investigação
        fecha (ver agentless_investigation_sessions)."""
        cur.execute(
            "SELECT total_executions, total_failures, consecutive_failures, "
            "avg_duration_ms, known_issues "
            "FROM agentless_machine_profile WHERE host = %s FOR UPDATE",
            (host,),
        )
        row = cur.fetchone()

        if row is None:
            total_executions, total_failures, consecutive_failures = 0, 0, 0
            avg_duration_ms = None
            known_issues: dict = {}
        else:
            total_executions, total_failures, consecutive_failures, avg_duration_ms, known_issues_raw = row
            known_issues = known_issues_raw or {}

        total_executions += 1
        if success:
            consecutive_failures = 0
            total_failures_new = total_failures
        else:
            consecutive_failures += 1
            total_failures_new = total_failures + 1
            signature = _classify_error(error_summary)
            if signature:
                entry = known_issues.get(signature, {"count": 0, "first_seen": None})
                entry["count"] = entry.get("count", 0) + 1
                entry["first_seen"] = entry.get("first_seen") or _now_iso()
                entry["last_seen"] = _now_iso()
                known_issues = {**known_issues, signature: entry}

        if duration_ms is not None:
            avg_duration_ms = (
                duration_ms if avg_duration_ms is None
                else round((avg_duration_ms * (total_executions - 1) + duration_ms) / total_executions)
            )

        cur.execute(
            """
            INSERT INTO agentless_machine_profile (
                host, machine_type_preferred, total_executions, total_failures,
                consecutive_failures, last_success_at, last_failure_at,
                last_error_summary, avg_duration_ms, known_issues, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            ON CONFLICT (host) DO UPDATE SET
                machine_type_preferred = EXCLUDED.machine_type_preferred,
                total_executions       = EXCLUDED.total_executions,
                total_failures         = EXCLUDED.total_failures,
                consecutive_failures   = EXCLUDED.consecutive_failures,
                last_success_at        = COALESCE(EXCLUDED.last_success_at, agentless_machine_profile.last_success_at),
                last_failure_at        = COALESCE(EXCLUDED.last_failure_at, agentless_machine_profile.last_failure_at),
                last_error_summary     = COALESCE(EXCLUDED.last_error_summary, agentless_machine_profile.last_error_summary),
                avg_duration_ms        = EXCLUDED.avg_duration_ms,
                known_issues           = EXCLUDED.known_issues,
                updated_at             = NOW()
            """,
            (
                host,
                machine_type if success else None,
                total_executions,
                total_failures_new,
                consecutive_failures,
                _now_iso() if success else None,
                _now_iso() if not success else None,
                error_summary if not success else None,
                avg_duration_ms,
                json.dumps(known_issues),
            ),
        )

    # ── Sessões de investigação (agrupam várias execuções que resolvem UM
    # problema na mesma máquina — ver schema_init.sql para a explicação
    # completa da janela de inactividade) ────────────────────────────────

    @classmethod
    def _upsert_session(
        cls, cur, host: str, script: str, success: bool,
        error_summary: str | None, snapshot: dict, reason: str | None,
    ) -> None:
        cur.execute(
            """
            SELECT id, reason, started_at, execution_count, executions,
                   EXTRACT(EPOCH FROM (NOW() - last_activity_at)) AS idle_seconds
            FROM agentless_investigation_sessions
            WHERE host = %s AND status = 'open'
            FOR UPDATE
            """,
            (host,),
        )
        row = cur.fetchone()

        if row is not None:
            session_id, existing_reason, started_at, execution_count, executions, idle_seconds = row
            if idle_seconds > _SESSION_WINDOW_MINUTES * 60:
                # Sessão anterior ficou "quieta" mais tempo que a janela —
                # não é a mesma investigação. Fecha-a com o que já tinha, e
                # trata esta execução como o início de uma sessão nova.
                cls._finalize_session_row(
                    cur, host=host, reason=existing_reason, started_at=started_at,
                    execution_count=execution_count, executions=executions,
                )
                row = None

        summary = _execution_summary(success, error_summary, snapshot)
        new_execution = {
            "script": (script or "")[:300], "success": success,
            "summary": summary, "at": _now_iso(),
        }

        if row is None:
            cur.execute(
                """
                INSERT INTO agentless_investigation_sessions
                    (host, status, reason, execution_count, executions)
                VALUES (%s, 'open', %s, 1, %s)
                """,
                (host, (reason or "")[:300] or None, json.dumps([new_execution])),
            )
        else:
            executions = (executions or []) + [new_execution]
            cur.execute(
                """
                UPDATE agentless_investigation_sessions
                SET execution_count = %s, executions = %s, last_activity_at = NOW()
                WHERE id = %s
                """,
                (execution_count + 1, json.dumps(executions), session_id),
            )

    @staticmethod
    def _finalize_session_row(cur, host: str, reason, started_at, execution_count, executions) -> None:
        """Escreve o contexto consolidado da sessão em
        agentless_machine_profile.known_state.last_session e marca a sessão
        como fechada. known_state SÓ é escrito aqui — nunca por execução
        avulsa (ver _upsert_machine_profile)."""
        last_session = {
            "reason":          reason,
            "started_at":      started_at.isoformat() if hasattr(started_at, "isoformat") else started_at,
            "closed_at":       _now_iso(),
            "execution_count": execution_count,
            "executions":      executions,
        }
        cur.execute(
            """
            INSERT INTO agentless_machine_profile (host, known_state, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (host) DO UPDATE SET
                known_state = jsonb_set(agentless_machine_profile.known_state, '{last_session}', %s::jsonb),
                updated_at  = NOW()
            """,
            (host, json.dumps({"last_session": last_session}), json.dumps(last_session)),
        )
        cur.execute(
            """
            UPDATE agentless_investigation_sessions
            SET status = 'closed', closed_at = NOW()
            WHERE host = %s AND status = 'open'
            """,
            (host,),
        )

    def close_stale_sessions(self) -> int:
        """Sweep periódico — fecha sessões 'open' cuja máquina ficou quieta
        mais tempo que a janela e nunca mais recebeu uma chamada a
        despoletar o fecho preguiçoso em _upsert_session. Chamado por um
        worker em segundo plano (ver execution_broker.py). Devolve quantas
        sessões fechou."""
        conn = _conn()
        try:
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute(
                """
                SELECT host, reason, started_at, execution_count, executions
                FROM agentless_investigation_sessions
                WHERE status = 'open'
                  AND last_activity_at < NOW() - (%s || ' minutes')::interval
                FOR UPDATE
                """,
                (_SESSION_WINDOW_MINUTES,),
            )
            stale = cur.fetchall()
            for host, reason, started_at, execution_count, executions in stale:
                self._finalize_session_row(
                    cur, host=host, reason=reason, started_at=started_at,
                    execution_count=execution_count, executions=executions,
                )
            conn.commit()
            return len(stale)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_machine_profiles(self, hosts: list[str]) -> dict[str, dict]:
        """Versão em lote de get_machine_profile — uma única query para até
        centenas de hosts (agentless_run_bulk), em vez de N round-trips."""
        if not hosts:
            return {}
        normalized = [h.strip().upper() for h in hosts if h]
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM agentless_machine_profile WHERE host = ANY(%s)",
                (normalized,),
            )
            return {row["host"]: dict(row) for row in cur.fetchall()}
        finally:
            conn.close()

    def get_machine_profile(self, host: str) -> dict | None:
        """Consulta rápida do perfil agregado de uma máquina — chamado ANTES
        de executar, para o Jarvis saber já o que se conhece sobre ela.
        Nunca deve bloquear a execução: o chamador apanha exceções."""
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM agentless_machine_profile WHERE host = %s",
                (host.strip().upper(),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def query_execution_history(
        self, host: str | None = None, since_hours: int | None = None, limit: int = 20,
    ) -> list[dict]:
        """Consulta o log bruto de auditoria (agentless_execution_log) — o
        lado da leitura que faltava: sem host, devolve as execuções mais
        recentes em TODAS as máquinas (ex: "qual foi a última máquina em que
        correste algo?"); com host, o histórico dessa máquina específica.
        Só leitura, sem aprovação — como query_ad/query_machine_profile."""
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            filters, params = [], []
            if host:
                filters.append("host = %s")
                params.append(host.strip().upper())
            if since_hours:
                filters.append("executed_at >= NOW() - (%s || ' hours')::interval")
                params.append(since_hours)
            where = ("WHERE " + " AND ".join(filters)) if filters else ""
            params.append(max(1, min(limit, 100)))
            cur.execute(
                f"""
                SELECT host, machine_type, operator, source, success, exit_code,
                       error_summary, transport, duration_ms, executed_at, script_snippet
                FROM agentless_execution_log
                {where}
                ORDER BY executed_at DESC
                LIMIT %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
