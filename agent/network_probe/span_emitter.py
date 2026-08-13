"""
SpanEmitter — emite spans OTel a partir de eventos capturados pelo probe.

Suporta três tipos de emissão:
  emit_connection_spans(conn_events)  — conexões TCP detectadas via ConnectionTracker
  emit_decoded_spans(decoded)         — pedidos decodificados: HTTP, SQL, Redis, etc.
  emit_execution_unit(unit)           — execution unit completa do CorrelationEngine

Envia para o OtelReceiver local (porta 4318) via HTTP JSON (OTLP).
Zero dependências externas — apenas stdlib Python.
"""

import json
import os
import random
import time
import urllib.request


class SpanEmitter:

    def __init__(self, endpoint: str = "http://localhost:4318"):
        self._url      = endpoint.rstrip("/") + "/v1/traces"
        self._hostname = os.environ.get("COMPUTERNAME", "unknown")

    # ─────────────────────────────────────────
    # EXECUTION UNIT (CorrelationEngine output)
    # ─────────────────────────────────────────

    def emit_execution_unit(self, unit: dict):
        """
        Emite uma execution unit completa como um trace OTel.
        Cada evento interno torna-se um span filho.
        O span raiz representa a execução completa do pedido.
        """
        trace_id = unit.get("trace_id") or format(random.getrandbits(128), "032x")
        root_id  = format(random.getrandbits(64), "016x")
        pid      = unit.get("pid", 0)
        proc     = unit.get("process_name", "unknown")
        now_ns   = int(unit.get("first_ts", time.time()) * 1_000_000_000)
        end_ns   = int(unit.get("last_ts",  time.time()) * 1_000_000_000)
        conf     = unit.get("confidence", "low")

        # Span raiz — representa o pedido completo
        root_span = {
            "traceId":           trace_id,
            "spanId":            root_id,
            "parentSpanId":      "",
            "name":              f"execution [{proc}]",
            "kind":              1,  # INTERNAL
            "startTimeUnixNano": str(now_ns),
            "endTimeUnixNano":   str(end_ns),
            "attributes": [
                {"key": "process.name",           "value": {"stringValue": proc}},
                {"key": "process.pid",            "value": {"intValue":    pid}},
                {"key": "host.name",              "value": {"stringValue": self._hostname}},
                {"key": "probe.confidence",       "value": {"stringValue": conf}},
                {"key": "probe.event_count",      "value": {"intValue":    unit.get("event_count", 0)}},
                {"key": "probe.sources",          "value": {"stringValue": ",".join(unit.get("sources", []))}},
                {"key": "probe.protocols",        "value": {"stringValue": ",".join(unit.get("protocols", []))}},
                {"key": "probe.type",             "value": {"stringValue": "execution_unit"}},
            ],
            "status": {"code": 1},
        }

        spans = [root_span]

        # Spans filhos — um por evento interno
        for ev in unit.get("events", []):
            child = self._event_to_span(ev, trace_id, root_id)
            if child:
                spans.append(child)

        self._send(spans, service_name=proc or "jarvis-probe")

    # ─────────────────────────────────────────
    # DECODED PACKETS (ProtocolDecoder output)
    # ─────────────────────────────────────────

    def emit_decoded_spans(self, decoded: list):
        by_service: dict[str, list] = {}

        for item in decoded:
            proto     = item.get("protocol", "unknown")
            span_name = item.get("span_name", f"{proto} request")
            now_ns    = int(item.get("timestamp", time.time()) * 1_000_000_000)
            trace_id  = format(random.getrandbits(128), "032x")
            span_id   = format(random.getrandbits(64),  "016x")

            attrs = [
                {"key": "net.peer.ip",   "value": {"stringValue": item.get("dst_ip", "")}},
                {"key": "net.peer.port", "value": {"intValue":    item.get("dst_port", 0)}},
                {"key": "net.host.name", "value": {"stringValue": self._hostname}},
                {"key": "probe.type",    "value": {"stringValue": "packet_decode"}},
            ]
            attrs += self._protocol_attrs(proto, item)

            svc = f"jarvis-probe-{proto}"
            by_service.setdefault(svc, []).append({
                "traceId":           trace_id,
                "spanId":            span_id,
                "parentSpanId":      "",
                "name":              span_name,
                "kind":              3,  # CLIENT
                "startTimeUnixNano": str(now_ns),
                "endTimeUnixNano":   str(now_ns + 1_000_000),
                "attributes":        attrs,
                "status":            {"code": 1},
            })

        for svc, spans in by_service.items():
            self._send(spans, service_name=svc)

    # ─────────────────────────────────────────
    # CONNECTION SPANS (ConnectionTracker output)
    # ─────────────────────────────────────────

    def emit_connection_spans(self, conn_events: list):
        spans = []
        for ev in conn_events:
            if ev.get("type") != "new_connection":
                continue
            now_ns = int(ev.get("timestamp", time.time()) * 1_000_000_000)
            spans.append({
                "traceId":           format(random.getrandbits(128), "032x"),
                "spanId":            format(random.getrandbits(64),  "016x"),
                "parentSpanId":      "",
                "name":              f"tcp.connect → {ev.get('remote_addr', '')}",
                "kind":              3,  # CLIENT
                "startTimeUnixNano": str(now_ns),
                "endTimeUnixNano":   str(now_ns + 1_000_000),
                "attributes": [
                    {"key": "net.peer.ip",    "value": {"stringValue": ev.get("remote_ip", "")}},
                    {"key": "net.peer.port",  "value": {"intValue":    ev.get("remote_port", 0)}},
                    {"key": "process.name",   "value": {"stringValue": ev.get("process", "")}},
                    {"key": "process.pid",    "value": {"intValue":    ev.get("pid", 0)}},
                    {"key": "net.host.name",  "value": {"stringValue": self._hostname}},
                    {"key": "probe.type",     "value": {"stringValue": "network_probe"}},
                ],
                "status": {"code": 1},
            })
        if spans:
            self._send(spans, service_name="jarvis-network-probe")

    # ─────────────────────────────────────────
    # ETW SPANS
    # ─────────────────────────────────────────

    def emit_etw_spans(self, etw_events: list):
        spans = []
        for ev in etw_events:
            if ev.get("type") != "etw_event":
                continue
            now_ns = int(ev.get("timestamp", time.time()) * 1_000_000_000)
            data   = ev.get("data", {})
            attrs  = [
                {"key": "etw.provider",  "value": {"stringValue": ev.get("provider", "")}},
                {"key": "etw.event_id",  "value": {"intValue":    ev.get("event_id", 0)}},
                {"key": "probe.type",    "value": {"stringValue": "etw_probe"}},
                {"key": "host.name",     "value": {"stringValue": self._hostname}},
            ]
            for field in ("ProcessName", "ImageFileName", "ProcessId", "QueryName"):
                if field in data:
                    attrs.append({"key": f"etw.{field.lower()}",
                                  "value": {"stringValue": str(data[field])}})
            spans.append({
                "traceId":           format(random.getrandbits(128), "032x"),
                "spanId":            format(random.getrandbits(64),  "016x"),
                "parentSpanId":      "",
                "name":              self._etw_name(ev),
                "kind":              1,
                "startTimeUnixNano": str(now_ns),
                "endTimeUnixNano":   str(now_ns + 100_000),
                "attributes":        attrs,
                "status":            {"code": 1},
            })
        if spans:
            self._send(spans, service_name="jarvis-etw-probe")

    # ─────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────

    def _event_to_span(self, ev: dict, trace_id: str, parent_id: str) -> dict | None:
        try:
            ts_ns = ev.get("timestamp_ns", int(time.time() * 1_000_000_000))
            name  = ev.get("operation_name") or ev.get("event_type", "event")
            attrs = [
                {"key": "probe.source",    "value": {"stringValue": ev.get("source", "")}},
                {"key": "probe.type",      "value": {"stringValue": "execution_unit_event"}},
                {"key": "probe.confidence","value": {"stringValue": ev.get("correlation_confidence", "low")}},
            ]

            net = ev.get("network")
            if net:
                attrs += [
                    {"key": "net.peer.ip",   "value": {"stringValue": net.get("dst_ip", "")}},
                    {"key": "net.peer.port", "value": {"intValue":    net.get("dst_port", 0)}},
                ]

            payload = ev.get("payload")
            if payload:
                proto = payload.get("protocol", "")
                attrs += self._protocol_attrs(proto, payload)

            return {
                "traceId":           trace_id,
                "spanId":            format(random.getrandbits(64), "016x"),
                "parentSpanId":      parent_id,
                "name":              name,
                "kind":              1,
                "startTimeUnixNano": str(ts_ns),
                "endTimeUnixNano":   str(ts_ns + 1_000_000),
                "attributes":        attrs,
                "status":            {"code": 1},
            }
        except Exception:
            return None

    def _protocol_attrs(self, proto: str, item: dict) -> list:
        if proto == "http":
            attrs = [
                {"key": "http.method",      "value": {"stringValue": item.get("method", "")}},
                {"key": "http.target",      "value": {"stringValue": item.get("path", "")}},
                {"key": "http.host",        "value": {"stringValue": item.get("host", "")}},
            ]
            if item.get("status_code"):
                attrs.append({"key": "http.status_code",
                               "value": {"intValue": item["status_code"]}})
            return attrs

        if proto in ("postgresql", "mysql", "mongodb"):
            return [
                {"key": "db.system",    "value": {"stringValue": proto}},
                {"key": "db.statement", "value": {"stringValue": item.get("query", "")[:500]}},
                {"key": "db.operation", "value": {"stringValue": item.get("query_type", "")}},
            ]

        if proto == "redis":
            return [
                {"key": "db.system",    "value": {"stringValue": "redis"}},
                {"key": "db.operation", "value": {"stringValue": item.get("command", "")}},
                {"key": "db.redis.key", "value": {"stringValue": item.get("key", "")}},
            ]

        if proto == "kafka":
            return [
                {"key": "messaging.system",    "value": {"stringValue": "kafka"}},
                {"key": "messaging.operation", "value": {"stringValue": item.get("operation", "")}},
            ]

        return []

    def _etw_name(self, ev: dict) -> str:
        provider = ev.get("provider", "")
        event_id = ev.get("event_id", 0)
        data     = ev.get("data", {})
        if "Process" in provider:
            proc   = data.get("ProcessName") or data.get("ImageFileName", "")
            action = "start" if event_id in (1, 3) else "exit"
            return f"process.{action} {proc}".strip()
        if "DNS" in provider:
            return f"dns.query {data.get('QueryName', '')}".strip()
        return f"etw.event id={event_id}"

    # ─────────────────────────────────────────
    # SEND
    # ─────────────────────────────────────────

    def _send(self, spans: list, service_name: str):
        try:
            payload = json.dumps({
                "resourceSpans": [{
                    "resource": {
                        "attributes": [
                            {"key": "service.name",       "value": {"stringValue": service_name}},
                            {"key": "host.name",          "value": {"stringValue": self._hostname}},
                            {"key": "telemetry.sdk.name", "value": {"stringValue": "jarvis-probe"}},
                        ]
                    },
                    "scopeSpans": [{"spans": spans}],
                }]
            }).encode("utf-8")

            req = urllib.request.Request(
                self._url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp.read()
        except Exception:
            pass
