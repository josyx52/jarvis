"""
sender — envia frames do Scope Collector para o próprio Center, usando
exactamente o mesmo transporte que o agente real (agent/agent_core/core_client.py):
gzip do JSON + POST /telemetry com X-API-Key.

O Scope Collector corre dentro do processo do Center (ver start_center.py),
por isso o endpoint por omissão aponta para localhost na porta da API.
"""

import gzip
import json
import os

import requests

_TIMEOUT_S = 10


class ScopeSender:

    def __init__(self):
        host = os.getenv("JARVIS_API_HOST", "localhost")
        # "0.0.0.0" não é um destino válido para o cliente ligar-se a si próprio
        if host in ("0.0.0.0", ""):
            host = "localhost"
        port = os.getenv("JARVIS_API_PORT", "8080")
        self.url = os.getenv("SCOPE_COLLECTOR_TELEMETRY_URL", f"http://{host}:{port}/telemetry")
        self.api_key = os.getenv("JARVIS_API_KEY", "")

    def send(self, frames: list[dict]) -> tuple[bool, str]:
        if not self.api_key:
            return False, "JARVIS_API_KEY não configurada"

        try:
            raw = json.dumps(frames).encode("utf-8")
            compressed = gzip.compress(raw)

            resp = requests.post(
                self.url,
                data=compressed,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                    "X-API-Key": self.api_key,
                },
                timeout=_TIMEOUT_S,
            )

            if resp.status_code == 200:
                return True, ""
            return False, f"HTTP {resp.status_code}: {resp.text[:300]}"

        except Exception as e:
            return False, str(e)
