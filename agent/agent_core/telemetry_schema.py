from datetime import datetime
from typing import Any


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


def build_frame(
    frame_type: str,
    host: dict[str, Any],
    sequence: int,
    telemetry: dict[str, Any],
) -> dict[str, Any]:
    return {
        "frame_type": frame_type,
        "timestamp":  utc_now_iso(),
        "sequence":   sequence,
        "host":       host,
        "telemetry":  telemetry,
    }


def build_otel_frame(
    host: dict[str, Any],
    sequence: int,
    spans: list[dict],
) -> dict[str, Any]:
    """Spans OpenTelemetry recebidos pelo OtelReceiver."""
    return {
        "frame_type": "otel_trace",
        "timestamp":  utc_now_iso(),
        "sequence":   sequence,
        "host":       host,
        "telemetry": {
            "spans":      spans,
            "span_count": len(spans),
        }
    }


def build_execution_unit_frame(
    host: dict[str, Any],
    sequence: int,
    units: list[dict],
) -> dict[str, Any]:
    """
    Frame com execution units produzidas pelo CorrelationEngine do agente.
    Substitui o otel_trace quando o network probe está activo:
    em vez de spans brutos, envia comportamento já correlacionado.

    Cada unit contém:
      pid, process_name, trace_id, confidence, duration_ms,
      protocols, sources, events (kernel + rede + userspace unidos)
    """
    return {
        "frame_type": "execution_unit",
        "timestamp":  utc_now_iso(),
        "sequence":   sequence,
        "host":       host,
        "telemetry": {
            "units":      units,
            "unit_count": len(units),
        }
    }


def build_security_frame(
    host: dict[str, Any],
    sequence: int,
    events: list[dict],
) -> dict[str, Any]:
    """
    Frame com eventos de segurança do Windows Security Event Log.
    Alimenta o UEBA engine no center.

    Eventos incluídos: logon/logoff, privilege_use, process_start,
    service_install, scheduled_task.
    """
    return {
        "frame_type": "security_events",
        "timestamp":  utc_now_iso(),
        "sequence":   sequence,
        "host":       host,
        "telemetry": {
            "events":      events,
            "event_count": len(events),
        }
    }


def build_specialized_frame(
    frame_type: str,
    host: dict[str, Any],
    sequence: int,
    data: dict[str, Any],
) -> dict[str, Any]:
    """
    Frame para collectors especializados (db_telemetry, kafka_telemetry, web_telemetry).

    frame_type values:
      db_telemetry    — PostgreSQL, SQL Server, Oracle, MySQL
      kafka_telemetry — Apache Kafka
      web_telemetry   — Nginx, IIS, Apache
      mq_telemetry    — RabbitMQ, IBM MQ, ActiveMQ
    """
    return {
        "frame_type": frame_type,
        "timestamp":  utc_now_iso(),
        "sequence":   sequence,
        "host":       host,
        "telemetry":  data,
    }
