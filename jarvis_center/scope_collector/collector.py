"""
Scope Collector — coletor separado do agente, que traz telemetria de fontes
externas (Zabbix, Splunk; agentless na Fase 4) apenas para os alvos que o
utilizador escolher explicitamente (scope_targets), e a envia para o próprio
pipeline de ingestão do Center (via sender.py), reaproveitando sem alterações
o BaselineEngine/Correlator/Predictor/AdaptiveThresholdEngine já existentes.

IMPORTANTE — como fala com o Zabbix/Splunk:
Não usa clientes HTTP próprios (zabbix_client.py/splunk_client.py) porque a
autenticação real destas integrações já está resolvida e testada nas tools
Clotho existentes (clotho_integrations.tools, geradas via clotho_tester.py),
que guardam o segredo em config["secret"] e sabem como o injectar em cada
pedido. Chamar essas tools (via o endpoint HTTP já existente
POST /fates/clotho/integrations/{id}/tools/{nome}/execute) evita duplicar
essa lógica de autenticação — que, ao tentar duplicar, já se revelou errada
uma vez (ver histórico da conversa).

Mapeamento categoria -> frame_type (ver plano):
  server      -> "metrics"       (cpu_percent/memory_percent/disk_percent/...)
  database    -> "db_telemetry"  ({database, slow_queries:[{avg_ms}], connection_pool_pct})
  application -> "web_telemetry" ({web_type, active_connections, requests, ...})
  network/security/business (Fase 5) -> "metrics" com chaves de namespace (net.*/sec.*/biz.*)
"""

import json
import os
import time

import requests

from clotho.clotho_store import ClothoStore
from scope_collector.scope_store import ScopeStore
from scope_collector.frame_builder import build_frame
from scope_collector.sender import ScopeSender
from scope_collector import host_status

_POLL_INTERVAL = 30  # segundos entre varreduras de scope_targets devidos
_TOOL_CALL_TIMEOUT_S = 20


def _center_base_url() -> str:
    host = os.getenv("JARVIS_API_HOST", "localhost")
    if host in ("0.0.0.0", ""):
        host = "localhost"
    port = os.getenv("JARVIS_API_PORT", "8080")
    return f"http://{host}:{port}/fates/clotho"


def _call_tool(integration_id: int, tool_name: str, params: dict) -> dict:
    """
    Chama uma tool Clotho já configurada (autenticação incluída) e devolve o
    corpo da resposta já parseado como JSON. Lança RuntimeError com uma
    mensagem clara em caso de falha de rede, HTTP, ou parsing.
    """
    api_key = os.getenv("JARVIS_API_KEY", "")
    if not api_key:
        raise RuntimeError("JARVIS_API_KEY não configurada")

    url = f"{_center_base_url()}/integrations/{integration_id}/tools/{tool_name}/execute"
    resp = requests.post(
        url,
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json={"params": params},
        timeout=_TOOL_CALL_TIMEOUT_S,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"chamada à tool '{tool_name}' falhou: HTTP {resp.status_code}")

    outer = resp.json()
    attempts = outer.get("attempts") or []
    if not attempts:
        raise RuntimeError(f"tool '{tool_name}' não devolveu nenhuma tentativa (mode={outer.get('mode')})")

    inner = attempts[0].get("response") or {}
    if inner.get("status_code") != 200:
        detail = inner.get("error") or f"HTTP {inner.get('status_code')}"
        raise RuntimeError(f"tool '{tool_name}': {detail}")

    body_snippet = inner.get("body_snippet") or ""
    try:
        return json.loads(body_snippet)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"tool '{tool_name}': resposta não é JSON válido ({e})")


# --------------------------------
# ZABBIX
# --------------------------------

