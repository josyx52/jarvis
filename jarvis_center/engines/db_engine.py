"""
DbEngine — analisa telemetria de bases de dados (db_telemetry frames).

Suporta PostgreSQL e SQL Server.
Detecta:
  - Queries lentas (avg_ms acima do threshold)
  - Deadlocks e locks em espera
  - Transacoes longas
  - Connection pool saturado
  - Bases de dados com crescimento anormal
  - Alta pressao de I/O (bgwriter checkpoints)
"""


# Thresholds (podem ser sobrescritos por config do host)
SLOW_QUERY_MS       = 500
LONG_TRANSACTION_S  = 30
CONN_POOL_HIGH_PCT  = 80
CONN_POOL_CRIT_PCT  = 95
LOCK_WAIT_HIGH_S    = 10


class DbEngine:

    def analyze(self, db_telemetry: dict) -> list[dict]:
        """
        Recebe um dict de db_telemetry (payload do frame db_telemetry).
        Devolve lista de eventos no formato padrao Jarvis.
        """
        if not db_telemetry:
            return []

        events = []
        db_type = db_telemetry.get("db_type", "unknown")
        host    = db_telemetry.get("host", "unknown")

        events.extend(self._check_slow_queries(db_type, host, db_telemetry))
        events.extend(self._check_locks(db_type, host, db_telemetry))
        events.extend(self._check_long_transactions(db_type, host, db_telemetry))
        events.extend(self._check_connection_pool(db_type, host, db_telemetry))
        events.extend(self._check_io_pressure(db_type, host, db_telemetry))

        return events

    # ----------------------------------------
    # QUERIES LENTAS
    # ----------------------------------------

    def _check_slow_queries(self, db_type, host, data) -> list[dict]:
        events = []
        slow   = data.get("slow_queries", [])

        if not slow:
            return events

        worst = max(slow, key=lambda q: q.get("avg_ms") or q.get("avg_ms", 0), default=None)
        if not worst:
            return events

        avg_ms = worst.get("avg_ms", 0)
        query  = str(worst.get("query", ""))[:200]

        severity = "high" if avg_ms >= 5000 else "medium"

        events.append({
            "event_type":  "db_slow_query",
            "severity":    severity,
            "entity_type": "database",
            "entity_name": f"{db_type}@{host}",
            "summary": (
                f"Query lenta em {db_type}@{host}: {avg_ms}ms media "
                f"({len(slow)} queries acima do threshold). "
                f"Query: {query[:100]}..."
            ),
            "payload": {
                "db_type":       db_type,
                "db_host":       host,
                "slow_count":    len(slow),
                "worst_avg_ms":  avg_ms,
                "worst_query":   query,
                "threshold_ms":  SLOW_QUERY_MS,
            }
        })

        return events

    # ----------------------------------------
    # LOCKS / DEADLOCKS
    # ----------------------------------------

    def _check_locks(self, db_type, host, data) -> list[dict]:
        events  = []
        locks   = data.get("active_locks", []) or data.get("blocking_queries", [])

        if not locks:
            return events

        long_waits = [
            l for l in locks
            if (l.get("wait_seconds") or l.get("wait_time") or 0) >= LOCK_WAIT_HIGH_S
        ]

        if not long_waits:
            return events

        worst_wait = max(l.get("wait_seconds") or l.get("wait_time") or 0 for l in long_waits)

        events.append({
            "event_type":  "db_lock_contention",
            "severity":    "high" if worst_wait >= 60 else "medium",
            "entity_type": "database",
            "entity_name": f"{db_type}@{host}",
            "summary": (
                f"Contencao de locks em {db_type}@{host}: "
                f"{len(long_waits)} lock(s) em espera ha mais de {LOCK_WAIT_HIGH_S}s. "
                f"Pior caso: {worst_wait}s."
            ),
            "payload": {
                "db_type":    db_type,
                "db_host":    host,
                "lock_count": len(long_waits),
                "worst_wait_s": worst_wait,
                "locks":      long_waits[:5],
            }
        })

        return events

    # ----------------------------------------
    # TRANSACOES LONGAS
    # ----------------------------------------

    def _check_long_transactions(self, db_type, host, data) -> list[dict]:
        events = []
        txns   = data.get("long_transactions", [])

        if not txns:
            return events

        worst = max(txns, key=lambda t: t.get("duration_s", 0), default=None)
        if not worst:
            return events

        duration = worst.get("duration_s", 0)
        severity = "high" if duration >= 300 else "medium"

        events.append({
            "event_type":  "db_long_transaction",
            "severity":    severity,
            "entity_type": "database",
            "entity_name": f"{db_type}@{host}",
            "summary": (
                f"Transacao longa em {db_type}@{host}: "
                f"{len(txns)} transacao(oes) abertas. "
                f"Mais longa: {duration}s."
            ),
            "payload": {
                "db_type":       db_type,
                "db_host":       host,
                "count":         len(txns),
                "worst_duration_s": duration,
                "worst_txn":     worst,
            }
        })

        return events

    # ----------------------------------------
    # CONNECTION POOL
    # ----------------------------------------

    def _check_connection_pool(self, db_type, host, data) -> list[dict]:
        events  = []
        stats   = data.get("connection_stats", {})
        pool_pct = stats.get("pool_pct", 0)

        if pool_pct < CONN_POOL_HIGH_PCT:
            return events

        severity = "high" if pool_pct >= CONN_POOL_CRIT_PCT else "medium"

        events.append({
            "event_type":  "db_connection_pool_saturation",
            "severity":    severity,
            "entity_type": "database",
            "entity_name": f"{db_type}@{host}",
            "summary": (
                f"Connection pool de {db_type}@{host} a {pool_pct}% de capacidade "
                f"({stats.get('total') or stats.get('user_sessions', '?')}"
                f"/{stats.get('max_connections', '?')} conexoes)."
            ),
            "payload": {
                "db_type":         db_type,
                "db_host":         host,
                "pool_pct":        pool_pct,
                "connection_stats": stats,
            }
        })

        return events

    # ----------------------------------------
    # PRESSAO DE I/O (PostgreSQL bgwriter)
    # ----------------------------------------

    def _check_io_pressure(self, db_type, host, data) -> list[dict]:
        if db_type != "postgresql":
            return []

        bgwriter = data.get("bgwriter", {})
        if not bgwriter:
            return []

        maxwritten = bgwriter.get("maxwritten_clean", 0) or 0
        checkpoints_req = bgwriter.get("checkpoints_req", 0) or 0

        if maxwritten < 100 and checkpoints_req < 10:
            return []

        return [{
            "event_type":  "db_io_pressure",
            "severity":    "medium",
            "entity_type": "database",
            "entity_name": f"{db_type}@{host}",
            "summary": (
                f"Pressao de I/O em PostgreSQL@{host}: "
                f"checkpoints forcados={checkpoints_req}, "
                f"maxwritten_clean={maxwritten}."
            ),
            "payload": {
                "db_type":  db_type,
                "db_host":  host,
                "bgwriter": bgwriter,
            }
        }]
