"""
WebEngine — analisa telemetria de servidores web (web_telemetry frames).

Suporta: Nginx, IIS, Apache, HAProxy, Caddy, ou qualquer servidor
que reporte conexoes ativas, requests e erros.

Detecta:
  - Spike de erros (4xx/5xx)
  - Saturacao de conexoes
  - Queda brusca de requests (possivel downtime)
  - Sites IIS offline
"""


CONN_HIGH_THRESHOLD     = 500
CONN_CRITICAL_THRESHOLD = 1000
ERROR_RATE_THRESHOLD    = 0.05   # 5% de erros
REQ_DROP_THRESHOLD      = 0.5    # queda de 50% de requests vs anterior


class WebEngine:

    def __init__(self):
        self._prev: dict[str, dict] = {}  # host -> ultimo estado

    def analyze(self, web_telemetry: dict, host: str = "unknown") -> list[dict]:
        if not web_telemetry:
            return []

        events   = []
        web_type = web_telemetry.get("web_type", "unknown")

        events.extend(self._check_connections(web_type, host, web_telemetry))
        events.extend(self._check_errors(web_type, host, web_telemetry))
        events.extend(self._check_request_drop(web_type, host, web_telemetry))
        events.extend(self._check_sites(web_type, host, web_telemetry))

        self._prev[host] = web_telemetry
        return events

    # ----------------------------------------
    # CONEXOES ATIVAS
    # ----------------------------------------

    def _check_connections(self, web_type, host, data) -> list[dict]:
        active = data.get("active_connections") or data.get("current_connections") or 0

        if active < CONN_HIGH_THRESHOLD:
            return []

        severity = "high" if active >= CONN_CRITICAL_THRESHOLD else "medium"

        return [{
            "event_type":  "web_connection_saturation",
            "severity":    severity,
            "entity_type": "web_server",
            "entity_name": f"{web_type}@{host}",
            "summary": (
                f"Servidor web {web_type}@{host} com {active} conexoes ativas "
                f"(threshold: {CONN_HIGH_THRESHOLD})."
            ),
            "payload": {
                "web_type":          web_type,
                "host":              host,
                "active_connections": active,
                "threshold":         CONN_HIGH_THRESHOLD,
            }
        }]

    # ----------------------------------------
    # TAXA DE ERROS
    # ----------------------------------------

    def _check_errors(self, web_type, host, data) -> list[dict]:
        # Nginx: not_found_errors como proxy de erros
        # IIS: not_found_errors
        errors  = data.get("not_found_errors") or 0
        total   = data.get("requests") or data.get("requests_per_sec") or 0

        if total <= 0 or errors <= 0:
            return []

        rate = errors / total
        if rate < ERROR_RATE_THRESHOLD:
            return []

        return [{
            "event_type":  "web_high_error_rate",
            "severity":    "high" if rate >= 0.2 else "medium",
            "entity_type": "web_server",
            "entity_name": f"{web_type}@{host}",
            "summary": (
                f"Taxa de erro elevada em {web_type}@{host}: "
                f"{rate*100:.1f}% ({errors}/{total} requests com erro)."
            ),
            "payload": {
                "web_type":   web_type,
                "host":       host,
                "error_count": errors,
                "total":      total,
                "error_rate": round(rate, 3),
            }
        }]

    # ----------------------------------------
    # QUEDA BRUSCA DE REQUESTS
    # ----------------------------------------

    def _check_request_drop(self, web_type, host, data) -> list[dict]:
        prev = self._prev.get(host, {})
        if not prev:
            return []

        curr_req = data.get("requests") or data.get("requests_per_sec") or 0
        prev_req = prev.get("requests") or prev.get("requests_per_sec") or 0

        if prev_req <= 0 or curr_req >= prev_req * (1 - REQ_DROP_THRESHOLD):
            return []

        drop_pct = round((1 - curr_req / prev_req) * 100, 1)

        return [{
            "event_type":  "web_request_drop",
            "severity":    "high",
            "entity_type": "web_server",
            "entity_name": f"{web_type}@{host}",
            "summary": (
                f"Queda brusca de requests em {web_type}@{host}: "
                f"-{drop_pct}% (de {prev_req} para {curr_req}). "
                f"Possivel downtime ou redirecionamento."
            ),
            "payload": {
                "web_type":    web_type,
                "host":        host,
                "prev_req":    prev_req,
                "curr_req":    curr_req,
                "drop_pct":    drop_pct,
            }
        }]

    # ----------------------------------------
    # SITES IIS OFFLINE
    # ----------------------------------------

    def _check_sites(self, web_type, host, data) -> list[dict]:
        if web_type != "iis":
            return []

        events = []
        for site in data.get("sites", []):
            if site.get("connections", 0) == 0 and site.get("requests", 0) == 0:
                events.append({
                    "event_type":  "web_site_inactive",
                    "severity":    "medium",
                    "entity_type": "web_server",
                    "entity_name": f"iis@{host}",
                    "summary":     f"Site IIS '{site.get('name')}' sem conexoes ou requests.",
                    "payload":     {"site": site, "host": host},
                })

        return events