def _default_zabbix_field_map(category: str) -> dict[str, str]:
    if category == "database":
        return {"avg_query_ms": "mysql.queries", "connections_pct": "mysql.threads_connected"}
    if category == "application":
        return {"active_connections": "net.tcp.listen", "requests": "web.requests.rate"}
    # server (omissão) — confirmado contra hosts Windows reais do BAI (template
    # standard Zabbix Agent para Windows): CPU/memória já vêm como % usada,
    # mas o item de disco reporta % LIVRE do drive C: (não % usada como no
    # resto do Jarvis) — ver _INVERT_PERCENT_FIELDS abaixo.
    return {
        "cpu_percent":    "system.cpu.util",
        "memory_percent": "vm.memory.util",
        "disk_percent":   "vfs.fs.size[C:,pfree]",
    }


# Campos cujo item Zabbix reporta o inverso do que o resto do Jarvis espera
# (ex: "% livre" em vez de "% usada") — invertidos em _poll_zabbix antes de
# entrarem no frame, para que BaselineEngine/Predictor recebam sempre "% usada".
_INVERT_PERCENT_FIELDS = {"disk_percent"}


def _update_host_status(target: dict, scope_store: ScopeStore | None, signals: list) -> None:
    if scope_store is None or not signals:
        return
    scope_store.upsert_host_status_signals(signals, scope_target_id=target["id"])


def _poll_zabbix(target: dict, clotho_store: ClothoStore, scope_store: ScopeStore | None = None) -> tuple[bool, str]:
    if not target.get("integration_id"):
        return False, "zabbix_host/zabbix_item requer integration_id (ligação Clotho)"

    # Sinal de "está em baixo/com problema" (distinto do sucesso da recolha de
    # métricas abaixo) — via a tool 'zabbix_get_active_problems', se a
    # integração a tiver configurada. Falha/ausência é silenciosa: o
    # heartbeat do agente continua a ser o sinal de saúde por omissão.
    _update_host_status(target, scope_store, host_status.adapt_zabbix(
        target, lambda name, params: _call_tool(target["integration_id"], name, params)
    ))

    integration = clotho_store.get_integration(target["integration_id"])
    if not integration:
        return False, f"integração {target['integration_id']} não encontrada"

    field_map = (integration.get("config") or {}).get("field_map") \
        or _default_zabbix_field_map(target.get("category", "server"))

    # 1 chamada por campo, filtrada por search_key — em vez de 1 chamada
    # genérica para todos os items do host. A tool tem um "limit": 50 sem
    # filtro, e hosts reais podem ter dezenas de items (ex: um "Incoming
    # network traffic on <interface>" por cada NIC virtual/física) que
    # empurram, por ordem alfabética, métricas relevantes (ex: memória)
    # para fora dos primeiros 50 — confirmado num servidor Windows real do
    # BAI, onde "Memory utilization %" desaparecia silenciosamente do
    # resultado sem o filtro.
    raw_values = {}
    for canonical_name, item_key in field_map.items():
        try:
            body = _call_tool(target["integration_id"], "zabbix_get_latest_metrics", {
                "hostid": target["target_ref"],
                "search_key": item_key,
            })
        except RuntimeError as e:
            return False, str(e)

        if body.get("error"):
            return False, f"Zabbix respondeu com erro: {body['error']}"

        item = next((i for i in (body.get("result") or []) if i.get("key_") == item_key), None)
        if item is None or item.get("lastvalue") is None:
            continue
        # lastclock=0 é o placeholder do Zabbix para um item que nunca foi
        # efectivamente populado — não é uma leitura real de "0". Ignorar,
        # senão um item nunca lido vira, por exemplo, um falso "disco 100% cheio".
        if not item.get("lastclock") or int(item["lastclock"]) == 0:
            continue

        try:
            value = float(item["lastvalue"])
        except (TypeError, ValueError):
            continue
        if canonical_name in _INVERT_PERCENT_FIELDS:
            value = 100.0 - value
        raw_values[canonical_name] = value

    if not raw_values:
        return False, f"nenhum item Zabbix encontrado para os keys configurados em field_map ({list(field_map.values())})"

    return _send_values(target, raw_values)


# --------------------------------
# SPLUNK
# --------------------------------

