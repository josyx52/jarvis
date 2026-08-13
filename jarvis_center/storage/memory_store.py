import json
import os

import psycopg2


class MemoryStore:

    def __init__(self):

        self.conn = psycopg2.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            database=os.getenv("POSTGRES_DB", "jarvis"),
            user=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", "")
        )

        self.conn.autocommit = True
        self.cursor = self.conn.cursor()

        self._columns_cache = {}

    # ================================
    # HELPERS
    # ================================

    @staticmethod
    def _strip_nulls(value):
        """Remove null bytes que o PostgreSQL rejeita em campos text/jsonb."""
        if isinstance(value, str):
            return value.replace("\x00", "")
        return value

    def _safe_json(self, value):
        try:
            s = json.dumps(value)
            # PostgreSQL rejeita null bytes (\u0000) em JSON/text
            return s.replace("\\u0000", "").replace("\x00", "")
        except Exception:
            return json.dumps(str(value))

    def _to_int(self, value, default=0):
        try:
            if value is None or value == "":
                return default
            return int(float(value))
        except Exception:
            return default

    def _to_float(self, value, default=0.0):
        try:
            if value is None or value == "":
                return default
            return float(value)
        except Exception:
            return default

    def _table_columns(self, table_name: str) -> set[str]:
        if table_name in self._columns_cache:
            return self._columns_cache[table_name]

        cur = self.conn.cursor()
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
        """, (table_name,))
        columns = {row[0] for row in cur.fetchall()}
        cur.close()

        self._columns_cache[table_name] = columns
        return columns

    def _best_snapshot_time(self, snapshot: dict):
        metrics = snapshot.get("metrics", {}) or {}
        return metrics.get("timestamp") or snapshot.get("timestamp")

    def _top_cpu(self, snapshot: dict):
        cpu_items = snapshot.get("cpu", []) or []

        if not isinstance(cpu_items, list) or not cpu_items:
            return None, 0.0

        best_name = None
        best_value = 0.0

        for item in cpu_items:
            if not isinstance(item, dict):
                continue

            value = self._to_float(
                item.get("cpu_percent")
                or item.get("percent_processor_time")
                or 0
            )

            if value > best_value:
                best_value = value
                best_name = item.get("name")

        return best_name, best_value

    def _top_memory(self, snapshot: dict):
        memory_items = snapshot.get("memory", []) or []

        if not isinstance(memory_items, list) or not memory_items:
            return None, 0

        best_name = None
        best_value = 0

        for item in memory_items:
            if not isinstance(item, dict):
                continue

            value = self._to_int(
                item.get("resident_size")
                or item.get("total_size")
                or 0
            )

            if value > best_value:
                best_value = value
                best_name = item.get("name")

        return best_name, best_value

    def _snapshot_metrics(self, snapshot: dict):
        metrics = snapshot.get("metrics", {}) or {}
        disk_items = snapshot.get("disk", []) or []

        cpu_percent = self._to_float(metrics.get("cpu_percent"), 0.0)
        memory_percent = self._to_float(metrics.get("memory_percent"), 0.0)
        memory_used = self._to_int(metrics.get("memory_used"), 0)
        disk_percent = self._to_float(metrics.get("disk_percent"), 0.0)
        disk_used = self._to_int(metrics.get("disk_used"), 0)
        disk_free = self._to_int(metrics.get("disk_free"), 0)
        net_bytes_sent = self._to_int(metrics.get("net_bytes_sent"), 0)
        net_bytes_recv = self._to_int(metrics.get("net_bytes_recv"), 0)

        if isinstance(disk_items, list) and disk_items:
            total_size = 0
            total_free = 0
            has_any = False

            for item in disk_items:
                if not isinstance(item, dict):
                    continue

                free_space = self._to_int(
                    item.get("free_space")
                    or item.get("disk_free")
                    or item.get("free")
                    or 0
                )
                size = self._to_int(
                    item.get("size")
                    or item.get("total_space")
                    or item.get("disk_total")
                    or 0
                )
                used = self._to_int(
                    item.get("used_space")
                    or item.get("disk_used")
                    or item.get("used")
                    or 0
                )

                if size > 0:
                    has_any = True
                    total_size += size
                    total_free += free_space

                if disk_used == 0 and used > 0:
                    disk_used += used

            if has_any and total_size > 0:
                if disk_free == 0:
                    disk_free = total_free
                if disk_used == 0:
                    disk_used = max(total_size - total_free, 0)
                if disk_percent == 0:
                    disk_percent = ((total_size - total_free) / total_size) * 100.0

        return {
            "snapshot_time": self._best_snapshot_time(snapshot),
            "cpu_percent": cpu_percent,
            "memory_percent": memory_percent,
            "memory_used": memory_used,
            "disk_percent": disk_percent,
            "disk_used": disk_used,
            "disk_free": disk_free,
            "net_bytes_sent": net_bytes_sent,
            "net_bytes_recv": net_bytes_recv
        }

    # ================================
    # SNAPSHOT
    # ================================
    def save_snapshot(self, host: str, snapshot: dict):

        try:

            if not snapshot or not isinstance(snapshot, dict):
                print("[memory_store WARNING] snapshot inválido")
                return

            table_columns = self._table_columns("snapshots")

            host_info = snapshot.get("host", {}) or {}
            metrics = self._snapshot_metrics(snapshot)

            top_cpu_name, top_cpu_value = self._top_cpu(snapshot)
            top_mem_name, top_mem_value = self._top_memory(snapshot)

            snapshot_json = self._safe_json(snapshot)

            payload = {
                "host": host,
                "boot_id": host_info.get("boot_id"),
                "os": host_info.get("os"),
                "os_version": host_info.get("os_version"),
                "ip": host_info.get("ip"),
                "agent_version": host_info.get("agent_version"),
                "env": host_info.get("env"),
                "snapshot_time": metrics["snapshot_time"],
                "cpu_percent": metrics["cpu_percent"],
                "memory_percent": metrics["memory_percent"],
                "disk_percent": metrics["disk_percent"],
                "memory_used": metrics["memory_used"],
                "disk_used": metrics["disk_used"],
                "disk_free": metrics["disk_free"],
                "net_bytes_sent": metrics["net_bytes_sent"],
                "net_bytes_recv": metrics["net_bytes_recv"],
                "process_count": len(snapshot.get("processes", []) or []),
                "service_count": len(snapshot.get("services", []) or []),
                "network_count": len(snapshot.get("network", []) or []),
                "connection_count": len(snapshot.get("connections", []) or []),
                "log_count": len(snapshot.get("logs", []) or []),
                "event_count": len(snapshot.get("events", []) or []),
                "top_cpu_process": top_cpu_name,
                "top_cpu_value": top_cpu_value,
                "top_memory_process": top_mem_name,
                "top_memory_value": top_mem_value,
                "snapshot_json": snapshot_json,
            }

            valid_columns = [c for c in payload.keys() if c in table_columns]
            if not valid_columns:
                raise RuntimeError("Tabela snapshots não possui colunas compatíveis para insert")

            sql = f"""
                INSERT INTO snapshots ({", ".join(valid_columns)})
                VALUES ({", ".join(["%s"] * len(valid_columns))})
            """

            values = [payload[c] for c in valid_columns]
            self.cursor.execute(sql, values)

        except Exception as e:
            print("[memory_store ERROR][snapshot]", e)

    # ================================
    # EVENTS
    # ================================
    def save_event(self, host, event):

        try:
            self.cursor.execute("""
                INSERT INTO events (
                    host,
                    event_type,
                    severity,
                    payload
                )
                VALUES (%s, %s, %s, %s)
            """, (
                host,
                event.get("event_type") or event.get("type"),
                event.get("severity"),
                self._safe_json(event)
            ))

        except Exception as e:
            print("[memory_store ERROR][event]", e)

    def save_events(self, host, events):
        for event in events or []:
            self.save_event(host, event)

    # ================================
    # CORRELATIONS
    # ================================
    def save_correlation(self, host, correlation):

        try:
            self.cursor.execute("""
                INSERT INTO correlations (
                    host,
                    correlation_type,
                    severity,
                    summary,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s)
            """, (
                host,
                correlation.get("correlation_type") or correlation.get("type"),
                correlation.get("severity"),
                correlation.get("summary"),
                self._safe_json(correlation)
            ))

        except Exception as e:
            print("[memory_store ERROR][correlation]", e)

    def save_correlations(self, host, correlations):
        for correlation in correlations or []:
            self.save_correlation(host, correlation)

    # ================================
    # PREDICTIONS
    # ================================
    def save_prediction(self, host, prediction):

        try:
            self.cursor.execute("""
                INSERT INTO predictions (
                    host,
                    prediction_type,
                    severity,
                    summary,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s)
            """, (
                host,
                prediction.get("prediction_type") or prediction.get("type"),
                prediction.get("severity"),
                prediction.get("summary"),
                self._safe_json(prediction)
            ))

        except Exception as e:
            print("[memory_store ERROR][prediction]", e)

    def save_predictions(self, host, predictions):
        for prediction in predictions or []:
            self.save_prediction(host, prediction)

    # ================================
    # ALERTS
    # ================================
    def save_alert(self, host, alert):

        try:
            self.cursor.execute("""
                INSERT INTO alerts (
                    host,
                    severity,
                    title,
                    payload
                )
                VALUES (%s, %s, %s, %s)
            """, (
                host,
                alert.get("severity"),
                alert.get("title"),
                self._safe_json(alert)
            ))

        except Exception as e:
            print("[memory_store ERROR][alert]", e)

    def save_alerts(self, host, alerts):
        for alert in alerts or []:
            self.save_alert(host, alert)

    # ================================
    # INVESTIGATIONS
    # ================================
    def save_investigation(self, host, investigation, alert_id=None):
        """Grava uma investigação. Devolve {id, created_at} se alert_id for passado
        (disparo manual, ver POST /investigations/trigger), para o endpoint poder
        devolver a linha gravada ao frontend sem precisar de reler a tabela."""

        try:
            self.cursor.execute("""
                INSERT INTO investigations (
                    host,
                    severity,
                    summary,
                    details,
                    payload,
                    alert_id
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
            """, (
                host,
                investigation.get("severity"),
                investigation.get("summary") or investigation.get("title"),
                investigation.get("details"),
                self._safe_json(investigation),
                alert_id,
            ))
            row = self.cursor.fetchone()
            return {"id": row[0], "created_at": row[1]} if row else None

        except Exception as e:
            print("[memory_store ERROR][investigation]", e)
            return None

    def save_investigations(self, host, investigations):
        for investigation in investigations or []:
            self.save_investigation(host, investigation)

    # ================================
    # TRACES (OpenTelemetry)
    # ================================
    def save_trace(self, host: str, span: dict):
        try:
            self.cursor.execute("""
                INSERT INTO traces (
                    host, service, operation, trace_id, span_id,
                    duration_ms, status, attributes, spans_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                host,
                self._strip_nulls(span.get("service")),
                self._strip_nulls(span.get("name")),
                self._strip_nulls(span.get("trace_id")),
                self._strip_nulls(span.get("span_id")),
                span.get("duration_ms", 0),
                self._strip_nulls(span.get("status")),
                self._safe_json(span.get("attributes", {})),
                self._safe_json(span),
            ))
        except Exception as e:
            print("[memory_store ERROR][trace]", e)

    def save_traces(self, host: str, spans: list):
        for span in spans or []:
            self.save_trace(host, span)

    # ================================
    # EXECUTION UNITS (Network Probe)
    # ================================

    def save_execution_unit(self, host: str, unit: dict):
        try:
            self._ensure_execution_units_table()
            self.cursor.execute("""
                INSERT INTO execution_units (
                    host, unit_id, pid, process_name,
                    trace_id, confidence, duration_ms,
                    event_count, protocols, sources,
                    first_ts, last_ts, events_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (unit_id) DO NOTHING
            """, (
                host,
                unit.get("unit_id", ""),
                self._to_int(unit.get("pid"), 0),
                unit.get("process_name", ""),
                unit.get("trace_id") or "",
                unit.get("confidence", "low"),
                self._to_float(unit.get("duration_ms"), 0.0),
                self._to_int(unit.get("event_count"), 0),
                unit.get("protocols", []),
                unit.get("sources", []),
                self._to_float(unit.get("first_ts"), 0.0),
                self._to_float(unit.get("last_ts"), 0.0),
                self._safe_json(unit.get("events", [])),
            ))
        except Exception as e:
            print("[memory_store ERROR][execution_unit]", e)

    def save_execution_units(self, host: str, units: list):
        for unit in units or []:
            self.save_execution_unit(host, unit)

    def _ensure_execution_units_table(self):
        if getattr(self, "_eu_table_checked", False):
            return
        try:
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS execution_units (
                    id           SERIAL PRIMARY KEY,
                    host         TEXT,
                    unit_id      TEXT UNIQUE,
                    pid          INTEGER,
                    process_name TEXT,
                    trace_id     TEXT,
                    confidence   TEXT,
                    duration_ms  FLOAT,
                    event_count  INTEGER,
                    protocols    TEXT[],
                    sources      TEXT[],
                    first_ts     FLOAT,
                    last_ts      FLOAT,
                    events_json  JSONB,
                    created_at   TIMESTAMP DEFAULT NOW()
                )
            """)
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_eu_host       ON execution_units (host)",
                "CREATE INDEX IF NOT EXISTS idx_eu_trace_id   ON execution_units (trace_id)",
                "CREATE INDEX IF NOT EXISTS idx_eu_process    ON execution_units (process_name)",
                "CREATE INDEX IF NOT EXISTS idx_eu_confidence ON execution_units (confidence)",
                "CREATE INDEX IF NOT EXISTS idx_eu_created_at ON execution_units (created_at DESC)",
            ]:
                self.cursor.execute(idx_sql)
            self._eu_table_checked = True
        except Exception as e:
            print("[memory_store] execution_units table check:", e)

    # ================================
    # PROBLEMS
    # ================================

    def save_problem(self, host: str, problem: dict):
        try:
            self._ensure_problems_table()
            self.cursor.execute("""
                INSERT INTO problems (
                    problem_id, host, title, severity, status,
                    alert_count, alert_titles, events_count, correlations_count,
                    opened_at, last_seen, resolved_at, duration_s, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        to_timestamp(%s), to_timestamp(%s), to_timestamp(%s), %s, %s)
                ON CONFLICT (problem_id) DO UPDATE SET
                    severity            = EXCLUDED.severity,
                    status              = EXCLUDED.status,
                    alert_count         = EXCLUDED.alert_count,
                    alert_titles        = EXCLUDED.alert_titles,
                    events_count        = EXCLUDED.events_count,
                    correlations_count  = EXCLUDED.correlations_count,
                    last_seen           = EXCLUDED.last_seen,
                    resolved_at         = EXCLUDED.resolved_at,
                    duration_s          = EXCLUDED.duration_s,
                    payload             = EXCLUDED.payload
            """, (
                problem.get("problem_id"),
                host,
                problem.get("title"),
                problem.get("severity"),
                problem.get("status"),
                problem.get("alert_count", 0),
                problem.get("alert_titles", []),
                problem.get("events_count", 0),
                problem.get("correlations_count", 0),
                problem.get("opened_at"),
                problem.get("last_seen"),
                problem.get("resolved_at"),
                problem.get("duration_s"),
                self._safe_json(problem),
            ))
        except Exception as e:
            print("[memory_store ERROR][problem]", e)

    def save_problems(self, host: str, problems: list):
        for problem in problems or []:
            self.save_problem(host, problem)

    def _ensure_problems_table(self):
        if getattr(self, "_problems_table_checked", False):
            return
        try:
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS problems (
                    id                 SERIAL PRIMARY KEY,
                    problem_id         TEXT UNIQUE NOT NULL,
                    host               TEXT,
                    title              TEXT,
                    severity           TEXT,
                    status             TEXT,
                    alert_count        INTEGER DEFAULT 0,
                    alert_titles       TEXT[],
                    events_count       INTEGER DEFAULT 0,
                    correlations_count INTEGER DEFAULT 0,
                    opened_at          TIMESTAMP,
                    last_seen          TIMESTAMP,
                    resolved_at        TIMESTAMP,
                    duration_s         FLOAT,
                    payload            JSONB,
                    created_at         TIMESTAMP DEFAULT NOW()
                )
            """)
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_problems_host      ON problems (host)",
                "CREATE INDEX IF NOT EXISTS idx_problems_status    ON problems (status)",
                "CREATE INDEX IF NOT EXISTS idx_problems_severity  ON problems (severity)",
                "CREATE INDEX IF NOT EXISTS idx_problems_opened_at ON problems (opened_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_problems_last_seen ON problems (last_seen DESC)",
            ]:
                self.cursor.execute(idx_sql)
            self._problems_table_checked = True
        except Exception as e:
            print("[memory_store] problems table check:", e)

    # ================================
    # INSTRUMENTATION RECOMMENDATIONS
    # ================================

    _instrumentation_table_checked = False

    def save_instrumentation_recommendations(self, host: str, recs: list[dict]):
        self._ensure_instrumentation_table()
        for rec in recs:
            try:
                self.cursor.execute("""
                    INSERT INTO instrumentation_recommendations
                        (host, pid, process_name, technology, priority, reason, source, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT DO NOTHING
                """, (
                    host,
                    rec.get("pid"),
                    self._strip_nulls(rec.get("process_name", "")),
                    rec.get("technology", ""),
                    rec.get("priority", "normal"),
                    self._strip_nulls(rec.get("reason", "")),
                    rec.get("source", "ai"),
                ))
            except Exception as e:
                print("[memory_store] instrumentation insert:", e)

    def get_pending_instrumentation(self, host: str) -> list[dict]:
        self._ensure_instrumentation_table()
        try:
            self.cursor.execute("""
                SELECT id, host, pid, process_name, technology, priority, reason, source
                FROM instrumentation_recommendations
                WHERE host = %s AND status = 'pending'
                ORDER BY
                    CASE priority WHEN 'urgent' THEN 0 ELSE 1 END,
                    created_at ASC
                LIMIT 10
            """, (host,))
            cols = [d[0] for d in self.cursor.description]
            return [dict(zip(cols, row)) for row in self.cursor.fetchall()]
        except Exception as e:
            print("[memory_store] get_pending_instrumentation:", e)
            return []

    def mark_instrumentation_applied(self, rec_id: int, status: str = "applied"):
        self._ensure_instrumentation_table()
        try:
            self.cursor.execute("""
                UPDATE instrumentation_recommendations
                SET status = %s, applied_at = NOW()
                WHERE id = %s
            """, (status, rec_id))
        except Exception as e:
            print("[memory_store] mark_instrumentation_applied:", e)

    def _ensure_instrumentation_table(self):
        if self._instrumentation_table_checked:
            return
        try:
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS instrumentation_recommendations (
                    id           SERIAL PRIMARY KEY,
                    host         TEXT NOT NULL,
                    pid          INTEGER,
                    process_name TEXT,
                    technology   TEXT,
                    priority     TEXT DEFAULT 'normal',
                    reason       TEXT,
                    source       TEXT DEFAULT 'ai',
                    status       TEXT DEFAULT 'pending',
                    created_at   TIMESTAMP DEFAULT NOW(),
                    applied_at   TIMESTAMP
                )
            """)
            self.cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_instr_host_status "
                "ON instrumentation_recommendations (host, status)"
            )
            self._instrumentation_table_checked = True
        except Exception as e:
            print("[memory_store] instrumentation table check:", e)

    # ================================
    # FLUSH
    # ================================
    def flush(self):
        return

    # ================================
    # HEALTH CHECK
    # ================================
    def health_check(self):

        try:
            self.cursor.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ================================
    # CLOSE
    # ================================
    def close(self):

        try:
            self.cursor.close()
            self.conn.close()
        except Exception:
            pass