"""
Config — carrega configuracao do jarvis_config.json se existir.
Faz fallback para valores default se o ficheiro nao existir.

O jarvis_config.json e gerado pelo SetupEngine na primeira instalacao
e atualizado pelo DiscoveryEngine em runtime.
"""

import json
import os

# ---------------------------------------------------
# LOCALIZACAO DO CONFIG FILE
# ---------------------------------------------------

_CONFIG_PATHS = [
    os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "JarvisAgent", "jarvis_config.json"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jarvis_config.json"),
]


def _load_config() -> dict:
    for path in _CONFIG_PATHS:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


_cfg = _load_config()


def _get(path: str, default):
    parts = path.split(".")
    node = _cfg
    for part in parts:
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node


def save_config(config: dict) -> str | None:
    """Persiste o config no primeiro path disponivel."""
    for path in _CONFIG_PATHS:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            return path
        except Exception:
            continue
    return None


def reload():
    """Rele o config do disco (chamado apos updates do DiscoveryEngine)."""
    global _cfg
    _cfg = _load_config()


# ---------------------------------------------------
# SERVIDOR / CENTER
# ---------------------------------------------------

CORE_URL           = _get("server.center_url", "http://localhost:8080")
API_KEY            = _get("server.api_key", "")
TELEMETRY_ENDPOINT = "/telemetry"
# Versão lida do config gerado pelo installer — nunca hardcoded aqui
AGENT_VERSION      = _get("agent_version", "0.4.1")

# ---------------------------------------------------
# COLETA CORE (sempre ativo)
# ---------------------------------------------------

COLLECTION_INTERVAL_SECONDS = _get("collectors.core.metrics_interval", 1)
SEND_INTERVAL_SECONDS       = 1
BATCH_SIZE                  = 50
HTTP_TIMEOUT_SECONDS        = 5
MAX_RETRIES                 = 5

METRICS_INTERVAL_SECONDS   = _get("collectors.core.metrics_interval", 1)
INVENTORY_INTERVAL_SECONDS = _get("collectors.core.inventory_interval", 10)
TRACES_INTERVAL_SECONDS    = _get("collectors.core.traces_interval", 5)
LOGS_INTERVAL_SECONDS      = _get("collectors.core.logs_interval", 15)
ENABLE_LOGS                = _get("collectors.core.enable_logs", True)
ENABLE_TRACES              = _get("collectors.core.enable_traces", True)

# ---------------------------------------------------
# OTEL RECEIVER
# ---------------------------------------------------

OTEL_RECEIVER_ENABLED       = _get("collectors.otel_receiver.enabled", True)
OTEL_RECEIVER_PORT          = _get("collectors.otel_receiver.port", 4318)
OTEL_FLUSH_INTERVAL_SECONDS = _get("collectors.otel_receiver.flush_interval", 5)

# ---------------------------------------------------
# NETWORK PROBE (Architecture B)
# ---------------------------------------------------

NETWORK_PROBE_ENABLED       = _get("network_probe.enabled", True)
NETWORK_PROBE_OTEL_ENDPOINT = _get("network_probe.otel_endpoint", "http://localhost:4318")
WINDIVERT_ENABLED           = _get("network_probe.windivert_enabled", False)

# ---------------------------------------------------
# PROCESS INJECTOR (Dynatrace Layer 2)
# Substitui AUTO_INSTRUMENT_ENABLED — injeta por processo, não globalmente
# ---------------------------------------------------

PROCESS_INJECTOR_ENABLED = _get("collectors.process_injector.enabled", True)

# ---------------------------------------------------
# COLLECTORS ESPECIALIZADOS
# ---------------------------------------------------

DB_POSTGRES_ENABLED = _get("collectors.db_postgres.enabled", False)
DB_MSSQL_ENABLED    = _get("collectors.db_mssql.enabled", False)
KAFKA_ENABLED       = _get("collectors.kafka.enabled", False)
WEB_ENABLED         = _get("collectors.web.enabled", False)
MQ_ENABLED          = _get("collectors.mq.enabled", False)

# ---------------------------------------------------
# THRESHOLDS
# ---------------------------------------------------

THRESH_CPU_MEDIUM         = _get("thresholds.cpu_medium", 75)
THRESH_CPU_HIGH           = _get("thresholds.cpu_high", 90)
THRESH_MEMORY_MEDIUM      = _get("thresholds.memory_medium", 80)
THRESH_MEMORY_HIGH        = _get("thresholds.memory_high", 90)
THRESH_DISK_MEDIUM        = _get("thresholds.disk_medium", 85)
THRESH_DISK_HIGH          = _get("thresholds.disk_high", 95)
THRESH_SLOW_QUERY_MS      = _get("thresholds.slow_query_ms", 2000)
THRESH_LONG_TRANSACTION_S = _get("thresholds.long_transaction_s", 60)
THRESH_CONN_POOL_PCT      = _get("thresholds.connection_pool_pct", 80)

# ---------------------------------------------------
# AUTODISCOVERY
# ---------------------------------------------------

AUTODISCOVERY_ENABLED          = _get("autodiscovery.enabled", True)
AUTODISCOVERY_INTERVAL_SECONDS = _get("autodiscovery.interval_seconds", 300)
AUTODISCOVERY_AI_ON_CHANGE     = _get("autodiscovery.ai_on_change", False)

# ---------------------------------------------------
# ESTADO DO SETUP
# ---------------------------------------------------

SETUP_COMPLETED = _get("setup_completed_at", None) is not None
SERVER_PROFILE  = _get("profile", "unknown")
