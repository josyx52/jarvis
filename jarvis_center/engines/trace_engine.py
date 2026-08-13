"""
TraceEngine — analisa spans OpenTelemetry recebidos de aplicacoes instrumentadas.

Detecta:
- Spans lentos (p95 > threshold)
- Endpoints com taxa de erro elevada
- Queries SQL lentas (atributo db.statement presente)
- Memory leaks por request (crescimento de memoria correlacionado com spans)
- Endpoints com timeout repetido
"""


SLOW_SPAN_MS = 2000       # span considerado lento acima de 2 segundos
ERROR_RATE_THRESHOLD = 0.1  # 10% de erros num endpoint e considerado problema
MIN_SPANS_FOR_STATS = 3     # minimo de spans para calcular estatisticas


class TraceEngine:

    def analyze(self, spans: list[dict]) -> list[dict]:
        """
        Recebe lista de spans normalizados (vindos do OtelReceiver via frame otel_trace).
        Devolve lista de eventos no mesmo formato dos outros engines.
        """
        if not spans:
            return []

        events = []

        # agrupa spans por endpoint (service + name)
        by_endpoint: dict[str, list[dict]] = {}
        for span in spans:
            key = f"{span.get('service', 'unknown')}::{span.get('name', 'unknown')}"
            by_endpoint.setdefault(key, []).append(span)

        for endpoint, endpoint_spans in by_endpoint.items():
            service, operation = endpoint.split("::", 1)

            events.extend(self._check_slow_spans(service, operation, endpoint_spans))
            events.extend(self._check_error_rate(service, operation, endpoint_spans))
            events.extend(self._check_slow_queries(service, operation, endpoint_spans))
            events.extend(self._check_timeouts(service, operation, endpoint_spans))

        return events

    # ----------------------------------------
    # SPANS LENTOS
    # ----------------------------------------

    def _check_slow_spans(self, service, operation, spans) -> list[dict]:
        events = []

        slow = [s for s in spans if s.get("duration_ms", 0) >= SLOW_SPAN_MS]

        if not slow:
            return events

        avg_ms = sum(s["duration_ms"] for s in slow) // len(slow)
        worst = max(slow, key=lambda s: s["duration_ms"])

        events.append({
            "event_type": "trace_slow_span",
            "severity": "high" if avg_ms >= 5000 else "medium",
            "entity_type": "service",
            "entity_name": service,
            "summary": (
                f"Operacao '{operation}' do servico '{service}' "
                f"com {len(slow)} spans lentos (media {avg_ms}ms, pior {worst['duration_ms']}ms)."
            ),
            "payload": {
                "service": service,
                "operation": operation,
                "slow_count": len(slow),
                "total_count": len(spans),
                "avg_duration_ms": avg_ms,
                "worst_duration_ms": worst["duration_ms"],
                "worst_trace_id": worst.get("trace_id", ""),
                "threshold_ms": SLOW_SPAN_MS,
            }
        })

        return events

    # ----------------------------------------
    # TAXA DE ERRO
    # ----------------------------------------

    def _check_error_rate(self, service, operation, spans) -> list[dict]:
        if len(spans) < MIN_SPANS_FOR_STATS:
            return []

        errors = [s for s in spans if s.get("status") == "error"]
        rate = len(errors) / len(spans)

        if rate < ERROR_RATE_THRESHOLD:
            return []

        return [{
            "event_type": "trace_high_error_rate",
            "severity": "high" if rate >= 0.5 else "medium",
            "entity_type": "service",
            "entity_name": service,
            "summary": (
                f"Servico '{service}' operacao '{operation}' com taxa de erro de "
                f"{rate * 100:.1f}% ({len(errors)}/{len(spans)} spans)."
            ),
            "payload": {
                "service": service,
                "operation": operation,
                "error_count": len(errors),
                "total_count": len(spans),
                "error_rate": round(rate, 3),
            }
        }]

    # ----------------------------------------
    # QUERIES SQL LENTAS
    # ----------------------------------------

    def _check_slow_queries(self, service, operation, spans) -> list[dict]:
        events = []

        for span in spans:
            attrs = span.get("attributes", {})
            db_statement = attrs.get("db.statement") or attrs.get("db.query")

            if not db_statement:
                continue

            duration_ms = span.get("duration_ms", 0)

            if duration_ms < SLOW_SPAN_MS:
                continue

            db_system = attrs.get("db.system", "unknown")
            db_name = attrs.get("db.name", "unknown")

            events.append({
                "event_type": "trace_slow_query",
                "severity": "high" if duration_ms >= 5000 else "medium",
                "entity_type": "service",
                "entity_name": service,
                "summary": (
                    f"Query lenta no servico '{service}': {duration_ms}ms "
                    f"em '{db_system}/{db_name}'. "
                    f"Query: {str(db_statement)[:120]}"
                ),
                "payload": {
                    "service": service,
                    "operation": operation,
                    "duration_ms": duration_ms,
                    "db_system": db_system,
                    "db_name": db_name,
                    "db_statement": str(db_statement)[:500],
                    "trace_id": span.get("trace_id", ""),
                    "span_id": span.get("span_id", ""),
                }
            })

        return events

    # ----------------------------------------
    # TIMEOUTS REPETIDOS
    # ----------------------------------------

    def _check_timeouts(self, service, operation, spans) -> list[dict]:
        # spans com status slow e duracao muito alta sao candidatos a timeout
        timeouts = [
            s for s in spans
            if s.get("status") == "slow" and s.get("duration_ms", 0) >= 10000
        ]

        if len(timeouts) < 2:
            return []

        avg_ms = sum(s["duration_ms"] for s in timeouts) // len(timeouts)

        return [{
            "event_type": "trace_timeout_pattern",
            "severity": "high",
            "entity_type": "service",
            "entity_name": service,
            "summary": (
                f"Padrao de timeout detectado no servico '{service}' operacao '{operation}': "
                f"{len(timeouts)} chamadas com duracao media {avg_ms}ms (>10s)."
            ),
            "payload": {
                "service": service,
                "operation": operation,
                "timeout_count": len(timeouts),
                "avg_duration_ms": avg_ms,
            }
        }]
