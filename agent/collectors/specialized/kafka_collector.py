"""
KafkaCollector — coleta metricas do Apache Kafka.

Recolhe via kafka-python:
  - Consumer group lag por topico e particao
  - Topics e particoes existentes
  - Throughput estimado (offsets mais recentes vs anteriores)
  - Brokers ativos no cluster

Frame gerado: kafka_telemetry
"""

import logging
import agent_core.config as cfg

logger = logging.getLogger(__name__)


class KafkaCollector:

    FRAME_TYPE = "kafka_telemetry"

    def __init__(self):
        self._brokers    = cfg._get("collectors.kafka.brokers", ["localhost:9092"])
        self._groups     = cfg._get("collectors.kafka.consumer_groups", [])
        self._prev_offsets = {}

    def collect(self) -> list[dict]:
        try:
            return [self._collect_all()]
        except Exception as e:
            logger.warning(f"[KAFKA] Erro na coleta: {e}")
            return []

    def _collect_all(self) -> dict:
        from kafka import KafkaAdminClient, KafkaConsumer
        from kafka.admin import NewTopic

        admin = KafkaAdminClient(
            bootstrap_servers=self._brokers,
            client_id="jarvis-kafka-collector",
            request_timeout_ms=5000,
        )

        try:
            brokers     = self._get_brokers(admin)
            topics      = self._get_topics(admin)
            group_lag   = self._get_consumer_lag(admin)
            throughput  = self._get_throughput()
        finally:
            admin.close()

        return {
            "brokers":     brokers,
            "topics":      topics,
            "consumer_lag": group_lag,
            "throughput":  throughput,
        }

    def _get_brokers(self, admin) -> list[dict]:
        try:
            metadata = admin.describe_cluster()
            return [
                {"id": b["node_id"], "host": b["host"], "port": b["port"]}
                for b in (metadata.get("brokers") or [])
            ]
        except Exception:
            return []

    def _get_topics(self, admin) -> list[dict]:
        try:
            topics = admin.list_topics()
            result = []
            for name in topics:
                if name.startswith("__"):
                    continue  # skip internal topics
                result.append({"name": name})
            return result
        except Exception:
            return []

    def _get_consumer_lag(self, admin) -> list[dict]:
        try:
            groups = self._groups or self._list_groups(admin)
            result = []

            for group in groups:
                try:
                    offsets = admin.list_consumer_group_offsets(group)
                    for tp, offset_meta in (offsets or {}).items():
                        result.append({
                            "group":     group,
                            "topic":     tp.topic,
                            "partition": tp.partition,
                            "committed": offset_meta.offset,
                        })
                except Exception:
                    pass

            return result
        except Exception:
            return []

    def _list_groups(self, admin) -> list[str]:
        try:
            groups = admin.list_consumer_groups()
            return [g[0] for g in (groups or [])][:20]
        except Exception:
            return []

    def _get_throughput(self) -> dict:
        """Placeholder — throughput real requer JMX ou Kafka Metrics."""
        return {}
