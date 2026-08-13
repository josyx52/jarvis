"""
Historical Engine — análise retrospectiva de um intervalo de tempo já
existente no Zabbix/Splunk (scope_targets.analysis_mode == "historical").

Diferente do explorer_engine.py (modo "live"): não passa pelo BaselineEngine/
Predictor, porque esses motores são desenhados para processamento contínuo
(o BaselineEngine agrupa amostras por hora/dia-da-semana usando o relógio
real no momento em que processa o frame — reproduzir dados antigos por esse
caminho atribuiria erradamente todas as amostras ao "agora" do backfill,
corrompendo o baseline sazonal). Em vez disso, busca a série histórica
completa de uma só vez (Zabbix `history.get` / Splunk com intervalo de tempo
real) e pede directamente à IA para raciocinar sobre essa série temporal.
"""

import json
import time
from datetime import datetime, timezone

from clotho.clotho_store import ClothoStore
from scope_collector.collector import _call_tool
from scope_collector.scope_store import ScopeStore
from explorer.explorer_engine import _client, _strip_fences, _text_of, _VALID_PRIORITIES

_MAX_POINTS_PER_FIELD = 300  # amostragem simples se a série tiver mais pontos que isto


_HISTORICAL_SYSTEM = """Você é o Explorer, o motor exploratório do Jarvis Fates Engine, em modo de
análise retrospectiva (histórica) — o utilizador pediu para analisares um intervalo de
tempo que já aconteceu, não uma situação em curso.

Dada uma série temporal real de um alvo monitorizado, identifica padrões, tendências,
picos, quedas ou comportamentos recorrentes (ex: por hora do dia ou dia da semana, se os
timestamps permitirem observar isso) que sejam relevantes para melhoria futura.

REGRAS OBRIGATÓRIAS:
- Só reporta achados com evidência real nos dados fornecidos — nunca inventes números,
  tendências ou padrões que não estejam presentes na série.
- Se não houver nada relevante, devolve uma lista vazia.
- As recomendações e comandos sugeridos têm de ser compatíveis com o sistema operativo do
  alvo indicado.
- Escreve sempre em português, linguagem clara e técnica.
- Enquadra sempre os achados como retrospectivos (ex: "durante o período analisado...",
  não como se estivesse a acontecer agora).

Prioridade de cada achado:
  low      — optimização, sem urgência
  medium   — vale a pena planear
  high     — risco real identificado no período, pode recorrer
  critical — houve impacto sério no período analisado

Responda APENAS com JSON válido, sem markdown:
{"findings": [
  {"title": "<curto>", "summary": "<1-2 frases>",
   "detail": "<explicação técnica + recomendação concreta>",
   "priority": "low|medium|high|critical"}
]}"""


def _to_epoch(value) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return int(value)


def fetch_zabbix_history_series(clotho_store: ClothoStore, target: dict, field_map: dict,
                                 time_from: int, time_till: int) -> dict[str, list[dict]]:
    integration_id = target["integration_id"]

    latest = _call_tool(integration_id, "zabbix_get_latest_metrics", {"hostid": target["target_ref"]})
    items_by_key = {item["key_"]: item for item in (latest.get("result") or [])}

    series: dict[str, list[dict]] = {}
    for canonical_name, item_key in field_map.items():
        item = items_by_key.get(item_key)
        if not item:
            continue

        history = _call_tool(integration_id, "zabbix_get_history", {
            "itemid": item["itemid"],
            "history_type": int(item.get("value_type", 0)),
            "time_from": time_from,
            "time_till": time_till,
            "limit": _MAX_POINTS_PER_FIELD,
        })

        points = [
            {"time": datetime.fromtimestamp(int(p["clock"]), tz=timezone.utc).isoformat(), "value": p["value"]}
            for p in (history.get("result") or [])
        ]
        if points:
            series[canonical_name] = points

    return series


def fetch_splunk_history_series(clotho_store: ClothoStore, target: dict,
                                 time_from: int, time_till: int) -> list[dict]:
    body = _call_tool(target["integration_id"], "splunk_run_search", {
        "search": target["target_ref"],
        "earliest_time": str(time_from),
        "latest_time": str(time_till),
        "max_count": _MAX_POINTS_PER_FIELD,
    })
    return body.get("results") or []


def analyze_historical_series(category: str, target_name: str, os_type: str,
                               period_start: str, period_end: str, series_json: str) -> list[dict]:
    details = [
        f"Categoria: {category}",
        f"Alvo: {target_name}",
        f"Sistema operativo do alvo: {os_type}",
        f"Período analisado: {period_start} até {period_end}",
        f"Série temporal recolhida: {series_json[:6000]}",
    ]

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _HISTORICAL_SYSTEM,
        messages   = [{"role": "user", "content": "\n".join(details)}],
        max_tokens = 1500,
    )

    text = _strip_fences(_text_of(resp))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}

    findings = []
    for f in (data.get("findings") or []):
        if not isinstance(f, dict) or not f.get("title"):
            continue
        priority = str(f.get("priority") or "medium").lower()
        if priority not in _VALID_PRIORITIES:
            priority = "medium"
        findings.append({
            "title":    str(f.get("title"))[:200],
            "summary":  str(f.get("summary") or "")[:1000],
            "detail":   str(f.get("detail") or "")[:4000],
            "priority": priority,
        })
    return findings


def run_historical_backfill(target: dict, clotho_store: ClothoStore, store: ScopeStore) -> list[dict]:
    if not target.get("history_start") or not target.get("history_end"):
        raise ValueError("alvo sem history_start/history_end definidos")
    if not target.get("integration_id"):
        raise ValueError("análise histórica requer integration_id (ligação Clotho)")

    time_from = _to_epoch(target["history_start"])
    time_till = _to_epoch(target["history_end"])
    category = target.get("category") or "server"

    if target["target_kind"] in ("zabbix_host", "zabbix_item"):
        integration = clotho_store.get_integration(target["integration_id"])
        field_map = (integration.get("config") or {}).get("field_map") or {
            "cpu_percent": "system.cpu.util",
            "memory_percent": "vm.memory.util",
            "disk_percent": "vfs.fs.size[/,pused]",
        }
        series = fetch_zabbix_history_series(clotho_store, target, field_map, time_from, time_till)
    elif target["target_kind"] in ("splunk_search", "splunk_index"):
        series = fetch_splunk_history_series(clotho_store, target, time_from, time_till)
    else:
        raise ValueError(f"target_kind '{target['target_kind']}' não suportado em modo histórico")

    if not series:
        raise ValueError("nenhum dado histórico encontrado para o intervalo pedido")

    series_json = json.dumps(series, ensure_ascii=False, default=str)
    period_start = datetime.fromtimestamp(time_from, tz=timezone.utc).isoformat()
    period_end = datetime.fromtimestamp(time_till, tz=timezone.utc).isoformat()

    findings = analyze_historical_series(
        category, target["name"], target.get("os_type") or "windows",
        period_start, period_end, series_json,
    )

    tips = []
    for finding in findings:
        tip = store.create_tip({
            "scope_target_id": target["id"],
            "category":        category,
            "title":           finding["title"],
            "summary":         finding["summary"],
            "detail":          finding["detail"],
            "priority":        finding["priority"],
            "evidence":        {"mode": "historical", "period_start": period_start,
                                 "period_end": period_end, "series": series},
        })
        tips.append(tip)
    return tips
