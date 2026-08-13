ALTER TABLE snapshots
    ADD COLUMN IF NOT EXISTS boot_id TEXT,
    ADD COLUMN IF NOT EXISTS os TEXT,
    ADD COLUMN IF NOT EXISTS os_version TEXT,
    ADD COLUMN IF NOT EXISTS ip TEXT,
    ADD COLUMN IF NOT EXISTS agent_version TEXT,
    ADD COLUMN IF NOT EXISTS env TEXT,
    ADD COLUMN IF NOT EXISTS snapshot_time DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cpu_percent DOUBLE PRECISION DEFAULT 0,
    ADD COLUMN IF NOT EXISTS memory_percent DOUBLE PRECISION DEFAULT 0,
    ADD COLUMN IF NOT EXISTS disk_percent DOUBLE PRECISION DEFAULT 0,
    ADD COLUMN IF NOT EXISTS memory_used BIGINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS disk_used BIGINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS disk_free BIGINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS net_bytes_sent BIGINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS net_bytes_recv BIGINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS process_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS service_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS network_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS trace_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS log_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS event_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS top_cpu_process TEXT,
    ADD COLUMN IF NOT EXISTS top_cpu_value DOUBLE PRECISION DEFAULT 0,
    ADD COLUMN IF NOT EXISTS top_memory_process TEXT,
    ADD COLUMN IF NOT EXISTS top_memory_value BIGINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS raw_json JSONB,
    ADD COLUMN IF NOT EXISTS snapshot_json JSONB;

ALTER TABLE snapshots
    ALTER COLUMN snapshot_json DROP NOT NULL;

UPDATE snapshots
SET snapshot_json = raw_json
WHERE snapshot_json IS NULL
  AND raw_json IS NOT NULL;

ALTER TABLE correlations
    ADD COLUMN IF NOT EXISTS summary TEXT,
    ADD COLUMN IF NOT EXISTS payload JSONB;

ALTER TABLE predictions
    ADD COLUMN IF NOT EXISTS summary TEXT,
    ADD COLUMN IF NOT EXISTS payload JSONB;

ALTER TABLE investigations
    ADD COLUMN IF NOT EXISTS summary TEXT,
    ADD COLUMN IF NOT EXISTS payload JSONB;

CREATE TABLE IF NOT EXISTS reasoning (
    id BIGSERIAL PRIMARY KEY,
    host TEXT NOT NULL,
    summary TEXT,
    details TEXT,
    payload JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_snapshots_host_time
    ON snapshots(host, snapshot_time DESC);

CREATE INDEX IF NOT EXISTS idx_events_host_created
    ON events(host, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_alerts_host_created
    ON alerts(host, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_predictions_host_created
    ON predictions(host, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_investigations_host_created
    ON investigations(host, created_at DESC);

-- Tabela de traces OpenTelemetry (auto-instrumentacao)
CREATE TABLE IF NOT EXISTS traces (
    id          BIGSERIAL PRIMARY KEY,
    host        TEXT NOT NULL,
    service     TEXT,
    operation   TEXT,
    trace_id    TEXT,
    span_id     TEXT,
    duration_ms INTEGER DEFAULT 0,
    status      TEXT,
    attributes  JSONB,
    spans_json  JSONB,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_traces_host_created
    ON traces(host, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_traces_service
    ON traces(service, created_at DESC);

-- -----------------------------------------------
-- Telemetria especializada (collectors v0.3.0)
-- -----------------------------------------------

-- Database telemetry (PostgreSQL, SQL Server, Oracle, MySQL)
CREATE TABLE IF NOT EXISTS db_telemetry (
    id              BIGSERIAL PRIMARY KEY,
    host            TEXT NOT NULL,
    db_type         TEXT NOT NULL,
    db_host         TEXT,
    db_port         INTEGER,
    slow_queries    JSONB,
    active_locks    JSONB,
    long_transactions JSONB,
    connection_stats  JSONB,
    db_sizes        JSONB,
    raw_payload     JSONB,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_db_telemetry_host_created
    ON db_telemetry(host, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_db_telemetry_type
    ON db_telemetry(db_type, created_at DESC);

-- Kafka / Message Queue telemetry
CREATE TABLE IF NOT EXISTS kafka_telemetry (
    id              BIGSERIAL PRIMARY KEY,
    host            TEXT NOT NULL,
    brokers         JSONB,
    topics          JSONB,
    consumer_lag    JSONB,
    raw_payload     JSONB,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kafka_telemetry_host_created
    ON kafka_telemetry(host, created_at DESC);

-- Web server telemetry (Nginx, IIS, Apache, HAProxy, ...)
CREATE TABLE IF NOT EXISTS web_telemetry (
    id                  BIGSERIAL PRIMARY KEY,
    host                TEXT NOT NULL,
    web_type            TEXT,
    active_connections  INTEGER DEFAULT 0,
    requests            BIGINT DEFAULT 0,
    requests_delta      INTEGER DEFAULT 0,
    error_count         INTEGER DEFAULT 0,
    raw_payload         JSONB,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_web_telemetry_host_created
    ON web_telemetry(host, created_at DESC);

-- Discovery events (mudancas detetadas pelo DiscoveryEngine)
CREATE TABLE IF NOT EXISTS discovery_events (
    id          BIGSERIAL PRIMARY KEY,
    host        TEXT NOT NULL,
    change_type TEXT,
    change_name TEXT,
    action      TEXT,
    ai_analysis TEXT,
    payload     JSONB,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discovery_host_created
    ON discovery_events(host, created_at DESC);

-- Server profiles (configuracao por servidor)
CREATE TABLE IF NOT EXISTS server_profiles (
    id              BIGSERIAL PRIMARY KEY,
    host            TEXT NOT NULL UNIQUE,
    profile         TEXT,
    setup_by        TEXT,
    setup_at        TIMESTAMP,
    collectors      JSONB,
    thresholds      JSONB,
    raw_config      JSONB,
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_server_profiles_host
    ON server_profiles(host);