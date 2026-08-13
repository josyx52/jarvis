"""
PostgresCollector — coleta metricas de performance do PostgreSQL.

Conecta diretamente ao PostgreSQL (utilizador de leitura) e recolhe:
  - Top queries lentas (pg_stat_statements)
  - Locks e deadlocks ativos (pg_locks)
  - Conexoes ativas e transacoes longas (pg_stat_activity)
  - Utilizacao de connection pool (max_connections vs ativos)
  - Tamanho das bases de dados (pg_database_size)
  - Checkpoints e pressao de I/O (pg_stat_bgwriter)

Frame gerado: db_telemetry
"""

import logging
import agent_core.config as cfg

logger = logging.getLogger(__name__)


class PostgresCollector:

    FRAME_TYPE = "db_telemetry"
    DB_TYPE    = "postgresql"

    def __init__(self):
        self._connections_cfg = cfg._get("collectors.db_postgres.connections", [])

    def collect(self) -> list[dict]:
        """Devolve lista de frames db_telemetry, um por conexao configurada."""
        frames = []
        for conn_cfg in self._connections_cfg:
            try:
                data = self._collect_one(conn_cfg)
                if data:
                    frames.append(data)
            except Exception as e:
                logger.warning(f"[POSTGRES] Erro na conexao {conn_cfg.get('host')}: {e}")
        return frames

    def _collect_one(self, conn_cfg: dict) -> dict | None:
        import psycopg2
        import psycopg2.extras

        host     = conn_cfg.get("host", "localhost")
        port     = conn_cfg.get("port", 5432)
        user     = conn_cfg.get("user", "")
        password = conn_cfg.get("password", "")
        dbname   = conn_cfg.get("databases", "postgres")

        if isinstance(dbname, list):
            dbname = dbname[0]
        if dbname == "all":
            dbname = "postgres"

        conn = psycopg2.connect(
            host=host, port=port,
            user=user, password=password,
            dbname=dbname,
            connect_timeout=5,
        )
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        try:
            slow_queries      = self._slow_queries(cur)
            active_locks      = self._active_locks(cur)
            long_transactions = self._long_transactions(cur)
            connection_stats  = self._connection_stats(cur)
            db_sizes          = self._db_sizes(cur)
            bgwriter          = self._bgwriter(cur)
        finally:
            cur.close()
            conn.close()

        return {
            "db_type":         self.DB_TYPE,
            "host":            host,
            "port":            port,
            "slow_queries":    slow_queries,
            "active_locks":    active_locks,
            "long_transactions": long_transactions,
            "connection_stats": connection_stats,
            "db_sizes":        db_sizes,
            "bgwriter":        bgwriter,
        }

    # ----------------------------------------
    # QUERIES LENTAS (pg_stat_statements)
    # ----------------------------------------

    def _slow_queries(self, cur) -> list[dict]:
        try:
            cur.execute("""
                SELECT
                    query,
                    calls,
                    ROUND((total_exec_time / calls)::numeric, 2) AS avg_ms,
                    ROUND(total_exec_time::numeric, 2) AS total_ms,
                    rows
                FROM pg_stat_statements
                WHERE calls > 0
                  AND (total_exec_time / calls) > %s
                ORDER BY avg_ms DESC
                LIMIT 10
            """, (cfg.THRESH_SLOW_QUERY_MS,))
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    # ----------------------------------------
    # LOCKS ATIVOS
    # ----------------------------------------

    def _active_locks(self, cur) -> list[dict]:
        try:
            cur.execute("""
                SELECT
                    l.pid,
                    l.locktype,
                    l.mode,
                    l.granted,
                    a.query,
                    a.state,
                    EXTRACT(EPOCH FROM (NOW() - a.query_start))::int AS wait_seconds
                FROM pg_locks l
                JOIN pg_stat_activity a ON l.pid = a.pid
                WHERE NOT l.granted
                ORDER BY wait_seconds DESC
                LIMIT 20
            """)
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    # ----------------------------------------
    # TRANSACOES LONGAS
    # ----------------------------------------

    def _long_transactions(self, cur) -> list[dict]:
        try:
            cur.execute("""
                SELECT
                    pid,
                    usename,
                    application_name,
                    client_addr::text,
                    state,
                    query,
                    EXTRACT(EPOCH FROM (NOW() - xact_start))::int AS duration_s
                FROM pg_stat_activity
                WHERE xact_start IS NOT NULL
                  AND state != 'idle'
                  AND EXTRACT(EPOCH FROM (NOW() - xact_start)) > %s
                ORDER BY duration_s DESC
                LIMIT 10
            """, (cfg.THRESH_LONG_TRANSACTION_S,))
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    # ----------------------------------------
    # CONEXOES / CONNECTION POOL
    # ----------------------------------------

    def _connection_stats(self, cur) -> dict:
        try:
            cur.execute("SHOW max_connections")
            max_conn = int(cur.fetchone()["max_connections"])

            cur.execute("SELECT COUNT(*) AS active FROM pg_stat_activity WHERE state != 'idle'")
            active = cur.fetchone()["active"]

            cur.execute("SELECT COUNT(*) AS total FROM pg_stat_activity")
            total = cur.fetchone()["total"]

            return {
                "max_connections": max_conn,
                "total":           total,
                "active":          active,
                "pool_pct":        round(total / max_conn * 100, 1) if max_conn else 0,
            }
        except Exception:
            return {}

    # ----------------------------------------
    # TAMANHO DAS BASES DE DADOS
    # ----------------------------------------

    def _db_sizes(self, cur) -> list[dict]:
        try:
            cur.execute("""
                SELECT
                    datname AS database,
                    pg_size_pretty(pg_database_size(datname)) AS size,
                    pg_database_size(datname) AS size_bytes
                FROM pg_database
                WHERE datistemplate = false
                ORDER BY size_bytes DESC
            """)
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    # ----------------------------------------
    # BGWRITER (pressao de I/O)
    # ----------------------------------------

    def _bgwriter(self, cur) -> dict:
        try:
            cur.execute("""
                SELECT
                    checkpoints_timed,
                    checkpoints_req,
                    buffers_checkpoint,
                    buffers_clean,
                    buffers_backend,
                    maxwritten_clean
                FROM pg_stat_bgwriter
            """)
            row = cur.fetchone()
            return dict(row) if row else {}
        except Exception:
            return {}
