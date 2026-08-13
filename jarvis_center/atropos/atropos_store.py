"""
AtroposStore — leitura agregada (PostgreSQL) para a dashboard de estado de
infraestrutura do Atropos: hosts/heartbeat, execuções Lachesis e alertas de
segurança (UEBA). Só leitura — nunca escreve nestas tabelas.
"""

import json
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

# Host considerado "com heartbeat recente" se reportou a agent_health dentro
# desta janela; caso contrário aparece honestamente como sem heartbeat/stale.
_RECENT_HEARTBEAT_MINUTES = 15

# Alertas de segurança são considerados desatualizados (motor UEBA
# provavelmente parado) se o mais recente tiver mais de N dias.
_ALERTS_STALE_DAYS = 3

# Combina o estado de heartbeat do agente com o pior sinal externo
# (host_status_signals, escrito pelo scope_collector a partir de
# Zabbix/Splunk/outras integrações Clotho) num único overall_status — o
# maior rank vence. "critical"/"no_heartbeat" empatam de propósito: tanto
# faz o host estar mudo como estar a gritar, o resultado visual é o mesmo.
_STATUS_RANK = {
    "online": 0, "ok": 0,
    "warning": 1, "stale": 1, "unknown": 1,
    "no_heartbeat": 2, "critical": 2,
}


def _conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",      "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


