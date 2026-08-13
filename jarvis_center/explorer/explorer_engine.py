"""
Explorer Engine — motor exploratório proactivo do Jarvis Fates Engine.

Gera "tips de melhoria" categorizados (server/application/database/network/
security/business) sobre os alvos definidos em scope_targets — distinto do
SolutionEngine (reactivo, só actua sobre Problems críticos já abertos há
> 5 min). O Explorer corre em cadência própria e muito mais espaçada
(scope_targets.explore_cadence_seconds, default 1h), e nunca duplica lógica
de detecção: lê apenas o que BaselineEngine/Correlator/Predictor já
detectaram e gravaram (ver explorer/state_reader.py).

Padrão de chamadas LLM (mesmo estilo do lachesis/asclepion_engine.py):
  generate_focus_areas — 1x por categoria (cache em memória do processo),
                         define o que vale a pena observar nessa categoria.
  evaluate_target       — por alvo/ciclo, cruza as áreas de foco com a
                         evidência real e produz os achados finais.
"""

import json
import os
import re


_FOCUS_SYSTEM = """Você é o Explorer, o motor exploratório proactivo do Jarvis Fates Engine.

O teu papel é sugerir MELHORIAS preventivas sobre sistemas monitorizados — nunca reagir
a incidentes já abertos (isso é feito por outro motor, o SolutionEngine).

Dada uma categoria de alvo monitorizado, lista entre 4 e 8 áreas de foco relevantes para
essa categoria — aspectos que vale a pena rever periodicamente para prevenir problemas
futuros ou melhorar eficiência.

Categorias possíveis:
  server      — sistema operativo / servidor (CPU, memória, disco)
  application — serviço aplicacional (latência, taxa de erro)
  database    — base de dados (queries, pool de conexões)
  network     — rede/infra (interfaces, throughput)
  security    — segurança (autenticações, privilégios)
  business    — métricas de negócio (filas, transacções)

Responda APENAS com JSON válido, sem markdown:
{"focus_areas": ["<área 1>", "<área 2>", ...]}"""


_EVALUATE_SYSTEM = """Você é o Explorer, o motor exploratório proactivo do Jarvis Fates Engine.

Dado um alvo monitorizado, as suas áreas de foco, e o histórico recente (snapshot mais
recente, previsões de tendência, correlações e eventos já detectados por outros motores
do Jarvis), identifica oportunidades concretas de melhoria — riscos que ainda não são
incidentes críticos mas que representam risco de falha futura ou ineficiência.

REGRAS OBRIGATÓRIAS:
- Só reporta achados com evidência real nos dados fornecidos — nunca inventes números,
  tendências ou eventos que não estejam presentes na evidência.
- Se não houver nada relevante na evidência fornecida, devolve uma lista vazia — não
  inventes problemas só para preencher a resposta.
- Escreve sempre em português, linguagem clara e técnica.

Prioridade de cada achado:
  low      — optimização, sem urgência
  medium   — vale a pena planear
  high     — risco real a médio prazo
  critical — agir em breve, mas ainda não é um incidente aberto

Além disto, para cada achado avalia se a evidência disponível (Zabbix/Splunk) já é
suficiente para confirmar a causa, ou se fica ambígua/insuficiente e vale a pena uma
investigação mais profunda (ex: ligar directamente à máquina via WinRM para ver
processos, logs, configuração — algo que os dados de telemetria não mostram). Isto
NÃO é uma exigência — é só uma recomendação para o utilizador decidir.

Responda APENAS com JSON válido, sem markdown:
{"findings": [
  {"title": "<curto>", "summary": "<1-2 frases>",
   "detail": "<explicação técnica + recomendação concreta>",
   "priority": "low|medium|high|critical",
   "needs_investigation": <true se a evidência disponível não chega para confirmar a causa>,
   "investigation_reason": "<só se needs_investigation=true — o que falta confirmar e como investigar>"}
]}"""


_VALID_PRIORITIES = {"low", "medium", "high", "critical"}

# Cache em memória do processo — as áreas de foco por categoria não mudam
# entre ciclos, por isso só se chama o LLM uma vez por categoria por arranque.
_focus_cache: dict[str, list[str]] = {}


def _client():
    from anthropic import AnthropicFoundry
    return AnthropicFoundry(
        api_key  = os.getenv("FOUNDRY_API_KEY",  ""),
        base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
    )


def _strip_fences(text: str) -> str:
    return re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()


def _text_of(resp) -> str:
    return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()


def generate_focus_areas(category: str) -> list[str]:
    if category in _focus_cache:
        return _focus_cache[category]

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _FOCUS_SYSTEM,
        messages   = [{"role": "user", "content": f"Categoria: {category}"}],
        max_tokens = 500,
    )

    text = _strip_fences(_text_of(resp))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}

    areas = [str(a) for a in (data.get("focus_areas") or [])][:8]
    _focus_cache[category] = areas
    return areas


