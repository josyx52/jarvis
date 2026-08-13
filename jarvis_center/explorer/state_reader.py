"""
state_reader — leitura só-leitura do estado já persistido pelo pipeline
existente (snapshots/events/correlations/predictions/alerts), para o
Explorer Engine consumir sem duplicar nenhuma lógica de detecção.

Nota de arquitectura: CentralRuntime (onde vivem BaselineEngine/Correlator/
Predictor em memória) corre uma instância por partição, cada uma no seu
próprio processo (run_processor.py) — não é acessível directamente a partir
do Explorer, que corre como thread do processo do Center (start_center.py).
Por isso este módulo lê a versão já gravada em Postgres (por MemoryStore,
storage/memory_store.py) em vez de tentar aceder aos objectos em memória.
"""

import os

import psycopg2
import psycopg2.extras


def _conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        port     = os.getenv("POSTGRES_PORT",     "5432"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


def get_recent_snapshot(host_key: str) -> dict | None:
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT host, snapshot_time, cpu_percent, memory_percent, disk_percent,
                   memory_used, disk_used, disk_free, process_count, service_count,
                   network_count, created_at
            FROM snapshots
            WHERE host = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (host_key,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _recent(table: str, columns: str, host_key: str, limit: int) -> list[dict]:
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"""
            SELECT {columns}
            FROM {table}
            WHERE host = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (host_key, limit),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_recent_predictions(host_key: str, limit: int = 10) -> list[dict]:
    return _recent("predictions", "prediction_type, severity, summary, payload, created_at", host_key, limit)


def get_recent_correlations(host_key: str, limit: int = 10) -> list[dict]:
    return _recent("correlations", "correlation_type, severity, summary, payload, created_at", host_key, limit)


def get_recent_events(host_key: str, limit: int = 20) -> list[dict]:
    return _recent("events", "event_type, severity, payload, created_at", host_key, limit)


def get_recent_alerts(host_key: str, limit: int = 10) -> list[dict]:
    return _recent("alerts", "severity, title, payload, created_at", host_key, limit)


def build_evidence(host_key: str) -> dict:
    """Reúne tudo o que o Explorer precisa para avaliar 1 alvo."""
    return {
        "snapshot":     get_recent_snapshot(host_key),
        "predictions":  get_recent_predictions(host_key),
        "correlations": get_recent_correlations(host_key),
        "events":       get_recent_events(host_key),
        "alerts":       get_recent_alerts(host_key),
    }
