"""
host_status.py — adaptadores que traduzem o resultado de um poll do Scope
Collector num HostStatusSignal, para o Atropos (dashboard de estado de
infraestrutura, sem custo de tokens LLM) saber quais hosts o Zabbix/Splunk/
outro SIEM-EDR consideram em baixo ou com problema — distinto do que
scope_targets.last_status já mede ("consegui buscar métricas desta
integração").

Como collector.py já documenta (ver o aviso no topo desse ficheiro), a
autenticação de cada integração só deve ser resolvida uma vez, através das
tools Clotho já configuradas e testadas (POST .../tools/{nome}/execute) —
por isso estes adaptadores nunca falam directamente com zabbix_client.py/
splunk_client.py; recebem um `call_tool` já ligado à integração certa
(fornecido por collector.py) e só interpretam a resposta.

Adicionar uma fonte nova (ex: CrowdStrike) = um `adapt_crowdstrike()` aqui +
chamá-lo em collector.py.poll_target() — sem alterações no Atropos.
"""

from dataclasses import dataclass
from typing import Callable

_SEVERITY_ORDER = {"ok": 0, "unknown": 1, "warning": 2, "critical": 3}


@dataclass
class HostStatusSignal:
    host: str
    source: str          # zabbix | splunk | crowdstrike | ...
    severity: str         # ok | warning | critical | unknown
    message: str | None = None
    raw: dict | None = None


def worst_severity(a: str, b: str) -> str:
    return a if _SEVERITY_ORDER.get(a, 0) >= _SEVERITY_ORDER.get(b, 0) else b


# Escala de severidade do Zabbix: 0=not classified, 1=information,
# 2=warning, 3=average, 4=high, 5=disaster.
_ZABBIX_SEVERITY_MAP = {0: "ok", 1: "ok", 2: "warning", 3: "warning", 4: "critical", 5: "critical"}


def adapt_zabbix(target: dict, call_tool: Callable[[str, dict], dict]) -> list[HostStatusSignal]:
    """
    Espera uma tool Clotho chamada 'zabbix_get_active_problems' já configurada
    na integração (mesma convenção que 'zabbix_get_latest_metrics', usada por
    collector.py._poll_zabbix) — um wrapper autenticado de problem.get. Se a
    tool não existir para esta integração, devolve [] silenciosamente: nem
    todo scope_target Zabbix precisa deste sinal, o heartbeat do agente
    continua a ser o fallback.
    """
    try:
        body = call_tool("zabbix_get_active_problems", {"hostids": [target["target_ref"]]})
    except RuntimeError:
        return []

    if body.get("error"):
        return []

    problems = body.get("result") or []
    host_key = target.get("host_key_hint") or target["target_ref"]

    if not problems:
        return [HostStatusSignal(host=host_key, source="zabbix", severity="ok")]

    worst = "ok"
    messages = []
    for p in problems:
        worst = worst_severity(worst, _ZABBIX_SEVERITY_MAP.get(int(p.get("severity") or 0), "unknown"))
        if p.get("name"):
            messages.append(p["name"])

    return [HostStatusSignal(
        host=host_key,
        source="zabbix",
        severity=worst,
        message="; ".join(messages[:5]) or None,
        raw={"problems": problems},
    )]


def adapt_splunk(target: dict, results: list[dict]) -> list[HostStatusSignal]:
    """
    Só produz sinal quando a pesquisa Splunk (já desenhada pelo utilizador
    via Clotho para este target_kind='splunk_search') devolver colunas
    reconhecíveis — host + status/severity. Não existe um "problema padrão"
    universal em Splunk, por isso não se inventa aqui uma SPL genérica; é
    oportunista sobre o que a pesquisa já configurada devolver.
    """
    out = []
    for row in results:
        host = row.get("host") or row.get("Host")
        sev_raw = str(row.get("severity") or row.get("status") or "").strip().lower()
        if not host or not sev_raw:
            continue
        if sev_raw in ("critical", "high", "error", "down", "fail"):
            severity = "critical"
        elif sev_raw in ("warning", "medium", "degraded"):
            severity = "warning"
        elif sev_raw in ("ok", "info", "up", "normal", "success"):
            severity = "ok"
        else:
            severity = "unknown"
        out.append(HostStatusSignal(
            host=host,
            source="splunk",
            severity=severity,
            message=row.get("message") or row.get("name"),
            raw=row,
        ))
    return out
