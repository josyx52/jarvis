"""
Scope targets — interpretação de instruções em linguagem natural via LLM.

Segue o mesmo padrão de jarvis_center/lachesis/lachesis_tasks_llm.py
(parse_schedule_description): AnthropicFoundry, prompt JSON-only, remoção
de markdown fences. Em vez de escolher manualmente "Tipo de alvo" +
"Referência" em dropdowns, o utilizador descreve o alvo em texto livre
(com $nome$ para referenciar a integração) e o LLM deduz os campos
estruturados que o scope_collector precisa para ir buscar telemetria real.
"""

import json
import os
import re

_VALID_TARGET_KINDS = {"zabbix_host", "zabbix_item", "splunk_index", "splunk_search", "agentless_host"}
_VALID_CATEGORIES = {"server", "application", "database", "network", "security", "business"}
_VALID_OS_TYPES = {"windows", "linux", "network_device", "unknown"}
_VALID_FRAME_TYPES = {"metrics", "db_telemetry", "web_telemetry"}

_TARGET_SYSTEM = """Você é o Explorer, o motor exploratório proactivo do Jarvis Fates Engine.

O utilizador descreve em linguagem natural (português) o que quer monitorizar.
Converta essa descrição num descritor JSON estruturado do alvo, para que o
recolector de telemetria saiba exactamente que chamada fazer.

Campos a devolver:
  target_kind — um de "zabbix_host", "zabbix_item", "splunk_index", "splunk_search",
    "agentless_host". Depende do tipo da integração indicada no contexto: integração
    Zabbix → "zabbix_host" (métricas gerais do host) ou "zabbix_item" (um item
    específico); integração Splunk → "splunk_index" ou "splunk_search"; sem
    integração, ou pedido explícito de acesso directo à máquina (WinRM/SSH) →
    "agentless_host".
  target_ref — a referência concreta a usar: hostid ou nome do host Zabbix, nome de
    índice ou query SPL do Splunk, ou hostname para agentless. Extrai isto
    literalmente do texto do utilizador (nomes de servidor, queries, etc.) — nunca
    inventes um valor que não esteja implícito no texto.
  category — um de "server", "application", "database", "network", "security",
    "business", a categoria que melhor descreve o que está a ser monitorizado, ou
    null se não for possível determinar com confiança.
  os_type — um de "windows", "linux", "network_device", "unknown".
  frame_type — um de "metrics", "db_telemetry", "web_telemetry" ("metrics" por
    omissão, salvo se o texto descrever claramente telemetria de base de dados ou
    de aplicação web).

Responda APENAS com JSON válido, sem markdown:
{"target_kind": "...", "target_ref": "...", "category": "..."|null, "os_type": "...",
 "frame_type": "...", "summary": "<1 frase em português a confirmar o que foi entendido>"}"""


def _client():
    from anthropic import AnthropicFoundry
    return AnthropicFoundry(
        api_key  = os.getenv("FOUNDRY_API_KEY",  ""),
        base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
    )


def parse_target_instruction(instruction: str, integration_name: str | None = None,
                              integration_type: str | None = None) -> dict:
    """Converte a instrução em texto livre num descritor estruturado do alvo.

    Devolve {"target_kind", "target_ref", "category", "os_type", "frame_type",
    "summary", "parse_warning": <str|None>}.
    """
    context = f"Integração escolhida: {integration_name or '(nenhuma)'}"
    if integration_type:
        context += f" (tipo: {integration_type})"
    context += f"\n\nInstrução: {instruction}"

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _TARGET_SYSTEM,
        messages   = [{"role": "user", "content": context}],
        max_tokens = 400,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    parse_warning = None
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    if not isinstance(data, dict):
        return {
            "target_kind":   "agentless_host",
            "target_ref":    instruction.strip()[:200],
            "category":      None,
            "os_type":       "windows",
            "frame_type":    "metrics",
            "summary":       "Não foi possível interpretar a instrução automaticamente.",
            "parse_warning": "Não foi possível interpretar a instrução; confirma/edita os campos antes de criar o alvo.",
        }

    target_kind = str(data.get("target_kind") or "")
    if target_kind not in _VALID_TARGET_KINDS:
        parse_warning = f'target_kind "{target_kind}" inválido; ajustado para "agentless_host".'
        target_kind = "agentless_host"

    target_ref = str(data.get("target_ref") or "").strip()
    if not target_ref:
        parse_warning = (parse_warning + " " if parse_warning else "") + "Referência não identificada — confirma/edita antes de criar."
        target_ref = instruction.strip()[:200]

    category = data.get("category")
    if category not in _VALID_CATEGORIES:
        category = None

    os_type = str(data.get("os_type") or "windows")
    if os_type not in _VALID_OS_TYPES:
        os_type = "windows"

    frame_type = str(data.get("frame_type") or "metrics")
    if frame_type not in _VALID_FRAME_TYPES:
        frame_type = "metrics"

    return {
        "target_kind":   target_kind,
        "target_ref":    target_ref,
        "category":      category,
        "os_type":       os_type,
        "frame_type":    frame_type,
        "summary":       str(data.get("summary") or "")[:300],
        "parse_warning": parse_warning,
    }