def evaluate_target(category: str, target_name: str, focus_areas: list[str], evidence: dict,
                     os_type: str = "windows") -> list[dict]:
    details = [
        f"Categoria: {category}",
        f"Alvo: {target_name}",
        f"Sistema operativo do alvo: {os_type} — as recomendações e comandos sugeridos "
        f"têm de ser compatíveis com este SO (ex: PowerShell/Get-* para windows, "
        f"não vmstat/cgroups/ps aux, que são comandos Linux).",
        f"Áreas de foco: {', '.join(focus_areas) or '(nenhuma definida)'}",
        f"Snapshot mais recente: {json.dumps(evidence.get('snapshot') or {}, default=str)[:1500]}",
        f"Previsões recentes: {json.dumps(evidence.get('predictions') or [], default=str)[:1500]}",
        f"Correlações recentes: {json.dumps(evidence.get('correlations') or [], default=str)[:1500]}",
        f"Eventos recentes: {json.dumps(evidence.get('events') or [], default=str)[:1500]}",
        f"Alertas recentes: {json.dumps(evidence.get('alerts') or [], default=str)[:1000]}",
    ]

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _EVALUATE_SYSTEM,
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
        needs_investigation = bool(f.get("needs_investigation", False))
        investigation_reason = str(f.get("investigation_reason") or "")[:1000] if needs_investigation else None
        findings.append({
            "title":                str(f.get("title"))[:200],
            "summary":              str(f.get("summary") or "")[:1000],
            "detail":               str(f.get("detail") or "")[:4000],
            "priority":             priority,
            "needs_investigation":  needs_investigation,
            "investigation_reason": investigation_reason,
        })
    return findings


def _normalized(title: str) -> str:
    return " ".join(title.lower().split())


def _titles_match(a: str, b: str) -> bool:
    a, b = _normalized(a), _normalized(b)
    return a == b or (len(a) > 8 and a in b) or (len(b) > 8 and b in a)


_REPEAT_THRESHOLD = 3   # esta ocorrência + pelo menos mais 2 anteriores
_REPEAT_WINDOW = 8      # só olha para os últimos N tips deste alvo/categoria
_PROPOSED_TTL = 7 * 86400  # não repropor o mesmo padrão durante 7 dias


def _detect_recurring_pattern(target: dict, category: str, finding: dict, store) -> None:
    """Um achado que se repete em vários ciclos, sem ninguém pedir, é um
    sinal forte por si só — código conta as ocorrências (o LLM de cada ciclo
    não tem memória do ciclo anterior, não há como isto depender dele) e
    propõe automaticamente um facto de âmbito 'target' quando o padrão é
    consistente o suficiente para deixar de ser "tip a resolver" e passar a
    "comportamento conhecido deste alvo"."""
    try:
        recent = store.list_tips(scope_target_id=target["id"], category=category, limit=_REPEAT_WINDOW)
    except Exception:
        return

    matches = [t for t in recent if _titles_match(t.get("title", ""), finding["title"])]
    if len(matches) < _REPEAT_THRESHOLD:
        return

    try:
        from api.chat_engine import _get_redis, _store_pending_action
    except Exception:
        return

    r = _get_redis()
    dedupe_key = f"jarvis:explorer_proposed:{target['id']}:{_normalized(finding['title'])[:80]}"
    if r is not None:
        try:
            if r.get(dedupe_key):
                return  # já propusemos isto recentemente, não repetir a cada ciclo
            r.setex(dedupe_key, _PROPOSED_TTL, "1")
        except Exception:
            pass

    action = {
        "tool":        "propose_infra_knowledge",
        "title":       f"Padrão recorrente — {finding['title']}",
        "instruction": finding["detail"] or finding["summary"],
        "category":    "infra_general",
        "version":     None,
        "evidence":    f"detectado automaticamente — {len(matches)} ocorrências nas últimas {_REPEAT_WINDOW} explorações deste alvo",
        "scope_type":  "target",
        "scope_value": target["name"],
    }
    _store_pending_action(action)


def run_explorer(target: dict, evidence: dict, store) -> list[dict]:
    """
    Corre o ciclo completo para 1 scope_target: gera/reaproveita áreas de
    foco da categoria, avalia a evidência, e persiste os achados como
    exploration_tips via `store` (scope_collector.scope_store.ScopeStore).
    """
    category = target.get("category") or "server"
    os_type = target.get("os_type") or "windows"
    focus_areas = generate_focus_areas(category)
    findings = evaluate_target(category, target["name"], focus_areas, evidence, os_type=os_type)

    tips = []
    for finding in findings:
        tip = store.create_tip({
            "scope_target_id":      target["id"],
            "category":             category,
            "title":                finding["title"],
            "summary":              finding["summary"],
            "detail":               finding["detail"],
            "priority":             finding["priority"],
            "evidence":             evidence,
            "needs_investigation":  finding["needs_investigation"],
            "investigation_reason": finding["investigation_reason"],
        })
        tips.append(tip)
        _detect_recurring_pattern(target, category, finding, store)
    return tips
