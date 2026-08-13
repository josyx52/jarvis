import time
import os

try:
    import redis as _redis_mod
    _REDIS = _redis_mod.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    _REDIS.ping()
    _REDIS_OK = True
except Exception:
    _REDIS = None
    _REDIS_OK = False


# Janela de supressão por tipo de alerta (segundos).
# Estado partilhado via Redis — válido para todos os 8 workers.
_SUPPRESS_WINDOW = {
    "service_stopped":                   300,
    "disk_low_space":                    600,
    "disk_usage_critical":               600,
    "disk_pressure_with_process_growth": 300,
    "memory_high":                       120,
    "cpu_high":                          120,
    "cpu_saturation_trend":              300,
    "network_throughput_spike":           60,
    # Alertas de saúde interna do agente
    "agent_component_error":             300,
    "instrumentation_failed":            600,
    "dll_not_compiled":                 3600,
    # Alertas UEBA / segurança comportamental
    "behavioral_anomaly":                900,   # 15 min — análise UEBA suspeita
    "non_compliant_activity":            600,   # 10 min — actividade não conforme
    "suspicious_process":                300,   # 5 min — processo suspeito detectado
    "privilege_abuse":                   600,   # 10 min — abuso de privilégios
    "high_risk_user":                   1800,   # 30 min — utilizador com risk score crítico
    "after_hours_activity":             3600,   # 1h — actividade fora de horas
}
_DEFAULT_SUPPRESS_WINDOW = 60


class AlertManager:
    def __init__(self):
        # fallback local caso Redis não esteja disponível
        self._last_fired: dict[str, float] = {}

    def _redis_key(self, host: str, title: str) -> str:
        return f"jarvis:alert:suppress:{host}:{title}"

    def _is_suppressed(self, title: str, host: str = "") -> bool:
        window = _SUPPRESS_WINDOW.get(title, _DEFAULT_SUPPRESS_WINDOW)
        if _REDIS_OK and _REDIS is not None:
            try:
                return bool(_REDIS.exists(self._redis_key(host, title)))
            except Exception:
                pass
        # fallback local
        last = self._last_fired.get(f"{host}:{title}", 0)
        return (time.time() - last) < window

    def _record(self, title: str, host: str = ""):
        window = _SUPPRESS_WINDOW.get(title, _DEFAULT_SUPPRESS_WINDOW)
        if _REDIS_OK and _REDIS is not None:
            try:
                _REDIS.setex(self._redis_key(host, title), window, 1)
                return
            except Exception:
                pass
        self._last_fired[f"{host}:{title}"] = time.time()

    def generate_alerts(self, events: list[dict], correlations: list[dict], predictions: list[dict], host: str = "") -> list[dict]:
        alerts = []

        for event in events:
            if event["severity"] not in ("high", "critical"):
                continue
            title = event["event_type"]
            if self._is_suppressed(title, host):
                continue
            self._record(title, host)
            alerts.append({
                "source_type": "event",
                "severity":    event["severity"],
                "title":       title,
                "message":     event["summary"],
                "payload":     event,
            })

        for corr in correlations:
            if corr["severity"] not in ("high", "critical"):
                continue
            title = corr["correlation_type"]
            if self._is_suppressed(title, host):
                continue
            self._record(title, host)
            alerts.append({
                "source_type": "correlation",
                "severity":    corr["severity"],
                "title":       title,
                "message":     corr["summary"],
                "payload":     corr,
            })

        for pred in predictions:
            if pred["severity"] not in ("high", "critical"):
                continue
            title = pred["prediction_type"]
            if self._is_suppressed(title, host):
                continue
            self._record(title, host)
            alerts.append({
                "source_type": "prediction",
                "severity":    pred["severity"],
                "title":       title,
                "message":     pred["summary"],
                "payload":     pred,
            })

        return alerts

    def generate_agent_health_alerts(self, health_report: dict, host: str = "") -> list[dict]:
        """
        Processa um relatório de saúde do agente (POST /agent/health) e gera
        alertas para componentes em estado error ou missing.

        health_report = {
            "overall":    "ok" | "degraded",
            "components": {
                "otel_receiver":  {"status": "ok"|"error"|"missing"|"degraded"|"warning", "detail": "...", "fix": "..."},
                "etw":            {...},
                ...
            },
            "instrumentation": {"failed": [...], "ok": [...]},
        }
        """
        alerts = []
        components = health_report.get("components", {})

        for comp_name, comp in components.items():
            status = comp.get("status", "ok")
            if status not in ("error", "missing"):
                continue

            detail = comp.get("detail", "")
            fix    = comp.get("fix", "")

            # DLL não compilada tem janela de supressão longa (1h) — avisa uma vez por turno
            if comp_name == "dll_injection" and status == "missing":
                alert_type = "dll_not_compiled"
                severity   = "medium"
            else:
                alert_type = "agent_component_error"
                severity   = "high" if status == "error" else "medium"

            key = f"{alert_type}:{comp_name}"
            if self._is_suppressed(key, host):
                continue
            self._record(key, host)

            msg = f"[{comp_name}] {detail}"
            if fix:
                msg += f" — FIX: {fix}"

            alerts.append({
                "source_type": "agent_health",
                "severity":    severity,
                "title":       alert_type,
                "message":     msg,
                "payload":     {"component": comp_name, **comp},
            })

        # Falhas de instrumentação sitecustomize.py
        failed = health_report.get("instrumentation", {}).get("failed", [])
        if failed:
            key = "instrumentation_failed"
            if not self._is_suppressed(key, host):
                self._record(key, host)
                names = ", ".join(
                    f"{f.get('name', '?')}(pid={f.get('pid', '?')})"
                    for f in failed[:5]
                )
                alerts.append({
                    "source_type": "agent_health",
                    "severity":    "high",
                    "title":       "instrumentation_failed",
                    "message":     f"{len(failed)} processo(s) Python não instrumentado(s): {names}",
                    "payload":     {"failed": failed[:10]},
                })

        return alerts