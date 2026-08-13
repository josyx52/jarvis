"""
CollectorRegistry — gere quais collectors estao ativos.

Le o jarvis_config.json via agent_core.config e inicializa
apenas os collectors configurados.

Dois niveis:
  CORE        — sempre ativos (metrics, inventory, traces, logs, otel_receiver)
  SPECIALIZED — ativados pela config (db_postgres, db_mssql, kafka, web, mq)

Interface com o RuntimeAgent:
  registry.core_collectors()       → lista de collectors core
  registry.specialized_collectors() → lista de collectors especializados ativos
  registry.all_collectors()        → todos
"""

import logging
import agent_core.config as cfg

logger = logging.getLogger(__name__)


class CollectorRegistry:

    def __init__(self):
        self._core        = []
        self._specialized = []
        self._init_core()
        self._init_specialized()

    # ----------------------------------------
    # INIT CORE (sempre ativo)
    # ----------------------------------------

    def _init_core(self):
        from collectors.metrics_collector import MetricsCollector
        from collectors.osquery_collector import OsqueryCollector
        from collectors.traces_collector  import ConnectionsCollector

        self._core = [
            MetricsCollector(),
            OsqueryCollector(),
            ConnectionsCollector(),
        ]

        if cfg.ENABLE_LOGS:
            try:
                from collectors.logs_collector import LogsCollector
                self._core.append(LogsCollector())
            except Exception as e:
                logger.warning(f"[REGISTRY] LogsCollector nao carregado: {e}")

        if cfg.OTEL_RECEIVER_ENABLED:
            try:
                from collectors.otel_receiver import OtelReceiver
                otel = OtelReceiver(port=cfg.OTEL_RECEIVER_PORT)
                otel.start()
                self._otel_receiver = otel
                logger.info(f"[REGISTRY] OtelReceiver ativo na porta {cfg.OTEL_RECEIVER_PORT}")
            except Exception as e:
                logger.warning(f"[REGISTRY] OtelReceiver nao iniciado: {e}")
                self._otel_receiver = None
        else:
            self._otel_receiver = None

        logger.info(f"[REGISTRY] Core collectors: {len(self._core)} ativos")

    # ----------------------------------------
    # INIT SPECIALIZED (baseado na config)
    # ----------------------------------------

    def _init_specialized(self):

        if cfg.DB_POSTGRES_ENABLED:
            self._try_load("collectors.specialized.db_postgres", "PostgresCollector", "PostgreSQL")

        if cfg.DB_MSSQL_ENABLED:
            self._try_load("collectors.specialized.db_mssql", "MssqlCollector", "SQL Server")

        if cfg.KAFKA_ENABLED:
            self._try_load("collectors.specialized.kafka_collector", "KafkaCollector", "Kafka")

        if cfg.WEB_ENABLED:
            web_type = cfg._get("collectors.web.type", None)
            self._try_load("collectors.specialized.web_collector", "WebCollector", f"Web ({web_type})")

        if cfg.MQ_ENABLED:
            mq_type = cfg._get("collectors.mq.type", None)
            self._try_load("collectors.specialized.mq_collector", "MqCollector", f"MQ ({mq_type})")

        logger.info(f"[REGISTRY] Specialized collectors: {len(self._specialized)} ativos")

    def _try_load(self, module_path: str, class_name: str, label: str):
        try:
            import importlib
            module = importlib.import_module(module_path)
            cls    = getattr(module, class_name)
            instance = cls()
            self._specialized.append(instance)
            logger.info(f"[REGISTRY] Collector ativado: {label}")
        except Exception as e:
            logger.warning(f"[REGISTRY] Nao foi possivel ativar {label}: {e}")

    # ----------------------------------------
    # ACESSO
    # ----------------------------------------

    def core_collectors(self) -> list:
        return list(self._core)

    def specialized_collectors(self) -> list:
        return list(self._specialized)

    def all_collectors(self) -> list:
        return self._core + self._specialized

    def otel_receiver(self):
        return getattr(self, "_otel_receiver", None)

    def report(self) -> dict:
        return {
            "core":        [type(c).__name__ for c in self._core],
            "specialized": [type(c).__name__ for c in self._specialized],
            "otel_receiver": self._otel_receiver is not None,
        }
