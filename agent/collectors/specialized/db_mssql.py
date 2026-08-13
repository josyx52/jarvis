"""
MssqlCollector — coleta metricas de performance do SQL Server.

Conecta via pyodbc e recolhe:
  - Top queries lentas (sys.dm_exec_query_stats)
  - Locks e deadlocks (sys.dm_exec_requests, sys.dm_os_wait_stats)
  - Conexoes e sessoes ativas (sys.dm_exec_sessions)
  - Utilizacao de CPU/memoria do SQL Server
  - Tamanho das bases de dados (sys.databases)

Frame gerado: db_telemetry
"""

import logging
import agent_core.config as cfg

logger = logging.getLogger(__name__)


class MssqlCollector:

    FRAME_TYPE = "db_telemetry"
    DB_TYPE    = "sqlserver"

    def __init__(self):
        self._connections_cfg = cfg._get("collectors.db_mssql.connections", [])

    def collect(self) -> list[dict]:
        frames = []
        for conn_cfg in self._connections_cfg:
            try:
                data = self._collect_one(conn_cfg)
                if data:
                    frames.append(data)
            except Exception as e:
                logger.warning(f"[MSSQL] Erro na conexao {conn_cfg.get('host')}: {e}")
        return frames

    def _collect_one(self, conn_cfg: dict) -> dict | None:
        import pyodbc

        host     = conn_cfg.get("host", "localhost")
        port     = conn_cfg.get("port", 1433)
        user     = conn_cfg.get("user", "")
        password = conn_cfg.get("password", "")
        database = conn_cfg.get("database", "master")
        driver   = conn_cfg.get("driver", "ODBC Driver 17 for SQL Server")

        conn_str = (
            f"DRIVER={{{driver}}};"
            f"SERVER={host},{port};"
            f"DATABASE={database};"
            f"UID={user};PWD={password};"
            f"Connection Timeout=5;"
        )

        conn = pyodbc.connect(conn_str, readonly=True)
        cur  = conn.cursor()

        try:
            slow_queries      = self._slow_queries(cur)
            blocking_queries  = self._blocking_queries(cur)
            long_transactions = self._long_transactions(cur)
            connection_stats  = self._connection_stats(cur)
            db_sizes          = self._db_sizes(cur)
            wait_stats        = self._wait_stats(cur)
        finally:
            cur.close()
            conn.close()

        return {
            "db_type":          self.DB_TYPE,
            "host":             host,
            "port":             port,
            "slow_queries":     slow_queries,
            "blocking_queries": blocking_queries,
            "long_transactions": long_transactions,
            "connection_stats": connection_stats,
            "db_sizes":         db_sizes,
            "wait_stats":       wait_stats,
        }

    def _slow_queries(self, cur) -> list[dict]:
        try:
            cur.execute(f"""
                SELECT TOP 10
                    SUBSTRING(st.text, (qs.statement_start_offset/2)+1,
                        ((CASE qs.statement_end_offset WHEN -1
                          THEN DATALENGTH(st.text)
                          ELSE qs.statement_end_offset END
                          - qs.statement_start_offset)/2)+1) AS query,
                    qs.execution_count AS calls,
                    ROUND(qs.total_elapsed_time / qs.execution_count / 1000.0, 2) AS avg_ms,
                    ROUND(qs.total_elapsed_time / 1000.0, 2) AS total_ms,
                    qs.total_logical_reads / qs.execution_count AS avg_reads
                FROM sys.dm_exec_query_stats qs
                CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
                WHERE qs.execution_count > 0
                  AND (qs.total_elapsed_time / qs.execution_count / 1000.0) > {cfg.THRESH_SLOW_QUERY_MS}
                ORDER BY avg_ms DESC
            """)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            return []

    def _blocking_queries(self, cur) -> list[dict]:
        try:
            cur.execute("""
                SELECT
                    r.session_id,
                    r.blocking_session_id,
                    r.wait_type,
                    r.wait_time / 1000 AS wait_seconds,
                    SUBSTRING(st.text, (r.statement_start_offset/2)+1, 200) AS query,
                    s.status,
                    s.program_name,
                    s.host_name
                FROM sys.dm_exec_requests r
                JOIN sys.dm_exec_sessions s ON r.session_id = s.session_id
                CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st
                WHERE r.blocking_session_id > 0
                ORDER BY r.wait_time DESC
            """)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            return []

    def _long_transactions(self, cur) -> list[dict]:
        try:
            cur.execute(f"""
                SELECT TOP 10
                    s.session_id,
                    s.login_name,
                    s.program_name,
                    s.host_name,
                    s.status,
                    DATEDIFF(SECOND, s.last_request_start_time, GETDATE()) AS duration_s,
                    SUBSTRING(st.text, 1, 200) AS query
                FROM sys.dm_exec_sessions s
                LEFT JOIN sys.dm_exec_requests r ON s.session_id = r.session_id
                OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) st
                WHERE s.is_user_process = 1
                  AND DATEDIFF(SECOND, s.last_request_start_time, GETDATE()) > {cfg.THRESH_LONG_TRANSACTION_S}
                ORDER BY duration_s DESC
            """)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            return []

    def _connection_stats(self, cur) -> dict:
        try:
            cur.execute("""
                SELECT
                    (SELECT value_in_use FROM sys.configurations WHERE name = 'max connections') AS max_connections,
                    (SELECT COUNT(*) FROM sys.dm_exec_sessions WHERE is_user_process = 1) AS user_sessions,
                    (SELECT COUNT(*) FROM sys.dm_exec_requests) AS active_requests
            """)
            row = cur.fetchone()
            if row:
                max_c = row[0] or 32767
                total = row[1] or 0
                return {
                    "max_connections": max_c,
                    "user_sessions":   total,
                    "active_requests": row[2] or 0,
                    "pool_pct":        round(total / max_c * 100, 1),
                }
            return {}
        except Exception:
            return {}

    def _db_sizes(self, cur) -> list[dict]:
        try:
            cur.execute("""
                SELECT
                    name AS database_name,
                    ROUND(SUM(size * 8.0 / 1024), 2) AS size_mb
                FROM sys.master_files
                GROUP BY name
                ORDER BY size_mb DESC
            """)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            return []

    def _wait_stats(self, cur) -> list[dict]:
        try:
            cur.execute("""
                SELECT TOP 10
                    wait_type,
                    wait_time_ms / 1000 AS wait_seconds,
                    waiting_tasks_count
                FROM sys.dm_os_wait_stats
                WHERE wait_type NOT IN (
                    'SLEEP_TASK','WAITFOR','BROKER_TO_FLUSH',
                    'BROKER_TASK_STOP','CLR_AUTO_EVENT','DISPATCHER_QUEUE_SEMAPHORE',
                    'FT_IFTS_SCHEDULER_IDLE_WAIT','HADR_FILESTREAM_IOMGR_IOCOMPLETION',
                    'HADR_WORK_QUEUE','LAZYWRITER_SLEEP','ONDEMAND_TASK_QUEUE',
                    'REQUEST_FOR_DEADLOCK_SEARCH','RESOURCE_QUEUE','SERVER_IDLE_CHECK',
                    'SLEEP_DBSTARTUP','SLEEP_DCOMSTARTUP','SLEEP_MASTERDBREADY',
                    'SLEEP_MASTERMDREADY','SLEEP_MASTERUPGRADED','SLEEP_MSDBSTARTUP',
                    'SLEEP_SYSTEMTASK','SLEEP_TEMPDBSTARTUP','SNI_HTTP_ACCEPT',
                    'SP_SERVER_DIAGNOSTICS_SLEEP','SQLTRACE_BUFFER_FLUSH',
                    'SQLTRACE_INCREMENTAL_FLUSH_SLEEP','WAITFOR',
                    'XE_DISPATCHER_WAIT','XE_TIMER_EVENT'
                )
                ORDER BY wait_time_ms DESC
            """)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            return []
