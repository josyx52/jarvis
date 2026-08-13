"""
UserActivityStore — persiste e consulta eventos de segurança por utilizador.

Alimentado pelos frames security_events vindos do SecurityEventsCollector
do agente. Fornece dados ao UEBAEngine e UserProfiler.
"""

import os
import psycopg2
import psycopg2.extras


def _conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        dbname=os.getenv("POSTGRES_DB", "jarvis"),
        user=os.getenv("POSTGRES_USER", "jarvis"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
    )


class UserActivityStore:

    def bulk_insert(self, host: str, events: list[dict]):
        """Insere batch de eventos de segurança vindos do agente."""
        if not events:
            return
        rows = []
        for ev in events:
            rows.append((
                host,
                ev.get("username", "unknown"),
                ev.get("event_type", "unknown"),
                ev.get("event_time"),
                ev.get("severity", "low"),
                psycopg2.extras.Json(ev.get("details") or {}),
            ))
        sql = """
            INSERT INTO user_activity_events
                (host, username, event_type, event_time, severity, details)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql, rows, page_size=200)
        except Exception as e:
            print(f"[UserActivityStore] bulk_insert erro: {e}")

    def get_recent_events(
        self,
        host: str,
        username: str,
        hours: int = 24,
        limit: int = 300,
    ) -> list[dict]:
        """Devolve eventos recentes de um utilizador num host."""
        sql = """
            SELECT username, event_type, event_time, severity, details
            FROM user_activity_events
            WHERE host = %s
              AND username = %s
              AND event_time >= NOW() - INTERVAL '%s hours'
            ORDER BY event_time ASC
            LIMIT %s
        """
        try:
            with _conn() as cx, cx.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (host, username, hours, limit))
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"[UserActivityStore] get_recent_events erro: {e}")
            return []

    def get_active_users(self, host: str, hours: int = 24) -> list[str]:
        """Lista utilizadores com actividade nas últimas N horas."""
        sql = """
            SELECT DISTINCT username
            FROM user_activity_events
            WHERE host = %s
              AND event_time >= NOW() - INTERVAL '%s hours'
            ORDER BY username
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (host, hours))
                return [r[0] for r in cur.fetchall()]
        except Exception as e:
            print(f"[UserActivityStore] get_active_users erro: {e}")
            return []

    def get_high_severity_events(self, host: str, hours: int = 1) -> list[dict]:
        """Eventos de severidade high/critical — trigger para análise imediata."""
        sql = """
            SELECT username, event_type, event_time, severity, details
            FROM user_activity_events
            WHERE host = %s
              AND severity IN ('high', 'critical')
              AND event_time >= NOW() - INTERVAL '%s hours'
            ORDER BY event_time DESC
            LIMIT 50
        """
        try:
            with _conn() as cx, cx.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (host, hours))
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"[UserActivityStore] get_high_severity_events erro: {e}")
            return []

    def get_events_for_profile(self, host: str, username: str, days: int = 7) -> list[dict]:
        """7 dias de actividade para construir perfil comportamental."""
        sql = """
            SELECT username, event_type, event_time, severity, details
            FROM user_activity_events
            WHERE host = %s
              AND username = %s
              AND event_time >= NOW() - INTERVAL '%s days'
            ORDER BY event_time ASC
            LIMIT 2000
        """
        try:
            with _conn() as cx, cx.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (host, username, days))
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"[UserActivityStore] get_events_for_profile erro: {e}")
            return []

    def save_analysis(self, host: str, username: str, analysis_type: str,
                      result: dict, events_analyzed: int, trigger_event: str = "") -> int | None:
        """Persiste resultado de análise UEBA."""
        sql = """
            INSERT INTO ueba_analyses
                (host, username, analysis_type, compliance_score, verdict,
                 summary, anomalies, events_analyzed, trigger_event, raw_analysis)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (
                    host, username, analysis_type,
                    result.get("compliance_score"),
                    result.get("verdict"),
                    result.get("summary"),
                    psycopg2.extras.Json(result.get("anomalies") or []),
                    events_analyzed,
                    trigger_event,
                    result.get("raw_analysis", ""),
                ))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            print(f"[UserActivityStore] save_analysis erro: {e}")
            return None

    def save_profile(self, host: str, username: str, profile: dict):
        """Upsert do perfil comportamental do utilizador."""
        sql = """
            INSERT INTO user_profiles
                (host, username, is_human, risk_score, risk_level, risk_delta,
                 work_type, primary_apps, typical_hours, active_days,
                 after_hours_activity, weekend_activity,
                 frequent_destinations, suspicious_destinations,
                 privilege_abuse_detected, behavioral_anomalies, summary, raw_analysis)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (host, username) DO UPDATE SET
                is_human                 = EXCLUDED.is_human,
                risk_score               = EXCLUDED.risk_score,
                risk_level               = EXCLUDED.risk_level,
                risk_delta               = EXCLUDED.risk_score - user_profiles.risk_score,
                work_type                = EXCLUDED.work_type,
                primary_apps             = EXCLUDED.primary_apps,
                typical_hours            = EXCLUDED.typical_hours,
                active_days              = EXCLUDED.active_days,
                after_hours_activity     = EXCLUDED.after_hours_activity,
                weekend_activity         = EXCLUDED.weekend_activity,
                frequent_destinations    = EXCLUDED.frequent_destinations,
                suspicious_destinations  = EXCLUDED.suspicious_destinations,
                privilege_abuse_detected = EXCLUDED.privilege_abuse_detected,
                behavioral_anomalies     = EXCLUDED.behavioral_anomalies,
                summary                  = EXCLUDED.summary,
                raw_analysis             = EXCLUDED.raw_analysis,
                profile_version          = user_profiles.profile_version + 1,
                created_at               = NOW()
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (
                    host, username,
                    profile.get("is_human"),
                    profile.get("risk_score", 0),
                    profile.get("risk_level", "low"),
                    profile.get("risk_delta", 0),
                    profile.get("work_type"),
                    psycopg2.extras.Json(profile.get("primary_apps") or []),
                    profile.get("typical_hours"),
                    psycopg2.extras.Json(profile.get("active_days") or []),
                    profile.get("after_hours_activity", False),
                    profile.get("weekend_activity", False),
                    psycopg2.extras.Json(profile.get("frequent_destinations") or []),
                    psycopg2.extras.Json(profile.get("suspicious_destinations") or []),
                    profile.get("privilege_abuse_detected", False),
                    psycopg2.extras.Json(profile.get("behavioral_anomalies") or []),
                    profile.get("summary", ""),
                    profile.get("raw_analysis", ""),
                ))
        except Exception as e:
            print(f"[UserActivityStore] save_profile erro: {e}")

    def save_security_alert(self, host: str, username: str, alert_type: str,
                            severity: str, title: str, details: dict,
                            analysis_id: int | None = None):
        """Persiste alerta de segurança comportamental."""
        sql = """
            INSERT INTO security_alerts
                (host, username, alert_type, severity, title, details, ueba_analysis_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (
                    host, username, alert_type, severity, title,
                    psycopg2.extras.Json(details),
                    analysis_id,
                ))
        except Exception as e:
            print(f"[UserActivityStore] save_security_alert erro: {e}")

    def get_current_risk(self, host: str, username: str) -> int:
        """Risk score actual do utilizador (0 se não existe perfil)."""
        sql = "SELECT risk_score FROM user_profiles WHERE host=%s AND username=%s"
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (host, username))
                row = cur.fetchone()
                return row[0] if row else 0
        except Exception:
            return 0
