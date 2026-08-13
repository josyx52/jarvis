-- =============================================================
-- JARVIS CENTER — Schema completo (idempotente)
-- Pode ser re-executado sem perda de dados
-- =============================================================

-- -----------------------------------------------
-- Tabela base: snapshots
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    host                TEXT NOT NULL,
    boot_id             TEXT,
    os                  TEXT,
    os_version          TEXT,
    ip                  TEXT,
    agent_version       TEXT,
    env                 TEXT,
    snapshot_time       DOUBLE PRECISION,
    cpu_percent         DOUBLE PRECISION DEFAULT 0,
    memory_percent      DOUBLE PRECISION DEFAULT 0,
    disk_percent        DOUBLE PRECISION DEFAULT 0,
    memory_used         BIGINT DEFAULT 0,
    disk_used           BIGINT DEFAULT 0,
    disk_free           BIGINT DEFAULT 0,
    net_bytes_sent      BIGINT DEFAULT 0,
    net_bytes_recv      BIGINT DEFAULT 0,
    process_count       INTEGER DEFAULT 0,
    service_count       INTEGER DEFAULT 0,
    network_count       INTEGER DEFAULT 0,
    trace_count         INTEGER DEFAULT 0,
    log_count           INTEGER DEFAULT 0,
    event_count         INTEGER DEFAULT 0,
    top_cpu_process     TEXT,
    top_cpu_value       DOUBLE PRECISION DEFAULT 0,
    top_memory_process  TEXT,
    top_memory_value    BIGINT DEFAULT 0,
    raw_json            JSONB,
    snapshot_json       JSONB,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_snapshots_host_time
    ON snapshots(host, snapshot_time DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_host_created
    ON snapshots(host, created_at DESC);

-- -----------------------------------------------
-- Tabela base: events
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL PRIMARY KEY,
    host            TEXT NOT NULL,
    event_type      TEXT,
    severity        TEXT,
    summary         TEXT,
    payload         JSONB,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_host_created
    ON events(host, created_at DESC);

-- -----------------------------------------------
-- Tabela base: correlations
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS correlations (
    id                  BIGSERIAL PRIMARY KEY,
    host                TEXT NOT NULL,
    correlation_type    TEXT,
    severity            TEXT,
    summary             TEXT,
    payload             JSONB,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_correlations_host_created
    ON correlations(host, created_at DESC);

-- -----------------------------------------------
-- Tabela base: predictions
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS predictions (
    id                  BIGSERIAL PRIMARY KEY,
    host                TEXT NOT NULL,
    prediction_type     TEXT,
    severity            TEXT,
    summary             TEXT,
    payload             JSONB,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_predictions_host_created
    ON predictions(host, created_at DESC);

-- -----------------------------------------------
-- Tabela base: alerts
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    host            TEXT NOT NULL,
    severity        TEXT,
    title           TEXT,
    payload         JSONB,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_host_created
    ON alerts(host, created_at DESC);

-- -----------------------------------------------
-- Tabela base: investigations
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS investigations (
    id              BIGSERIAL PRIMARY KEY,
    host            TEXT NOT NULL,
    severity        TEXT,
    summary         TEXT,
    details         TEXT,
    payload         JSONB,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_investigations_host_created
    ON investigations(host, created_at DESC);

-- -----------------------------------------------
-- Tabela: reasoning (análise LLM)
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS reasoning (
    id          BIGSERIAL PRIMARY KEY,
    host        TEXT NOT NULL,
    summary     TEXT,
    details     TEXT,
    payload     JSONB,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reasoning_host_created
    ON reasoning(host, created_at DESC);

-- -----------------------------------------------
-- Tabela: traces (OpenTelemetry)
-- -----------------------------------------------
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
-- Tabela: db_telemetry (PostgreSQL, SQL Server...)
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS db_telemetry (
    id                  BIGSERIAL PRIMARY KEY,
    host                TEXT NOT NULL,
    db_type             TEXT NOT NULL,
    db_host             TEXT,
    db_port             INTEGER,
    slow_queries        JSONB,
    active_locks        JSONB,
    long_transactions   JSONB,
    connection_stats    JSONB,
    db_sizes            JSONB,
    raw_payload         JSONB,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_db_telemetry_host_created
    ON db_telemetry(host, created_at DESC);

-- -----------------------------------------------
-- Tabela: kafka_telemetry
-- -----------------------------------------------
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

-- -----------------------------------------------
-- Tabela: web_telemetry (Nginx, IIS, Apache...)
-- -----------------------------------------------
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

-- -----------------------------------------------
-- Tabela: discovery_events
-- -----------------------------------------------
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

-- -----------------------------------------------
-- Tabela: server_profiles
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS server_profiles (
    id          BIGSERIAL PRIMARY KEY,
    host        TEXT NOT NULL UNIQUE,
    profile     TEXT,
    setup_by    TEXT,
    setup_at    TIMESTAMP,
    collectors  JSONB,
    thresholds  JSONB,
    raw_config  JSONB,
    updated_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_server_profiles_host
    ON server_profiles(host);

-- -----------------------------------------------
-- Garantir colunas novas em tabelas existentes
-- (idempotente via ADD COLUMN IF NOT EXISTS)
-- -----------------------------------------------
ALTER TABLE snapshots    ADD COLUMN IF NOT EXISTS raw_json       JSONB;
ALTER TABLE snapshots    ADD COLUMN IF NOT EXISTS snapshot_json  JSONB;
ALTER TABLE snapshots    ADD COLUMN IF NOT EXISTS net_bytes_sent BIGINT DEFAULT 0;
ALTER TABLE snapshots    ADD COLUMN IF NOT EXISTS net_bytes_recv BIGINT DEFAULT 0;
ALTER TABLE events       ADD COLUMN IF NOT EXISTS summary        TEXT;
ALTER TABLE correlations ADD COLUMN IF NOT EXISTS summary        TEXT;
ALTER TABLE predictions  ADD COLUMN IF NOT EXISTS summary        TEXT;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS summary      TEXT;
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS details      TEXT;
-- Associa a investigação ao alerta que a originou (disparo manual, ver
-- POST /investigations/trigger) — permite reutilizar o resultado em vez
-- de repetir a chamada LLM quando o utilizador clica "Investigar" outra vez.
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS alert_id     BIGINT;
CREATE INDEX IF NOT EXISTS idx_investigations_alert_id ON investigations(alert_id);

-- -----------------------------------------------
-- Tabela: agent_commands
-- O chat envia comandos e o agente executa e devolve resultado.
-- Definicao completa mais abaixo (Solution Driver).
-- -----------------------------------------------

-- -----------------------------------------------
-- Tabela: agent_health
-- Saúde interna do agente — componentes, falhas de instrumentação, etc.
-- O agente reporta-se como um "paciente" que descreve os seus sintomas.
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS agent_health (
    id          BIGSERIAL   PRIMARY KEY,
    host        TEXT        NOT NULL,
    component   TEXT        NOT NULL,   -- otel_receiver | etw | dll_injection | sitecustomize | packet_capture | solution_driver | python_otel | java_otel | dotnet_otel
    status      TEXT        NOT NULL,   -- ok | error | missing | degraded | warning
    detail      TEXT,
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_health_host_created
    ON agent_health(host, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_health_component
    ON agent_health(host, component, created_at DESC);

-- -----------------------------------------------
-- Tabela: artifacts
-- Registo dos binários disponíveis no repositório interno do center.
-- Evita downloads de terceiros nas máquinas dos agentes.
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS artifacts (
    id           BIGSERIAL   PRIMARY KEY,
    name         TEXT        NOT NULL UNIQUE,
    filename     TEXT        NOT NULL,
    size_bytes   BIGINT      DEFAULT 0,
    sha256       TEXT,
    upstream_url TEXT,
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_artifacts_name ON artifacts(name);

-- -----------------------------------------------
-- UEBA: user_activity_events
-- Eventos de segurança por utilizador (alimentados pelo SecurityEventsCollector)
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS user_activity_events (
    id          BIGSERIAL   PRIMARY KEY,
    host        TEXT        NOT NULL,
    username    TEXT        NOT NULL,
    event_type  TEXT        NOT NULL,   -- user_logon | user_logoff | privilege_use | process_start | service_install | scheduled_task
    event_time  TIMESTAMPTZ NOT NULL,
    severity    TEXT        DEFAULT 'low',
    details     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_uae_host_user_time
    ON user_activity_events(host, username, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_uae_event_type
    ON user_activity_events(host, event_type, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_uae_severity
    ON user_activity_events(host, severity, event_time DESC);

-- -----------------------------------------------
-- UEBA: user_profiles
-- Perfil comportamental de 7 dias por utilizador
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS user_profiles (
    id                       BIGSERIAL   PRIMARY KEY,
    host                     TEXT        NOT NULL,
    username                 TEXT        NOT NULL,
    profile_version          INT         DEFAULT 1,
    is_human                 BOOLEAN,
    risk_score               INT         DEFAULT 0,   -- 0-100
    risk_level               TEXT        DEFAULT 'low',  -- low | medium | high | critical
    risk_delta               INT         DEFAULT 0,   -- variação face ao perfil anterior
    work_type                TEXT,
    primary_apps             JSONB,
    typical_hours            TEXT,
    active_days              JSONB,
    after_hours_activity     BOOLEAN     DEFAULT FALSE,
    weekend_activity         BOOLEAN     DEFAULT FALSE,
    frequent_destinations    JSONB,
    suspicious_destinations  JSONB,
    privilege_abuse_detected BOOLEAN     DEFAULT FALSE,
    behavioral_anomalies     JSONB,
    summary                  TEXT,
    raw_analysis             TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profiles_host_user
    ON user_profiles(host, username);

CREATE INDEX IF NOT EXISTS idx_user_profiles_risk
    ON user_profiles(host, risk_score DESC);

-- -----------------------------------------------
-- UEBA: ueba_analyses
-- Análises comportamentais pontuais com Claude
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS ueba_analyses (
    id               BIGSERIAL   PRIMARY KEY,
    host             TEXT        NOT NULL,
    username         TEXT        NOT NULL,
    analysis_type    TEXT        NOT NULL,   -- periodic | triggered | high_risk
    compliance_score INT,                     -- 0-100
    verdict          TEXT,                    -- compliant | suspicious | non_compliant
    summary          TEXT,
    anomalies        JSONB,
    events_analyzed  INT         DEFAULT 0,
    trigger_event    TEXT,                    -- evento que despoletou a análise
    raw_analysis     TEXT,
    analyzed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ueba_host_user_time
    ON ueba_analyses(host, username, analyzed_at DESC);

-- -----------------------------------------------
-- UEBA: security_alerts
-- Alertas comportamentais/segurança (separados dos alertas de infra)
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS security_alerts (
    id                  BIGSERIAL   PRIMARY KEY,
    host                TEXT        NOT NULL,
    username            TEXT,
    alert_type          TEXT        NOT NULL,   -- behavioral_anomaly | privilege_abuse | suspicious_process | etc.
    severity            TEXT        NOT NULL,   -- low | medium | high | critical
    title               TEXT        NOT NULL,
    details             JSONB,
    ueba_analysis_id    BIGINT      REFERENCES ueba_analyses(id),
    acknowledged        BOOLEAN     DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_security_alerts_host_time
    ON security_alerts(host, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_security_alerts_user
    ON security_alerts(username, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_security_alerts_severity
    ON security_alerts(severity, acknowledged, created_at DESC);

-- -----------------------------------------------
-- Adaptive thresholds — sigma per série métrica
-- Alimentado pelo AdaptiveThresholdEngine.
-- Período de aprendizagem: learning=TRUE (primeiros 20 alertas).
-- Após aprendizagem: sigma_warn/sigma_high ajustados automaticamente.
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS threshold_overrides (
    id                  BIGSERIAL   PRIMARY KEY,
    tracking_key        TEXT        UNIQUE NOT NULL,   -- "{host_key}::{metric_family}"
    alerts_fired        INTEGER     NOT NULL DEFAULT 0,
    alerts_with_problem INTEGER     NOT NULL DEFAULT 0,
    sigma_warn          NUMERIC(5,2) NOT NULL DEFAULT 2.5,
    sigma_high          NUMERIC(5,2) NOT NULL DEFAULT 3.5,
    learning            BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_threshold_overrides_key
    ON threshold_overrides(tracking_key);

-- -----------------------------------------------
-- Solution Driver — webhooks de notificação
-- Configurados via INSERT ou pela env JARVIS_NOTIFICATION_WEBHOOKS.
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS notification_webhooks (
    id              BIGSERIAL   PRIMARY KEY,
    name            TEXT        NOT NULL,
    url             TEXT        NOT NULL,
    secret          TEXT,                        -- HMAC key para assinar payload (opcional)
    min_severity    TEXT        NOT NULL DEFAULT 'high',  -- low|medium|high|critical
    host_filter     TEXT[],                      -- NULL = todos os hosts
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------
-- Solution Driver — comandos PowerShell para o agente
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS agent_commands (
    id              BIGSERIAL   PRIMARY KEY,
    host            TEXT        NOT NULL,
    problem_id      TEXT,
    conv_id         TEXT,
    script          TEXT        NOT NULL,
    timeout_s       INTEGER     NOT NULL DEFAULT 30,
    risk_level      TEXT        NOT NULL DEFAULT 'low',   -- low|medium|high
    status          TEXT        NOT NULL DEFAULT 'pending',
    -- pending | running | done | failed | rejected
    stdout          TEXT,
    stderr          TEXT,
    exit_code       INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at     TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

-- Migrar tabela antiga (sem colunas do Solution Driver)
ALTER TABLE agent_commands ADD COLUMN IF NOT EXISTS problem_id   TEXT;
ALTER TABLE agent_commands ADD COLUMN IF NOT EXISTS conv_id      TEXT;
ALTER TABLE agent_commands ADD COLUMN IF NOT EXISTS risk_level   TEXT NOT NULL DEFAULT 'low';
ALTER TABLE agent_commands ADD COLUMN IF NOT EXISTS executed_at  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_agent_commands_host_status
    ON agent_commands(host, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_commands_conv
    ON agent_commands(conv_id);

-- -----------------------------------------------
-- Solution Driver — conversas analista <-> Jarvis
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id              TEXT        PRIMARY KEY,     -- UUID gerado pelo center
    problem_id      TEXT        NOT NULL,
    host            TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'awaiting_analyst',
    -- awaiting_analyst | in_progress | action_approved | action_rejected | resolved | expired
    solution_summary    TEXT,
    proposed_script     TEXT,
    risk_level          TEXT,
    snapshot_at_detection JSONB,                -- estado da máquina no momento da detecção
    webhook_urls    TEXT[],                     -- webhooks que receberam esta notificação
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_problem
    ON conversations(problem_id);

CREATE INDEX IF NOT EXISTS idx_conversations_host_status
    ON conversations(host, status, created_at DESC);

-- -----------------------------------------------
-- Solution Driver — mensagens da conversa
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS conversation_messages (
    id              BIGSERIAL   PRIMARY KEY,
    conv_id         TEXT        NOT NULL REFERENCES conversations(id),
    role            TEXT        NOT NULL,        -- jarvis | analyst
    content         TEXT        NOT NULL,
    metadata        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_messages_conv
    ON conversation_messages(conv_id, created_at ASC);

-- -----------------------------------------------
-- Agentless Operations — investigações de segurança
-- Investigações de acesso privilegiado em máquinas sem agente
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS agentless_security_investigations (
    id                  BIGSERIAL   PRIMARY KEY,
    investigation_id    TEXT        NOT NULL UNIQUE,  -- machine-username-timestamp
    machine             TEXT        NOT NULL,
    username            TEXT        NOT NULL,
    access_method       TEXT        NOT NULL,          -- laps | password_reset
    credential_time     TIMESTAMPTZ NOT NULL,           -- hora em que a credencial foi gerada
    stated_reason       TEXT        NOT NULL,
    verdict             TEXT        NOT NULL DEFAULT 'PENDENTE',  -- CONFORME | SUSPEITO | NÃO CONFORME | ERRO
    report              TEXT,                          -- texto completo NIST
    evidence_summary    JSONB,                         -- contagens de evidências recolhidas
    error               TEXT,
    investigated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agentless_sec_machine
    ON agentless_security_investigations(machine, investigated_at DESC);

CREATE INDEX IF NOT EXISTS idx_agentless_sec_username
    ON agentless_security_investigations(username, investigated_at DESC);

-- -----------------------------------------------
-- Agentless Operations — auditoria de execução
-- Uma linha por host por chamada a agentless_run/agentless_run_bulk.
-- Nunca bloqueia nem condiciona a resposta ao utilizador — é escrita
-- depois do resultado já ter sido devolvido (ver execution_broker.py).
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS agentless_execution_log (
    id              BIGSERIAL   PRIMARY KEY,
    host            TEXT        NOT NULL,
    machine_type    TEXT        NOT NULL,
    operator        TEXT        NOT NULL DEFAULT 'system',
    source          TEXT        NOT NULL DEFAULT 'chat',
    context_id      TEXT,
    script_hash     TEXT        NOT NULL,
    script_snippet  TEXT        NOT NULL,
    transport       TEXT,
    success         BOOLEAN     NOT NULL,
    exit_code       INTEGER,
    error_summary   TEXT,
    snapshot        JSONB       NOT NULL DEFAULT '{}',  -- estado da máquina observado nesta execução
    duration_ms     INTEGER,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agentless_exec_host
    ON agentless_execution_log(host, executed_at DESC);

CREATE INDEX IF NOT EXISTS idx_agentless_exec_script
    ON agentless_execution_log(host, script_hash);

CREATE INDEX IF NOT EXISTS idx_agentless_exec_snapshot
    ON agentless_execution_log USING GIN (snapshot);

-- -----------------------------------------------
-- Agentless Operations — perfil agregado por máquina
-- Uma linha por host, actualizada a cada execução. É o que se consulta
-- ANTES de executar (rápido — evita agregar o log completo a cada chamada).
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS agentless_machine_profile (
    host                    TEXT        PRIMARY KEY,
    machine_type_preferred  TEXT,
    total_executions        INTEGER     NOT NULL DEFAULT 0,
    total_failures          INTEGER     NOT NULL DEFAULT 0,
    consecutive_failures    INTEGER     NOT NULL DEFAULT 0,
    last_success_at         TIMESTAMPTZ,
    last_failure_at         TIMESTAMPTZ,
    last_error_summary      TEXT,
    avg_duration_ms         INTEGER,
    known_state             JSONB       NOT NULL DEFAULT '{}',  -- {"last_session": {...}} — ver agentless_investigation_sessions
    known_issues            JSONB       NOT NULL DEFAULT '{}',  -- {signature: {count, first_seen, last_seen}}
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------
-- Agentless Operations — sessões de investigação por máquina
-- Agrupa várias execuções agentless que resolvem UM problema na mesma
-- máquina. Fica "open" enquanto houver actividade dentro da janela de
-- inactividade (AGENTLESS_SESSION_WINDOW_MINUTES); fecha e consolida o
-- contexto em agentless_machine_profile.known_state.last_session quando
-- a máquina fica "quieta" por mais tempo que a janela (detectado de forma
-- preguiçosa na próxima chamada a essa máquina, e por um sweep periódico
-- para sessões que nunca mais são tocadas).
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS agentless_investigation_sessions (
    id                  BIGSERIAL   PRIMARY KEY,
    host                TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'open',  -- 'open' | 'closed'
    reason              TEXT,                                  -- pedido do utilizador que abriu a sessão
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at           TIMESTAMPTZ,
    execution_count     INTEGER     NOT NULL DEFAULT 0,
    executions          JSONB       NOT NULL DEFAULT '[]'      -- [{script, success, summary, at}, ...]
);

-- No máximo uma sessão "open" por host de cada vez.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agentless_sessions_one_open_per_host
    ON agentless_investigation_sessions(host) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_agentless_sessions_host_activity
    ON agentless_investigation_sessions(host, last_activity_at DESC);

CREATE INDEX IF NOT EXISTS idx_agentless_sec_verdict
    ON agentless_security_investigations(verdict, investigated_at DESC);

-- -----------------------------------------------
-- Jarvis Fates Engine — Clotho
-- Integrações com ferramentas empresariais. O utilizador descreve a ligação
-- (nome, host, porta, autenticação, notas) e é o Jarvis (Clotho) que
-- identifica de que solução se trata — não há uma lista fixa de tipos.
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS clotho_integrations (
    id          BIGSERIAL   PRIMARY KEY,
    name        TEXT        NOT NULL,
    type        TEXT,                   -- identificado pelo Clotho (ex: Zabbix, Checkpoint, PostgreSQL...)
    host        TEXT,
    port        INTEGER,
    auth_type   TEXT,                   -- api_key | basic | token | none
    config      JSONB,                  -- campos adicionais não-sensíveis
    notes       TEXT,
    analysis    TEXT,                   -- análise assistida pelo Jarvis (Clotho)
    tools       JSONB,                  -- {"connection_profile": {...}, "tools": {"<nome>": {...}}}
    status      TEXT        NOT NULL DEFAULT 'draft',  -- draft | configured | active | error
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE clotho_integrations ALTER COLUMN type DROP NOT NULL;
ALTER TABLE clotho_integrations ADD COLUMN IF NOT EXISTS tools JSONB;

CREATE INDEX IF NOT EXISTS idx_clotho_integrations_type
    ON clotho_integrations(type, created_at DESC);

-- -----------------------------------------------
-- Jarvis Fates Engine — Lachesis
-- Tarefas agendadas, relatórios automáticos, comportamentos ensinados
-- e Asclepion Systems (hardening / CIS Control Benchmarking).
-- -----------------------------------------------

CREATE TABLE IF NOT EXISTS lachesis_tasks (
    id              BIGSERIAL   PRIMARY KEY,
    name            TEXT        NOT NULL,
    instruction     TEXT        NOT NULL,
    task_type       TEXT        NOT NULL DEFAULT 'agentless',  -- integration | agentless | database
    schedule        JSONB       NOT NULL,
    webhook_id      BIGINT      REFERENCES notification_webhooks(id) ON DELETE SET NULL,
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    last_run_at     TIMESTAMPTZ,
    next_run_at     TIMESTAMPTZ,
    last_status     TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lachesis_tasks_due
    ON lachesis_tasks(enabled, next_run_at);

-- Automações (generalização das tarefas agendadas): tipo de gatilho e grafo
-- de flow compilado a partir da instrução. Tarefas sem flow_definition
-- continuam a correr pelo caminho legado (instrução -> run_jarvis_loop).
ALTER TABLE lachesis_tasks ADD COLUMN IF NOT EXISTS trigger_type   TEXT NOT NULL DEFAULT 'schedule';  -- schedule | event | webhook_in | manual
ALTER TABLE lachesis_tasks ADD COLUMN IF NOT EXISTS trigger_config JSONB;
ALTER TABLE lachesis_tasks ADD COLUMN IF NOT EXISTS flow_definition JSONB;

-- Token único (URL-safe, 256 bits) que autentica o endpoint público
-- POST /lachesis/webhook_in/{token} para tarefas com trigger_type = 'webhook_in'.
ALTER TABLE lachesis_tasks ADD COLUMN IF NOT EXISTS webhook_token TEXT UNIQUE;

CREATE TABLE IF NOT EXISTS lachesis_runs (
    id              BIGSERIAL   PRIMARY KEY,
    task_id         BIGINT      NOT NULL REFERENCES lachesis_tasks(id) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'running',  -- running | ok | error
    result_text     TEXT,
    error           TEXT,
    webhook_delivered BOOLEAN   NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_lachesis_runs_task
    ON lachesis_runs(task_id, started_at DESC);

CREATE TABLE IF NOT EXISTS lachesis_behaviors (
    id              BIGSERIAL   PRIMARY KEY,
    title           TEXT        NOT NULL,
    instruction     TEXT        NOT NULL,
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Casa do Conhecimento: reaproveita lachesis_behaviors para factos de infraestrutura
-- (kind='infra_fact'), além dos comportamentos ensinados originais (kind='behavior').
ALTER TABLE lachesis_behaviors ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'behavior';
ALTER TABLE lachesis_behaviors ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE lachesis_behaviors ADD COLUMN IF NOT EXISTS version TEXT;
ALTER TABLE lachesis_behaviors ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE lachesis_behaviors ADD COLUMN IF NOT EXISTS evidence TEXT;

-- Âmbito do facto: 'global' (infra-wide, ex: EDR padrão), 'host' (uma máquina
-- específica, ex: "DC-X inacessível via agentless"), ou 'target' (um alvo do
-- Explorer, ex: "alvo X cai todos os dias às 10h"). scope_value é o hostname
-- ou o nome do scope_target; NULL para 'global'.
ALTER TABLE lachesis_behaviors ADD COLUMN IF NOT EXISTS scope_type TEXT NOT NULL DEFAULT 'global';
ALTER TABLE lachesis_behaviors ADD COLUMN IF NOT EXISTS scope_value TEXT;

CREATE INDEX IF NOT EXISTS idx_lachesis_behaviors_active
    ON lachesis_behaviors(active);

CREATE INDEX IF NOT EXISTS idx_lachesis_behaviors_scope
    ON lachesis_behaviors(scope_type, scope_value);

CREATE INDEX IF NOT EXISTS idx_lachesis_behaviors_kind
    ON lachesis_behaviors(kind);

CREATE TABLE IF NOT EXISTS asclepion_profiles (
    id              BIGSERIAL   PRIMARY KEY,
    name            TEXT        NOT NULL,
    targets         TEXT[]      NOT NULL,
    os_type         TEXT        NOT NULL DEFAULT 'windows',
    machine_type    TEXT        NOT NULL DEFAULT 'workstation',
    benchmark       TEXT        NOT NULL,
    checklist       JSONB,
    status          TEXT        NOT NULL DEFAULT 'draft',  -- draft | ready | error
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_asclepion_profiles_status
    ON asclepion_profiles(status, created_at DESC);

CREATE TABLE IF NOT EXISTS asclepion_runs (
    id              BIGSERIAL   PRIMARY KEY,
    profile_id      BIGINT      NOT NULL REFERENCES asclepion_profiles(id) ON DELETE CASCADE,
    target          TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'running',  -- running | ok | error
    score           NUMERIC(5,2),
    results         JSONB,
    summary         TEXT,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_asclepion_runs_profile
    ON asclepion_runs(profile_id, started_at DESC);

-- -----------------------------------------------
-- Jarvis Fates Engine — refactor de tipos de tarefa Lachesis
-- (integration | agentless | database) + Conector de Bases de Dados (Clotho)
-- -----------------------------------------------

UPDATE lachesis_tasks SET task_type = 'agentless' WHERE task_type NOT IN ('integration', 'agentless', 'database');
COMMENT ON COLUMN lachesis_tasks.task_type IS 'integration | agentless | database';

CREATE SCHEMA IF NOT EXISTS clotho_data;

CREATE TABLE IF NOT EXISTS clotho_databases (
    id              BIGSERIAL   PRIMARY KEY,
    name            TEXT        NOT NULL,
    source          TEXT        NOT NULL DEFAULT 'connection',  -- connection | csv
    engine          TEXT        NOT NULL DEFAULT 'postgres',     -- postgres | mysql | mssql | oracle | ...
    host            TEXT,
    port            INTEGER,
    db_name         TEXT,
    table_name      TEXT,
    config          JSONB       NOT NULL DEFAULT '{}',
    schema_cache    JSONB,
    notes           TEXT,
    status          TEXT        NOT NULL DEFAULT 'draft',  -- draft | ready | error
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_clotho_databases_status
    ON clotho_databases(status, created_at DESC);

-- -----------------------------------------------
-- Tabela: dashboard_widgets
-- Widgets da página inicial criados por linguagem natural (Fase 3 do
-- redesign da UI) — cada widget guarda o pedido original em texto e o
-- resultado JSON da última execução do run_jarvis_loop (que pode chamar
-- ferramentas Clotho ao vivo). Sem ACL por utilizador: partilhados por quem
-- acede com a API key, tal como as outras tabelas do Fates Engine.
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS dashboard_widgets (
    id                BIGSERIAL   PRIMARY KEY,
    created_by        TEXT,
    prompt            TEXT        NOT NULL,
    title             TEXT,
    widget_type       TEXT,
    last_data         JSONB,
    last_refreshed_at TIMESTAMPTZ,
    last_error        TEXT,
    position          INT         NOT NULL DEFAULT 0,
    col_span          INT         NOT NULL DEFAULT 4,
    row_span          INT         NOT NULL DEFAULT 3,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fase 4: layout editável (redimensionar/reordenar) — colunas novas numa
-- instalação que já tinha a tabela da Fase 3.
ALTER TABLE dashboard_widgets ADD COLUMN IF NOT EXISTS col_span INT NOT NULL DEFAULT 4;
ALTER TABLE dashboard_widgets ADD COLUMN IF NOT EXISTS row_span INT NOT NULL DEFAULT 3;

CREATE INDEX IF NOT EXISTS idx_dashboard_widgets_position
    ON dashboard_widgets(position, created_at);

-- -----------------------------------------------
-- Jarvis Fates Engine — Scope Collector + Explorer
-- Coletor separado que traz telemetria de fontes externas (Zabbix, Splunk,
-- agentless) apenas para os alvos que o utilizador escolher explicitamente
-- (scope_targets), reaproveitando o mesmo pipeline de ingestão/análise do
-- agente (BaselineEngine, Correlator, Predictor, AdaptiveThresholdEngine)
-- sem alterações. exploration_tips guarda as sugestões proactivas de
-- melhoria geradas pelo Explorer Engine, por categoria do alvo monitorizado.
-- -----------------------------------------------

CREATE TABLE IF NOT EXISTS scope_targets (
    id                 BIGSERIAL   PRIMARY KEY,
    integration_id     BIGINT      REFERENCES clotho_integrations(id) ON DELETE CASCADE,  -- NULL = alvo só-agentless
    name               TEXT        NOT NULL,
    target_kind        TEXT        NOT NULL,  -- zabbix_host | zabbix_item | splunk_index | splunk_search | agentless_host
    target_ref         TEXT        NOT NULL,  -- hostid/itemid Zabbix, index/saved-search Splunk, hostname/IP agentless
    category           TEXT,                   -- server | application | database | network | security | business
    category_source    TEXT        NOT NULL DEFAULT 'manual',  -- manual | llm_suggested
    frame_type         TEXT        NOT NULL DEFAULT 'metrics', -- metrics | db_telemetry | web_telemetry
    agentless_fallback BOOLEAN     NOT NULL DEFAULT FALSE,
    cadence_seconds    INTEGER     NOT NULL DEFAULT 60,
    host_key_hint      TEXT,                   -- hostname estável usado para sintetizar host{} no frame
    enabled            BOOLEAN     NOT NULL DEFAULT TRUE,
    last_polled_at     TIMESTAMPTZ,
    last_status        TEXT,
    created_by         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scope_targets_due
    ON scope_targets(enabled, last_polled_at);

-- -----------------------------------------------
-- Tabela: host_status_signals
-- Sinal de saúde mais recente de um host, por fonte externa (Zabbix,
-- Splunk, e futuramente outros SIEM/EDR via Clotho). Uma linha por
-- (host, source) — só o estado mais recente, não histórico — mesmo padrão
-- de baixo custo que scope_targets.last_status já usa. Escrita pelo
-- scope_collector (host_status.py) no mesmo ciclo de poll que já mantém
-- scope_targets.last_status; lida em modo só-leitura pelo Atropos
-- (dashboard de estado de infraestrutura, sem custo de tokens de LLM).
-- Adicionar uma fonte nova (ex: CrowdStrike) = um adaptador novo em
-- host_status.py que escreve aqui com source='crowdstrike' — não requer
-- alterações no Atropos.
-- -----------------------------------------------

CREATE TABLE IF NOT EXISTS host_status_signals (
    host             TEXT        NOT NULL,
    source           TEXT        NOT NULL,   -- zabbix | splunk | crowdstrike | ...
    severity         TEXT        NOT NULL,   -- ok | warning | critical | unknown
    message          TEXT,
    raw              JSONB,
    scope_target_id  BIGINT      REFERENCES scope_targets(id) ON DELETE SET NULL,
    observed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (host, source)
);

CREATE INDEX IF NOT EXISTS idx_host_status_signals_host
    ON host_status_signals(host);

CREATE TABLE IF NOT EXISTS exploration_tips (
    id              BIGSERIAL   PRIMARY KEY,
    scope_target_id BIGINT      NOT NULL REFERENCES scope_targets(id) ON DELETE CASCADE,
    category        TEXT        NOT NULL,  -- herdado de scope_targets.category no momento da geração
    title           TEXT        NOT NULL,
    summary         TEXT        NOT NULL,
    detail          TEXT,
    priority        TEXT        NOT NULL DEFAULT 'medium',  -- low | medium | high | critical
    status          TEXT        NOT NULL DEFAULT 'new',     -- new | acknowledged | dismissed | resolved
    evidence        JSONB,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    created_by      TEXT        NOT NULL DEFAULT 'explorer_engine'
);

CREATE INDEX IF NOT EXISTS idx_exploration_tips_target
    ON exploration_tips(scope_target_id, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_exploration_tips_status
    ON exploration_tips(status, priority);

-- Cadência própria do Explorer Engine (muito mais espaçada que a cadência de
-- recolha de telemetria) — evita chamar o LLM a cada ciclo de polling.
ALTER TABLE scope_targets ADD COLUMN IF NOT EXISTS explore_cadence_seconds INTEGER NOT NULL DEFAULT 3600;
ALTER TABLE scope_targets ADD COLUMN IF NOT EXISTS last_explored_at TIMESTAMPTZ;

-- SO do alvo monitorizado — o Explorer Engine usa isto para não sugerir
-- comandos Linux (vmstat, cgroups...) num alvo Windows, ou vice-versa.
ALTER TABLE scope_targets ADD COLUMN IF NOT EXISTS os_type TEXT NOT NULL DEFAULT 'windows';

-- O Explorer pode sinalizar que a evidência já recolhida (Zabbix/Splunk) não
-- chega para confirmar a causa, e recomendar uma investigação mais profunda
-- via agentless (chat principal do Jarvis, com aprovação humana — ver
-- api/chat_engine.py APPROVAL_TOOLS). Não é uma restrição — o utilizador
-- pode sempre investigar qualquer tip, isto só destaca quando vale a pena.
ALTER TABLE exploration_tips ADD COLUMN IF NOT EXISTS needs_investigation BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE exploration_tips ADD COLUMN IF NOT EXISTS investigation_reason TEXT;

-- Modo de análise escolhido pelo utilizador ao criar o alvo:
--   live       -> recolha contínua a partir de agora (poll_target/BaselineEngine/Predictor, como já existia)
--   historical -> análise retrospectiva de um intervalo já existente no Zabbix/Splunk
--                 (não passa pelo BaselineEngine/Predictor — ver explorer/historical_engine.py)
ALTER TABLE scope_targets ADD COLUMN IF NOT EXISTS analysis_mode TEXT NOT NULL DEFAULT 'live';
ALTER TABLE scope_targets ADD COLUMN IF NOT EXISTS history_start TIMESTAMPTZ;
ALTER TABLE scope_targets ADD COLUMN IF NOT EXISTS history_end TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_scope_targets_explore_due
    ON scope_targets(enabled, last_explored_at);

-- -----------------------------------------------
-- Tabela: root_reset_requests (perfil de chat "root-only")
-- -----------------------------------------------
-- A senha gerada (reset_root_rsi ou LAPS) NUNCA é persistida aqui — só é
-- devolvida uma vez na resposta ao utilizador. Esta tabela guarda apenas o
-- pedido (motivo declarado, host, mecanismo usado, sucesso/erro) e o estado
-- da investigação de 24h feita pelo SecurityInvestigator (ver
-- agentless/investigators/security_investigator.py e agentless_store —
-- investigate_id aponta para o registo lá guardado).
CREATE TABLE IF NOT EXISTS root_reset_requests (
    id                  BIGSERIAL   PRIMARY KEY,
    requested_by        TEXT        NOT NULL,
    motivo_slug         TEXT        NOT NULL,
    motivo_text         TEXT        NOT NULL,
    hostname            TEXT        NOT NULL,
    mechanism           TEXT,       -- 'reset_root_rsi' | 'laps' | NULL (se ambos falharam ou ainda pendente)
    status              TEXT        NOT NULL DEFAULT 'pending_approval', -- 'pending_approval' | 'ok' | 'error' | 'denied'
    error_message       TEXT,
    approved_by         TEXT,
    approved_at         TIMESTAMPTZ,
    delivered           BOOLEAN     NOT NULL DEFAULT FALSE,
    investigate_at      TIMESTAMPTZ,
    investigated_at     TIMESTAMPTZ,
    investigation_id    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE root_reset_requests ADD COLUMN IF NOT EXISTS approved_by TEXT;
ALTER TABLE root_reset_requests ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE root_reset_requests ADD COLUMN IF NOT EXISTS delivered BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE root_reset_requests ALTER COLUMN investigate_at DROP NOT NULL;
ALTER TABLE root_reset_requests ALTER COLUMN status SET DEFAULT 'pending_approval';
-- Para entrega automática (push) da senha ao aprovar, sem o utilizador ter de
-- reenviar a mensagem — ver rootreset/openwebui_push.py.
ALTER TABLE root_reset_requests ADD COLUMN IF NOT EXISTS requester_user_id TEXT;
ALTER TABLE root_reset_requests ADD COLUMN IF NOT EXISTS chat_id TEXT;
ALTER TABLE root_reset_requests ADD COLUMN IF NOT EXISTS message_id TEXT;

CREATE INDEX IF NOT EXISTS idx_root_reset_requests_created
    ON root_reset_requests(created_at DESC);

-- -----------------------------------------------
-- Tabela: root_reset_auto_approve
-- -----------------------------------------------
-- Motivos (motivo_slug — whitelist oficial em rootreset/reasons.py OU um
-- "custom_*" livre já usado antes) que um admin marcou como não precisando
-- de aprovação humana: o pedido passa logo a 'processing' e executa,
-- sem aparecer em /pedidos. Presença na tabela = auto-aprovado.
CREATE TABLE IF NOT EXISTS root_reset_auto_approve (
    motivo_slug TEXT        PRIMARY KEY,
    motivo_text TEXT,
    updated_by  TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------
-- Tabela: root_reset_custom_reasons
-- -----------------------------------------------
-- Motivos novos criados pelo admin (Fates → Clotho → "root_motivos"), além
-- dos 29 oficiais hardcoded em rootreset/reasons.py::REASON_TITLES. Lidos
-- com cache de 60s em reasons.py (parser.py está no caminho quente de cada
-- mensagem do chat root-only).
CREATE TABLE IF NOT EXISTS root_reset_custom_reasons (
    slug        TEXT        PRIMARY KEY,
    title       TEXT        NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------
-- Tabela: access_request_types (laboratório — tipos de pedido de acesso
-- privilegiado definidos pelo admin, além do reset root/LAPS nativo).
-- -----------------------------------------------
-- A acção executada na aprovação NÃO é código novo — é uma tool Clotho já
-- criada e testada no Laboratório (clotho_integrations.tools). Isto permite
-- ao admin criar um tipo de pedido novo (ex: desbloqueio de conta AD, reset
-- de senha de outra aplicação) sem precisar de deploy de código, desde que
-- exista uma tool Clotho que faça a acção via HTTP.
CREATE TABLE IF NOT EXISTS access_request_types (
    id                BIGSERIAL   PRIMARY KEY,
    slug              TEXT        NOT NULL UNIQUE,
    name              TEXT        NOT NULL,
    description       TEXT,
    integration_name  TEXT        NOT NULL,  -- nome da integração Clotho (clotho_integrations.name)
    tool_name         TEXT        NOT NULL,  -- nome da tool dentro dessa integração
    target_param      TEXT        NOT NULL DEFAULT 'target', -- qual params.<nome> da tool recebe o "alvo" do pedido
    result_field      TEXT,       -- campo do JSON de resposta da tool a mostrar ao utilizador (ex: "senha")
    requires_approval BOOLEAN     NOT NULL DEFAULT TRUE,
    active            BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------
-- Tabela: access_requests (pedidos concretos de um access_request_types)
-- -----------------------------------------------
CREATE TABLE IF NOT EXISTS access_requests (
    id                BIGSERIAL   PRIMARY KEY,
    request_type_id   BIGINT      NOT NULL REFERENCES access_request_types(id),
    requested_by      TEXT        NOT NULL,
    target            TEXT        NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'pending_approval', -- pending_approval | processing | ok | error | denied
    error_message     TEXT,
    approved_by       TEXT,
    approved_at       TIMESTAMPTZ,
    delivered         BOOLEAN     NOT NULL DEFAULT FALSE,
    requester_user_id TEXT,
    chat_id           TEXT,
    message_id        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_access_requests_type_created
    ON access_requests(request_type_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_root_reset_requests_due
    ON root_reset_requests(investigate_at)
    WHERE investigated_at IS NULL AND status = 'ok';

-- -----------------------------------------------
-- Tabela: root_reset_alert_acks
-- -----------------------------------------------
-- Card "Auditoria de Root" no Início — contagem de máquinas com alerta (por
-- veredicto de investigação SUSPEITO/NÃO CONFORME/ERRO) que ainda não foram
-- vistas por um analista. Guarda, por máquina, qual foi a última investigação
-- já vista (investigation_id) — o badge conta máquinas cujo veredicto mais
-- recente ainda não corresponde ao último visto aqui.
CREATE TABLE IF NOT EXISTS root_reset_alert_acks (
    machine                    TEXT        PRIMARY KEY,
    last_seen_investigation_id TEXT,
    acknowledged_by            TEXT,
    acknowledged_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