def _poll_splunk(target: dict, clotho_store: ClothoStore, scope_store: ScopeStore | None = None) -> tuple[bool, str]:
    if not target.get("integration_id"):
        return False, "splunk_search/splunk_index requer integration_id (ligação Clotho)"

    integration = clotho_store.get_integration(target["integration_id"])
    if not integration:
        return False, f"integração {target['integration_id']} não encontrada"

    if target["target_kind"] == "splunk_search":
        # target_ref é a query SPL completa, já desenhada para devolver uma
        # linha com campos com os nomes canónicos esperados pela categoria
        # (ex: "search index=win_srv_int host=X | stats avg(cpu_percent) as cpu_percent")
        spl = target["target_ref"]
    else:  # splunk_index
        spl = f"search index={target['target_ref']} | stats count as event_count"

    earliest = f"-{max(target.get('cadence_seconds', 60), 60)}s"

    try:
        body = _call_tool(target["integration_id"], "splunk_run_search", {
            "search": spl,
            "earliest_time": earliest,
            "latest_time": "now",
            "max_count": 1,
        })
    except RuntimeError as e:
        return False, str(e)

    results = body.get("results") or []

    # Sinal de "está em baixo/com problema" — só se a pesquisa já configurada
    # pelo utilizador devolver colunas host+status/severity reconhecíveis
    # (ver host_status.adapt_splunk). Não é um segundo pedido: reaproveita os
    # mesmos results já trazidos acima.
    if target["target_kind"] == "splunk_search":
        _update_host_status(target, scope_store, host_status.adapt_splunk(target, results))

    if not results:
        return False, "pesquisa Splunk não devolveu nenhuma linha de resultado"

    raw_values = {}
    for key, value in results[0].items():
        if key.startswith("_"):
            continue  # campos internos do Splunk (_time, _raw, _bkt, ...)
        try:
            raw_values[key] = float(value)
        except (TypeError, ValueError):
            continue

    if not raw_values:
        return False, "resultado do Splunk não tem nenhum campo numérico mapeável"

    return _send_values(target, raw_values)


# --------------------------------
# SHAPE + ENVIO (comum a Zabbix e Splunk)
# --------------------------------

def _shape_for_frame(target: dict, raw_values: dict) -> dict:
    frame_type = target.get("frame_type", "metrics")

    if frame_type == "db_telemetry":
        return {
            "database": target["name"],
            "slow_queries": [{"avg_ms": raw_values.get("avg_query_ms", 0)}],
            "connection_pool_pct": raw_values.get("connections_pct", 0),
        }

    if frame_type == "web_telemetry":
        return {"web_type": target["target_kind"].split("_")[0], **raw_values}

    # "metrics" (server, ou rede/seguranca/negocio com chaves de namespace)
    return raw_values


def _send_values(target: dict, raw_values: dict) -> tuple[bool, str]:
    values = _shape_for_frame(target, raw_values)
    frame = build_frame(target, values)
    return ScopeSender().send([frame])


# --------------------------------
# ORQUESTRAÇÃO
# --------------------------------

def poll_target(target: dict, clotho_store: ClothoStore, scope_store: ScopeStore | None = None) -> tuple[bool, str]:
    kind = target["target_kind"]

    if kind in ("zabbix_host", "zabbix_item"):
        return _poll_zabbix(target, clotho_store, scope_store)

    if kind in ("splunk_search", "splunk_index"):
        return _poll_splunk(target, clotho_store, scope_store)

    return False, f"target_kind '{kind}' ainda não suportado (agentless: Fase 4)"


def run_due_targets(scope_store: ScopeStore, clotho_store: ClothoStore) -> int:
    due = scope_store.due_targets()
    for target in due:
        ok, err = poll_target(target, clotho_store, scope_store)
        scope_store.record_poll_result(target["id"], "ok" if ok else f"error: {err}")
    return len(due)


def scope_collector_worker():
    """Thread de fundo, arrancada por start_center.py (padrão de lachesis_scheduler_worker)."""
    import start_center as _sc

    scope_store = ScopeStore()
    clotho_store = ClothoStore()

    while not _sc.shutdown_flag:
        time.sleep(_POLL_INTERVAL)
        try:
            run_due_targets(scope_store, clotho_store)
        except Exception as e:
            _sc.log(f"[SCOPE_COLLECTOR] erro no loop: {e}")
