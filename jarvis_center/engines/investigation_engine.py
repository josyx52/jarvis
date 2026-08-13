import os
import time

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

# Não investigar o mesmo alerta+host mais do que 1x por janela
_INVESTIGATION_WINDOW = 300  # 5 minutos

from brain.foundry_client import FoundryClient


class InvestigationEngine:

    def __init__(self):
        self.llm = FoundryClient()
        self._local_investigated: dict[str, float] = {}

    def _already_investigated(self, host: str, title: str) -> bool:
        key = f"jarvis:investigated:{host}:{title}"
        if _REDIS_OK and _REDIS is not None:
            try:
                return bool(_REDIS.exists(key))
            except Exception:
                pass
        last = self._local_investigated.get(key, 0)
        return (time.time() - last) < _INVESTIGATION_WINDOW

    def _mark_investigated(self, host: str, title: str):
        key = f"jarvis:investigated:{host}:{title}"
        if _REDIS_OK and _REDIS is not None:
            try:
                _REDIS.setex(key, _INVESTIGATION_WINDOW, 1)
                return
            except Exception:
                pass
        self._local_investigated[key] = time.time()

    def investigate(
        self,
        alerts: list[dict],
        snapshot: dict,
        events: list[dict],
        correlations: list[dict],
        topology: dict,
        predictions: list[dict],
        host: str = "",
        force: bool = False,
    ) -> list[dict]:
        """
        force=True ignora a janela de dedupe de 5 min (jarvis:investigated:{host}:{title}).
        Usar apenas para disparos manuais explícitos do utilizador (POST /investigations/trigger)
        — um pedido de "Investigar" clicado pelo utilizador nunca deve ser engolido em silêncio
        só porque o mesmo tipo de alerta já foi investigado há pouco tempo.
        """

        if not alerts:
            return []

        investigations = []

        for alert in alerts:
            title = alert.get("title", "")
            if not force and self._already_investigated(host, title):
                continue
            evidence = self._collect_evidence(
                alert,
                snapshot,
                events,
                correlations,
                topology,
                predictions
            )

            if not evidence:
                continue

            prompt = self._build_prompt(alert, evidence)

            try:
                response = self.llm.generate(prompt)
                self._mark_investigated(host, title)
            except Exception as e:
                response = f"Falha ao gerar investigação via LLM: {e}"

            investigations.append({
                "alert_type": alert.get("source_type", "unknown"),
                "severity": alert.get("severity", "unknown"),
                "summary": f"Investigação: {alert.get('title', 'alerta sem título')}",
                "details": response
            })

        return investigations

    def _collect_evidence(
        self,
        alert: dict,
        snapshot: dict,
        events: list[dict],
        correlations: list[dict],
        topology: dict,
        predictions: list[dict]
    ) -> dict:

        related_events = [
            e for e in events
            if e.get("severity") in ("high", "critical")
        ]

        related_corr = correlations

        # Use pre-sorted views from SnapshotBuilder (top 10 by CPU)
        # Avoids empty top_processes when CPU is distributed among many processes
        top_processes = [
            {
                "pid": p.get("pid"),
                "name": p.get("name"),
                "cpu_percent": self._to_float(
                    p.get("cpu_percent") or p.get("percent_processor_time")
                ),
                "resident_size_mb": round(
                    self._to_int(p.get("resident_size")) / 1_048_576, 1
                ),
            }
            for p in snapshot.get("cpu", [])[:10]
        ]

        network_activity = []

        for pid, remotes in topology.get("process_to_remotes", {}).items():
            if len(remotes) > 10:
                proc = topology.get("processes", {}).get(pid)

                if proc:
                    network_activity.append({
                        "pid": pid,
                        "process": proc.get("name"),
                        "connections": len(remotes),
                        "remotes": remotes
                    })

        return {
            "alert": alert,
            "events": related_events,
            "correlations": related_corr,
            "predictions": predictions,
            "top_processes": top_processes,
            "network_activity": network_activity
        }

    def _build_prompt(self, alert: dict, evidence: dict) -> str:

        return f"""
SYSTEM:
You are an infrastructure investigation AI.

Your task is to determine the most likely root cause of an alert.

Respond in Portuguese.
Be concise and technical.

ALERT:
{alert}

EVIDENCE:
{evidence}

TASK:

1. Identify the root cause
2. Explain the technical mechanism
3. Identify impacted services if possible
4. Suggest remediation steps

Return structured text with:
- Causa Raiz
- Mecanismo Técnico
- Impacto
- Remediação
"""

    def _to_int(self, value):

        try:
            return int(value)
        except Exception:
            return 0

    def _to_float(self, value):

        try:
            return float(value)
        except Exception:
            return 0.0