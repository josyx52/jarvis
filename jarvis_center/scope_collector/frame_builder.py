"""
frame_builder — monta o envelope de frame que o Center já sabe consumir
(agent/agent_core/telemetry_schema.py), a partir de um scope_target e dos
valores já recolhidos de Zabbix/Splunk/agentless.

Não introduz nenhum frame_type novo: reutiliza "metrics", "db_telemetry" e
"web_telemetry" exactamente como o agente real os produz, para que
BaselineEngine/Correlator/Predictor/AdaptiveThresholdEngine funcionem sem
qualquer alteração no Center.

host.boot_id é sintetizado de forma estável a partir do target_ref (hash),
em vez de aleatório a cada arranque como faz o agente real — isto é
propositado: o Predictor só acumula tendência através de várias snapshots
do mesmo host_key ("hostname::boot_id"), por isso o boot_id tem de se manter
igual entre ciclos de polling do mesmo alvo.
"""

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

_AGENT_VERSION = "scope-collector-0.1.0"


def _stable_boot_id(target_ref: str) -> str:
    return "scope-" + hashlib.sha1(target_ref.encode("utf-8")).hexdigest()[:24]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_host(target: dict) -> dict[str, Any]:
    return {
        "hostname":      target.get("host_key_hint") or target["name"],
        "os":            "external",
        "os_version":    target.get("target_kind", "unknown"),
        "ip":            "0.0.0.0",
        "machine":       "scope_collector",
        "agent_version": _AGENT_VERSION,
        "boot_id":       _stable_boot_id(target["target_ref"]),
        "env":           "scope",
    }


def host_key(target: dict) -> str:
    host = build_host(target)
    return f"{host['hostname']}::{host['boot_id']}"


def build_frame(target: dict, values: dict, sequence: int = 1) -> dict[str, Any]:
    """
    values: dicionário já mapeado para o shape esperado pelo frame_type do
    target (ver mapeamento categoria -> frame_type no plano):
      - frame_type "metrics"       -> values é o próprio dict de métricas
                                       (cpu_percent/memory_percent/... ou,
                                       para rede/segurança/negócio, chaves
                                       com namespace "net.*"/"sec.*"/"biz.*")
      - frame_type "db_telemetry"  -> values = {database, slow_queries, connection_pool_pct}
      - frame_type "web_telemetry" -> values = {web_type, active_connections, requests, ...}
    """
    frame_type = target.get("frame_type", "metrics")
    host = build_host(target)

    if frame_type == "metrics":
        telemetry = {"metrics": {"timestamp": time.time(), **values}}
    else:
        # db_telemetry / web_telemetry: o Center espera o dict de dados
        # directamente em telemetry (ver build_specialized_frame no agente).
        telemetry = values

    return {
        "frame_type": frame_type,
        "timestamp":  _utc_now_iso(),
        "sequence":   sequence,
        "host":       host,
        "telemetry":  telemetry,
    }
