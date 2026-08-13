"""
Jarvis Python Code Module — sitecustomize.py

Carregado automaticamente pelo Python quando PYTHONPATH inclui este directório.
O ProcessInjector injeta o PYTHONPATH apenas no processo alvo — nunca global.

Instrumenta Flask, Django, SQLAlchemy, requests, psycopg2, urllib3.
Envia spans em OTLP JSON para o OtelReceiver local (porta 4318).
"""

import json
import os
import urllib.request

# Sentinel: ProcessInjector verifica estes ficheiros 30s após injecção para confirmar
# que o OTel carregou com sucesso.
#   C:\ProgramData\JarvisAgent\py_loaded\{pid}       — sucesso (contém service_name)
#   C:\ProgramData\JarvisAgent\py_loaded\{pid}.fail  — falha  (contém mensagem de erro)
_SENTINEL_DIR = os.path.join(
    os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
    "JarvisAgent", "py_loaded",
)


def _write_sentinel_ok(service_name: str):
    try:
        os.makedirs(_SENTINEL_DIR, exist_ok=True)
        with open(os.path.join(_SENTINEL_DIR, str(os.getpid())), "w", encoding="utf-8") as f:
            f.write(service_name)
    except Exception:
        pass


def _write_sentinel_fail(reason: str):
    try:
        os.makedirs(_SENTINEL_DIR, exist_ok=True)
        with open(os.path.join(_SENTINEL_DIR, str(os.getpid()) + ".fail"), "w", encoding="utf-8") as f:
            f.write(reason[:500])
    except Exception:
        pass


# Não instrumentar o próprio agente Jarvis
def _is_jarvis_process() -> bool:
    try:
        import sys
        argv0 = (sys.argv[0] if sys.argv else "").lower()
        return "jarvis" in argv0 or "agent_daemon" in argv0
    except Exception:
        return False


# ── Exporter JSON (OTLP JSON → otel_receiver) ────────────────────────────────

class _JarvisJsonExporter:
    """Envia spans como OTLP JSON para o otel_receiver local."""

    def __init__(self, endpoint: str):
        self._url = endpoint.rstrip("/") + "/v1/traces"

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult
        try:
            data = json.dumps(self._to_otlp_json(spans)).encode()
            req  = urllib.request.Request(
                self._url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
            return SpanExportResult.SUCCESS
        except Exception:
            return SpanExportResult.FAILURE

    def _to_otlp_json(self, spans) -> dict:
        buckets: dict = {}
        for span in spans:
            rid = id(span.resource)
            if rid not in buckets:
                buckets[rid] = {
                    "resource": {
                        "attributes": [
                            {"key": k, "value": {"stringValue": str(v)}}
                            for k, v in span.resource.attributes.items()
                        ]
                    },
                    "scopeSpans": [{"spans": []}],
                }
            buckets[rid]["scopeSpans"][0]["spans"].append({
                "traceId":      format(span.context.trace_id, "032x"),
                "spanId":       format(span.context.span_id, "016x"),
                "parentSpanId": format(span.parent.span_id, "016x") if span.parent else "",
                "name":         span.name,
                "kind":         span.kind.value,
                "startTimeUnixNano": str(span.start_time),
                "endTimeUnixNano":   str(span.end_time or span.start_time),
                "status":       {"code": span.status.status_code.value},
                "attributes": [
                    {"key": k, "value": {"stringValue": str(v)}}
                    for k, v in (span.attributes or {}).items()
                ],
            })
        return {"resourceSpans": list(buckets.values())}

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# ── Setup ─────────────────────────────────────────────────────────────────────

def _setup():
    if _is_jarvis_process():
        return

    # Garantir que os OTel packages bundled em code_modules/lib/ são encontrados
    # antes de qualquer import, independentemente do venv da aplicação alvo.
    try:
        import sys as _sys
        _lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
        if os.path.isdir(_lib_dir) and _lib_dir not in _sys.path:
            _sys.path.insert(0, _lib_dir)
    except Exception:
        pass

    try:
        endpoint     = os.environ.get("JARVIS_OTEL_ENDPOINT", "http://localhost:4318")
        service_name = _detect_service_name()

        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME

        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(_JarvisJsonExporter(endpoint)))
        trace.set_tracer_provider(provider)

        _instrument_flask()
        _instrument_django()
        _instrument_sqlalchemy()
        _instrument_requests()
        _instrument_psycopg2()
        _instrument_urllib3()

        # Sentinel de sucesso — ProcessInjector confirma instrumentação em 30s
        _write_sentinel_ok(service_name)

    except Exception as _e:
        # Nunca quebrar a aplicação — mas registar a falha para o Jarvis detectar
        _write_sentinel_fail(f"{type(_e).__name__}: {_e}")
        pass


def _detect_service_name() -> str:
    explicit = os.environ.get("OTEL_SERVICE_NAME")
    if explicit:
        return explicit
    try:
        import sys, pathlib
        if sys.argv and sys.argv[0]:
            name = pathlib.Path(sys.argv[0]).stem
            if name and name not in ("", "-c", "__main__"):
                return name
    except Exception:
        pass
    try:
        import psutil
        return psutil.Process().name().replace(".exe", "").replace(".py", "")
    except Exception:
        pass
    return "python-service"


def _instrument_flask():
    try:
        from opentelemetry.instrumentation.flask import FlaskInstrumentation
        FlaskInstrumentation().instrument()
    except Exception:
        pass


def _instrument_django():
    try:
        from opentelemetry.instrumentation.django import DjangoInstrumentation
        DjangoInstrumentation().instrument()
    except Exception:
        pass


def _instrument_sqlalchemy():
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentation
        SQLAlchemyInstrumentation().instrument()
    except Exception:
        pass


def _instrument_requests():
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentation
        RequestsInstrumentation().instrument()
    except Exception:
        pass


def _instrument_psycopg2():
    try:
        from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentation
        Psycopg2Instrumentation().instrument()
    except Exception:
        pass


def _instrument_urllib3():
    try:
        from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentation
        URLLib3Instrumentation().instrument()
    except Exception:
        pass


_setup()
