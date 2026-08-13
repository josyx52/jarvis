"""
OTel Receiver — recebe spans via HTTP OTLP (porta 4318) de aplicacoes
instrumentadas com opentelemetry-sdk e transforma-os em frames Jarvis.

As aplicacoes instrumentadas enviam para:
  http://localhost:4318/v1/traces

Este receiver abre um servidor HTTP leve nessa porta e acumula os spans
recebidos. O RuntimeAgent vai busca-los periodicamente via drain().
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

_log = logging.getLogger(__name__)


_MAX_PENDING = 10_000


class OtelReceiver:

    def __init__(self, host: str = "0.0.0.0", port: int = 4318):
        self._host = host
        self._port = port
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ----------------------------------------
    # START / STOP
    # ----------------------------------------

    def start(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length)
                    data = json.loads(body)
                    spans = receiver._extract_spans(data)
                    if spans:
                        with receiver._lock:
                            space = _MAX_PENDING - len(receiver._pending)
                            if space >= len(spans):
                                receiver._pending.extend(spans)
                                dropped = 0
                            elif space > 0:
                                receiver._pending.extend(spans[:space])
                                dropped = len(spans) - space
                            else:
                                dropped = len(spans)
                        if dropped:
                            _log.warning(
                                "[OtelReceiver] Buffer cheio — %d span(s) descartado(s) "
                                "(limite: %d)",
                                dropped, _MAX_PENDING,
                            )
                    self.send_response(200)
                    self.end_headers()
                except Exception:
                    self.send_response(400)
                    self.end_headers()

            def log_message(self, fmt, *args):
                pass  # silencia logs HTTP

        self._server = HTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="otel-receiver"
        )
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()

    # ----------------------------------------
    # DRAIN — devolve spans acumulados e limpa
    # ----------------------------------------

    def drain(self) -> list[dict]:
        with self._lock:
            spans = self._pending[:]
            self._pending.clear()
        return spans

    # ----------------------------------------
    # PARSE OTLP JSON → spans normalizados
    # ----------------------------------------

    def _extract_spans(self, payload: dict) -> list[dict]:
        """
        OTLP JSON format:
        {
          "resourceSpans": [{
            "resource": { "attributes": [...] },
            "scopeSpans": [{
              "spans": [{
                "traceId": "...",
                "spanId": "...",
                "parentSpanId": "...",
                "name": "...",
                "kind": 2,
                "startTimeUnixNano": "...",
                "endTimeUnixNano": "...",
                "status": { "code": 0 },
                "attributes": [...]
              }]
            }]
          }]
        }
        """
        results = []

        for resource_span in payload.get("resourceSpans", []):
            # extrair atributos do resource (service.name, etc.)
            resource_attrs = self._attrs_to_dict(
                resource_span.get("resource", {}).get("attributes", [])
            )
            service_name = resource_attrs.get("service.name", "unknown")

            for scope_span in resource_span.get("scopeSpans", []):
                for span in scope_span.get("spans", []):
                    start_ns = int(span.get("startTimeUnixNano", 0) or 0)
                    end_ns = int(span.get("endTimeUnixNano", 0) or 0)
                    duration_ms = max(0, (end_ns - start_ns) // 1_000_000)

                    status_code = (span.get("status") or {}).get("code", 0)
                    # OTLP: 0=unset, 1=ok, 2=error
                    if status_code == 2:
                        status = "error"
                    elif duration_ms > 5000:
                        status = "slow"
                    else:
                        status = "ok"

                    span_attrs = self._attrs_to_dict(span.get("attributes", []))

                    results.append({
                        "trace_id": span.get("traceId", ""),
                        "span_id": span.get("spanId", ""),
                        "parent_span_id": span.get("parentSpanId", ""),
                        "service": service_name,
                        "name": span.get("name", ""),
                        "duration_ms": duration_ms,
                        "status": status,
                        "attributes": span_attrs,
                    })

        return results

    def _attrs_to_dict(self, attributes: list) -> dict:
        result = {}
        for attr in attributes or []:
            key = attr.get("key", "")
            value_obj = attr.get("value", {})
            # OTLP value pode ser stringValue, intValue, boolValue, doubleValue
            for vtype in ("stringValue", "intValue", "boolValue", "doubleValue"):
                if vtype in value_obj:
                    result[key] = value_obj[vtype]
                    break
        return result