class AtroposStore:

    # ── Hosts ────────────────────────────────────────────────────────────────

    def list_hosts(self) -> list[dict]:
        # server_profiles.host stores "HOSTNAME::run-uuid" — one row per
        # profiling run, several rows can share the same real hostname.
        # agent_health.host, on the other hand, stores the plain hostname.
        # Group by the real hostname (latest profiling run) before joining.
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT DISTINCT ON (split_part(host, '::', 1))
                       split_part(host, '::', 1) AS host,
                       profile,
                       updated_at
                FROM server_profiles
                ORDER BY split_part(host, '::', 1), updated_at DESC
                """
            )
            profile_rows = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT DISTINCT ON (host) host, status AS last_status, created_at AS last_seen
                FROM agent_health
                ORDER BY host, created_at DESC
                """
            )
            health_by_host = {row["host"]: row for row in cur.fetchall()}

            cur.execute("SELECT host, source, severity, message, observed_at FROM host_status_signals")
            signals_by_host: dict[str, list[dict]] = {}
            for row in cur.fetchall():
                signals_by_host.setdefault(row["host"], []).append({
                    "source": row["source"],
                    "severity": row["severity"],
                    "message": row["message"],
                    "observed_at": row["observed_at"].isoformat() if row["observed_at"] else None,
                })
        finally:
            conn.close()

        # Base = união de todos os hosts conhecidos, não só os já perfilados
        # pelo agente — um host visto só pelo Zabbix/Splunk (sem agente
        # Jarvis instalado) continua a aparecer, com profile/last_seen a None.
        profile_by_host = {p["host"]: p for p in profile_rows}
        all_hosts = set(profile_by_host) | set(health_by_host) | set(signals_by_host)

        rows = []
        for host in all_hosts:
            p = profile_by_host.get(host)
            health = health_by_host.get(host)
            signals = signals_by_host.get(host, [])
            has_agent = bool(p or health)
            # Um host só visto pelo Zabbix/Splunk nunca teve heartbeat de
            # agente para "deixar de reportar" — "no_heartbeat" seria
            # enganador aqui. status fica None; o estado passa a depender só
            # dos sinais externos (ok por omissão, na ausência de qualquer).
            heartbeat_status = self._host_status(health["last_seen"] if health else None) if has_agent else None
            row = {
                "host": host,
                "profile": self._profile_role(p["profile"]) if p else None,
                "updated_at": p["updated_at"].isoformat() if p and p["updated_at"] else None,
                "last_status": health["last_status"] if health else None,
                "last_seen": health["last_seen"].isoformat() if health else None,
                "status": heartbeat_status,
                "external_signals": signals,
                "overall_status": self._overall_status(heartbeat_status or "ok", signals),
                "known_via": "agent" if has_agent else "external_only",
            }
            rows.append(row)
        return sorted(rows, key=lambda r: r["host"])

    @staticmethod
    def _overall_status(heartbeat_status: str, signals: list[dict]) -> str:
        worst = heartbeat_status
        for s in signals:
            if _STATUS_RANK.get(s["severity"], 0) > _STATUS_RANK.get(worst, 0):
                worst = s["severity"]
        return worst

    @staticmethod
    def _profile_role(profile_raw) -> str | None:
        if not profile_raw:
            return None
        try:
            data = json.loads(profile_raw) if isinstance(profile_raw, str) else profile_raw
            if isinstance(data, dict):
                return data.get("role") or data.get("role_detail")
        except (ValueError, TypeError):
            pass
        return str(profile_raw)[:60]

    @staticmethod
    def _host_status(last_seen) -> str:
        if last_seen is None:
            return "no_heartbeat"
        from datetime import datetime, timezone
        age = datetime.now(timezone.utc) - last_seen.astimezone(timezone.utc)
        if age <= timedelta(minutes=_RECENT_HEARTBEAT_MINUTES):
            return "online"
        return "stale"

    # ── Resumo (KPIs + tendência 7 dias) ────────────────────────────────────

    def summary(self) -> dict:
        hosts = self.list_hosts()
        hosts_total = len(hosts)
        hosts_online = sum(1 for h in hosts if h["status"] == "online")
        hosts_critical = sum(1 for h in hosts if h["overall_status"] == "critical")

        signals_by_source: dict[str, dict[str, int]] = {}
        for h in hosts:
            for sig in h["external_signals"]:
                bucket = signals_by_source.setdefault(sig["source"], {})
                bucket[sig["severity"]] = bucket.get(sig["severity"], 0) + 1

        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            cur.execute(
                "SELECT status, COUNT(*) AS c FROM lachesis_runs "
                "WHERE started_at::date = CURRENT_DATE GROUP BY status"
            )
            today_by_status = {row["status"]: row["c"] for row in cur.fetchall()}
            run_count = sum(today_by_status.values())
            success_count = today_by_status.get("ok", 0)
            failed_count = today_by_status.get("error", 0)

            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE enabled) AS active, COUNT(*) AS total "
                "FROM lachesis_tasks"
            )
            tasks_row = cur.fetchone()

            cur.execute(
                "SELECT severity, COUNT(*) AS c FROM security_alerts "
                "WHERE acknowledged = FALSE GROUP BY severity"
            )
            by_severity = {row["severity"]: row["c"] for row in cur.fetchall()}
            open_count = sum(by_severity.values())

            cur.execute("SELECT MAX(created_at) AS most_recent FROM security_alerts")
            most_recent_at = cur.fetchone()["most_recent"]

            cur.execute(
                "SELECT started_at::date AS d, COUNT(*) AS c FROM lachesis_runs "
                "WHERE started_at >= CURRENT_DATE - INTERVAL '6 days' "
                "GROUP BY d ORDER BY d"
            )
            trend_by_day = {row["d"]: row["c"] for row in cur.fetchall()}
        finally:
            conn.close()

        stale, stale_reason = self._alerts_staleness(most_recent_at)

        today = date.today()
        trend_7d = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            trend_7d.append({"date": d.isoformat(), "run_count": trend_by_day.get(d, 0)})

        return {
            "hosts": {
                "total": hosts_total,
                "with_recent_heartbeat": hosts_online,
                "stale_or_no_heartbeat": hosts_total - hosts_online,
                "overall_critical": hosts_critical,
            },
            "external_signals": {
                "by_source": signals_by_source,
            },
            "automations_today": {
                "run_count": run_count,
                "success_count": success_count,
                "failed_count": failed_count,
                "success_rate": round(success_count / run_count, 3) if run_count else None,
            },
            "lachesis_tasks": {
                "active": tasks_row["active"],
                "total": tasks_row["total"],
            },
            "security_alerts": {
                "open_count": open_count,
                "by_severity": by_severity,
                "most_recent_at": most_recent_at.isoformat() if most_recent_at else None,
                "stale": stale,
                "stale_reason": stale_reason,
            },
            "trend_7d": trend_7d,
        }

    # ── Detalhe de host: alerts / predictions / investigations / problems ──────
    # host_key nestas tabelas é "hostname::boot_id" (ver CentralRuntime._host_key)
    # — o parâmetro `host` recebido aqui é só o hostname, por isso o match é
    # sempre por prefixo "hostname::%".

    def host_alerts(self, host: str, limit: int = 50) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT id, host, severity, title, payload, created_at
                FROM alerts
                WHERE host ILIKE %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (f"{host}::%", limit),
            )
            return [self._row_out(dict(r)) for r in cur.fetchall()]
        finally:
            conn.close()

    def host_predictions(self, host: str, limit: int = 50) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT id, host, prediction_type, severity, summary, payload, created_at
                FROM predictions
                WHERE host ILIKE %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (f"{host}::%", limit),
            )
            return [self._row_out(dict(r)) for r in cur.fetchall()]
        finally:
            conn.close()

    def host_investigations(self, host: str, limit: int = 50) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT id, host, severity, summary, details, alert_id, created_at
                FROM investigations
                WHERE host ILIKE %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (f"{host}::%", limit),
            )
            return [self._row_out(dict(r)) for r in cur.fetchall()]
        finally:
            conn.close()

    def host_problems(self, host: str, limit: int = 50) -> list[dict]:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute(
                    """
                    SELECT problem_id, host, title, severity, status, alert_count,
                           alert_titles, opened_at, last_seen, resolved_at, duration_s
                    FROM problems
                    WHERE host ILIKE %s
                    ORDER BY COALESCE(last_seen, opened_at) DESC
                    LIMIT %s
                    """,
                    (f"{host}::%", limit),
                )
            except psycopg2.errors.UndefinedTable:
                # 'problems' só é criada em runtime, na primeira vez que um
                # Problem é gravado (ver MemoryStore._ensure_problems_table).
                # Numa instalação nova, ainda pode não existir.
                return []
            return [self._row_out(dict(r)) for r in cur.fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _row_out(row: dict) -> dict:
        """Serializa timestamps para ISO 8601 para poderem ir em JSON."""
        for key, value in row.items():
            if hasattr(value, "isoformat"):
                row[key] = value.isoformat()
        return row

    @staticmethod
    def _alerts_staleness(most_recent_at) -> tuple[bool, str | None]:
        if most_recent_at is None:
            return True, "Sem alertas de segurança registados."
        from datetime import datetime, timezone
        age = datetime.now(timezone.utc) - most_recent_at.astimezone(timezone.utc)
        if age > timedelta(days=_ALERTS_STALE_DAYS):
            days = age.days
            return True, f"Nenhum alerta novo há {days} dias — confirmar se o motor UEBA está a correr."
        return False, None

    # ── Atividade Lachesis recente entre tarefas ────────────────────────────

    def activity(self, limit: int = 50) -> dict:
        conn = _conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT r.id AS run_id, r.task_id, t.name AS task_name, r.status,
                       r.started_at, r.finished_at, r.error
                FROM lachesis_runs r
                JOIN lachesis_tasks t ON t.id = r.task_id
                ORDER BY r.started_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            runs = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT r.task_id, t.name AS task_name, COUNT(*) AS run_count,
                       COUNT(*) FILTER (WHERE r.status = 'error') AS error_count
                FROM lachesis_runs r
                JOIN lachesis_tasks t ON t.id = r.task_id
                WHERE r.started_at >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY r.task_id, t.name
                HAVING COUNT(*) >= 3
                """
            )
            candidates = [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

        for run in runs:
            for field in ("started_at", "finished_at"):
                if run[field] is not None:
                    run[field] = run[field].isoformat()

        flagged_tasks = []
        for c in candidates:
            failure_rate = c["error_count"] / c["run_count"]
            if failure_rate >= 0.5:
                flagged_tasks.append({
                    "task_id": c["task_id"],
                    "task_name": c["task_name"],
                    "failure_rate_7d": round(failure_rate, 3),
                    "run_count_7d": c["run_count"],
                })

        return {"runs": runs, "flagged_tasks": flagged_tasks}
