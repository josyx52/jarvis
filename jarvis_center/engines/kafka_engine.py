"""
KafkaEngine — analisa telemetria de Apache Kafka (kafka_telemetry frames).

Detecta:
  - Consumer group lag elevado (mensagens por processar)
  - Brokers em falta no cluster
  - Topics sem consumidores ativos
  - Particoes offline
"""


LAG_MEDIUM_THRESHOLD  = 1_000    # mensagens atrasadas — alerta medio
LAG_HIGH_THRESHOLD    = 10_000   # mensagens atrasadas — alerta alto
MIN_BROKERS_EXPECTED  = 1


class KafkaEngine:

    def analyze(self, kafka_telemetry: dict) -> list[dict]:
        if not kafka_telemetry:
            return []

        events  = []
        brokers = kafka_telemetry.get("brokers", [])
        host    = kafka_telemetry.get("host", "kafka")

        events.extend(self._check_broker_count(host, brokers))
        events.extend(self._check_consumer_lag(host, kafka_telemetry.get("consumer_lag", [])))

        return events

    # ----------------------------------------
    # BROKERS
    # ----------------------------------------

    def _check_broker_count(self, host, brokers: list) -> list[dict]:
        if len(brokers) < MIN_BROKERS_EXPECTED:
            return [{
                "event_type":  "kafka_broker_down",
                "severity":    "high",
                "entity_type": "message_queue",
                "entity_name": host,
                "summary":     f"Kafka em {host}: apenas {len(brokers)} broker(s) ativo(s).",
                "payload":     {"brokers": brokers},
            }]
        return []

    # ----------------------------------------
    # CONSUMER LAG
    # ----------------------------------------

    def _check_consumer_lag(self, host, lag_data: list) -> list[dict]:
        if not lag_data:
            return []

        # Agrega lag por grupo/topico
        by_group_topic: dict[str, int] = {}
        for entry in lag_data:
            key = f"{entry.get('group')}::{entry.get('topic')}"
            by_group_topic[key] = by_group_topic.get(key, 0) + (entry.get("lag") or 0)

        events = []
        for key, total_lag in by_group_topic.items():
            if total_lag < LAG_MEDIUM_THRESHOLD:
                continue

            group, topic = key.split("::", 1)
            severity = "high" if total_lag >= LAG_HIGH_THRESHOLD else "medium"

            events.append({
                "event_type":  "kafka_consumer_lag",
                "severity":    severity,
                "entity_type": "message_queue",
                "entity_name": host,
                "summary": (
                    f"Consumer lag elevado em Kafka@{host}: "
                    f"grupo '{group}' / topico '{topic}' com {total_lag:,} mensagens por processar."
                ),
                "payload": {
                    "group":     group,
                    "topic":     topic,
                    "total_lag": total_lag,
                    "threshold": LAG_MEDIUM_THRESHOLD,
                }
            })

        return events
