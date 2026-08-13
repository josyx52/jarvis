"""
Lachesis — execução determinística de um flow_definition compilado (Automações).

Ao contrário do caminho legado (execute_task -> run_jarvis_loop, que reinterpreta
a instrução em texto livre pelo LLM a cada corrida), aqui cada nó do flow é
chamado directamente: tools do Clotho, queries a bases de dados geridas pelo
Clotho, scripts agentless via WinRM (num único host ou em lote/dinâmico),
filtros/condições deterministas sem custo de LLM, ou um passo explícito de
"decisão por IA" (generalização do padrão já usado pelo SolutionEngine) para
os casos que realmente precisam de julgamento do LLM.

Os resultados de cada nó (dict `results`) são mantidos estruturados (dict/list/str,
não pré-serializados) para que nós a jusante — sobretudo agentless_bulk_script,
condition_filter e a iteração de tool_call — consigam consumir dados reais em
vez de texto solto.
"""

import json
import re

# {{steps.<id>.result}} ou {{steps.<id>.result.<caminho.aninhado>}} — o sufixo
# opcional permite "perfurar" directamente para dentro de um resultado
# estruturado (ex: {{steps.n2.result.result}} para o campo "result" da resposta
# JSON-RPC do Zabbix).
_STEP_REF = re.compile(r"\{\{\s*steps\.([\w\-]+)\.result(?:\.([\w.\-]+))?\s*\}\}")
# {{trigger}} ou {{trigger.<campo>}} — alias estável para o nó do tipo "trigger",
# independente do id que o compilador (ou o utilizador) lhe deu. Evita ter de
# adivinhar/conhecer o id real do nó (ex: "n1", "trigger1") ao escrever a
# instrução ou editar o flow à mão.
_TRIGGER_REF = re.compile(r"\{\{\s*trigger(?:\.([\w.\-]+))?\s*\}\}")
# {{item}} ou {{item.<caminho>}} — só resolve dentro de uma iteração (tool_call.each).
_ITEM_REF = re.compile(r"\{\{\s*item(?:\.([\w.\-]+))?\s*\}\}")
# {{now}} — data/hora actual do servidor (WAT, UTC+1 — mesma convenção do resto do Jarvis).
_NOW_REF = re.compile(r"\{\{\s*now\s*\}\}")


def _now_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " UTC+1"


class FlowError(Exception):
    def __init__(self, node_id: str, message: str):
        super().__init__(f"[{node_id}] {message}")
        self.node_id = node_id


def _get_path(obj, path: str | None):
    """Navega um caminho 'a.b.0.c' dentro de dicts/lists aninhados."""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return None
    return cur


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _display(value) -> str:
    """Versão legível de um resultado estruturado, para o result_text final."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        return str(value)


def _substitute(value, results: dict, item=None, trigger_id: str | None = None):
    """Substitui {{steps.*}}, {{trigger.*}} e {{item.*}} em `value`.

    Quando a *string inteira* é uma única referência (ex: params.hosts_from =
    "{{steps.n2.result}}"), devolve o objecto estruturado tal-e-qual (dict/list),
    em vez de o serializar — é isto que permite aos nós a jusante iterar/filtrar
    dados reais. Só quando a referência está embutida noutro texto (ex: um
    prompt ou uma mensagem) é que o valor é serializado para string.

    {{trigger.<campo>}} é sempre equivalente a {{steps.<id-real-do-trigger>.result.<campo>}}
    — resolve-se via `trigger_id` (calculado uma vez em run_flow), não pelo id
    literal que apareça na instrução/flow.
    """
    if isinstance(value, str):
        stripped = value.strip()

        full_step = _STEP_REF.fullmatch(stripped)
        if full_step:
            step_id, path = full_step.group(1), full_step.group(2)
            return _get_path(results.get(step_id), path)

        full_trigger = _TRIGGER_REF.fullmatch(stripped)
        if full_trigger and trigger_id is not None:
            return _get_path(results.get(trigger_id), full_trigger.group(1))

        full_item = _ITEM_REF.fullmatch(stripped)
        if full_item and item is not None:
            return _get_path(item, full_item.group(1))

        if _NOW_REF.fullmatch(stripped):
            return _now_str()

        def repl_step(m):
            return _stringify(_get_path(results.get(m.group(1)), m.group(2)))

        def repl_trigger(m):
            if trigger_id is None:
                return m.group(0)
            return _stringify(_get_path(results.get(trigger_id), m.group(1)))

        def repl_item(m):
            return _stringify(_get_path(item, m.group(1))) if item is not None else m.group(0)

        value = _STEP_REF.sub(repl_step, value)
        value = _TRIGGER_REF.sub(repl_trigger, value)
        value = _ITEM_REF.sub(repl_item, value)
        value = _NOW_REF.sub(lambda m: _now_str(), value)
        return value
    if isinstance(value, dict):
        return {k: _substitute(v, results, item, trigger_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, results, item, trigger_id) for v in value]
    return value


def _find_integration_by_name(name: str):
    from clotho.clotho_store import ClothoStore
    store = ClothoStore()
    for integ in store.list_integrations():
        if integ["name"].lower() == name.lower():
            return store.get_integration(integ["id"])
    return None


def _run_tool_call(node: dict, results: dict, trigger_id: str | None = None):
    tool_ref = node.get("tool_ref", "")
    if "." not in tool_ref:
        raise FlowError(node["id"], f"tool_ref inválido (esperado 'Integracao.tool'): {tool_ref!r}")
    integ_name, tool_name = tool_ref.split(".", 1)

    integration = _find_integration_by_name(integ_name)
    if not integration:
        raise FlowError(node["id"], f"integração '{integ_name}' não encontrada no Clotho")

    tools = ((integration.get("tools") or {}).get("tools") or {})
    tool_def = tools.get(tool_name)
    if not tool_def:
        raise FlowError(node["id"], f"tool '{tool_name}' não encontrada na integração '{integ_name}'")

    from clotho.clotho_tester import build_tool_request, execute_test_request

    def _call(params):
        request_spec = build_tool_request(tool_def["request"], params)
        response = execute_test_request(integration, request_spec)
        if not response.get("ok"):
            raise FlowError(
                node["id"],
                response.get("error") or f"HTTP {response.get('status_code')} em {tool_ref}",
            )
        raw = response.get("body_snippet") or ""
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    raw_params = node.get("params") or {}
    each_ref = node.get("each")
    if each_ref:
        items = _substitute(each_ref, results, trigger_id=trigger_id)
        if not isinstance(items, list):
            raise FlowError(node["id"], f"'each' não resolveu para uma lista: {each_ref!r}")
        return [_call(_substitute(raw_params, results, item, trigger_id)) for item in items]

    return _call(_substitute(raw_params, results, trigger_id=trigger_id))


def _run_database_query(node: dict, results: dict, trigger_id: str | None = None):
    tool_ref = node.get("tool_ref", "")
    slug = tool_ref.split(".", 1)[1] if "." in tool_ref else tool_ref

    from clotho.clotho_database_store import ClothoDatabaseStore
    from clotho.clotho_database_engine import run_query

    db = ClothoDatabaseStore().get_database_by_name(slug)
    if not db:
        raise FlowError(node["id"], f"base de dados '{slug}' não encontrada no Clotho")

    params = _substitute(node.get("params") or {}, results, trigger_id=trigger_id)
    sql = params.get("sql")
    if not sql:
        raise FlowError(node["id"], "nó database_query sem params.sql")

    result = run_query(db, sql)
    if result.get("error"):
        raise FlowError(node["id"], result["error"])

    # Normaliza {"columns": [...], "rows": [[...], ...]} para uma lista de
    # dicts (uma entrada por linha) — é isto que permite encadear directamente
    # para condition_filter/agentless_bulk_script/tool_call.each por nome de
    # coluna, em vez de índices posicionais.
    columns = result.get("columns") or []
    return [dict(zip(columns, row)) for row in (result.get("rows") or [])]


def _run_agentless_script(node: dict, results: dict, trigger_id: str | None = None):
    params = _substitute(node.get("params") or {}, results, trigger_id=trigger_id)
    host = params.get("host")
    script = params.get("script")
    if not host or not script:
        raise FlowError(node["id"], "nó agentless_script requer params.host e params.script")

    from agentless.remote_executor import RemoteExecutor, RemoteExecutorError

    try:
        executor = RemoteExecutor(host=host, machine_type=params.get("machine_type", "workstation"))
        outcome = executor.run(script, timeout=int(params.get("timeout", 60)))
    except RemoteExecutorError as e:
        # Falha de COMUNICAÇÃO com a máquina (WinRM inacessível, credenciais em falta,
        # etc.) — devolvida como resultado estruturado em vez de abortar o flow, para
        # que passos a jusante (condition_filter/ai_decision) possam decidir notificar
        # sobre a máquina estar incontactável. "needsAttention" alinha com o mesmo campo
        # usado por automações que fazem gating por essa chave (ver automação de disco).
        return {"unreachable": True, "needsAttention": True, "host": host, "error": str(e)}
    if not outcome.get("ok"):
        raise FlowError(node["id"], outcome.get("stderr") or "script falhou sem stderr")
    raw = str(outcome.get("stdout", ""))
    # Tal como _run_tool_call/_run_agentless_bulk_script: se o script devolveu JSON
    # (ex: ConvertTo-Json), mantém estruturado para que condition_filter e outros nós
    # a jusante consigam filtrar/aceder a campos reais, não só texto solto.
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _run_query_ad(node: dict, results: dict, trigger_id: str | None = None):
    """Consulta directa ao Active Directory via LDAP/ADSI — primitiva nativa,
    não passa pelo Clotho. Espelha _run_agentless_script: extrai params,
    chama a função já existente, faz parse do JSON devolvido. compact=False
    garante sempre {"results": [...], "count": N} mesmo em fleets grandes,
    para agentless_bulk_script.hosts_from poder iterar a lista real."""
    params = _substitute(node.get("params") or {}, results, trigger_id=trigger_id)
    filter_ = params.get("filter")
    if not filter_:
        raise FlowError(node["id"], "nó query_ad requer params.filter")

    from api.chat_engine import _query_ad

    raw, _label = _query_ad(filter_, params.get("attributes"), params.get("limit", 0), compact=False)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _run_agentless_bulk_script(node: dict, results: dict, trigger_id: str | None = None) -> dict:
    """Corre o mesmo script PowerShell em paralelo numa lista de hosts — a lista
    pode ser fixa (params.hosts) ou vir dinamicamente do resultado de um passo
    anterior (params.hosts_from, com params.host_field quando os itens são
    objectos e não strings simples). Devolve {"results": {host: <stdout parseado
    como JSON, ou dict cru>}, "unreachable": [...]}."""
    raw_params = node.get("params") or {}
    script = _substitute(raw_params.get("script"), results, trigger_id=trigger_id)
    if not script:
        raise FlowError(node["id"], "nó agentless_bulk_script requer params.script")

    if raw_params.get("hosts") is not None:
        hosts = _substitute(raw_params.get("hosts"), results, trigger_id=trigger_id)
    else:
        hosts_from = raw_params.get("hosts_from")
        if not hosts_from:
            raise FlowError(node["id"], "nó agentless_bulk_script requer params.hosts ou params.hosts_from")
        source = _substitute(hosts_from, results, trigger_id=trigger_id)
        if not isinstance(source, list):
            raise FlowError(node["id"], f"'hosts_from' não resolveu para uma lista: {hosts_from!r}")
        host_field = raw_params.get("host_field")
        seen = set()
        hosts = []
        for entry in source:
            name = entry.get(host_field) if (host_field and isinstance(entry, dict)) else entry
            if isinstance(name, str) and name.strip() and name not in seen:
                seen.add(name)
                hosts.append(name.strip())

    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise FlowError(node["id"], "'hosts'/'hosts_from' não resolveu numa lista de nomes de host")

    if not hosts:
        return {"results": {}, "unreachable": []}

    from agentless.remote_executor import RemoteExecutor

    machine_type = raw_params.get("machine_type", "server")
    timeout = int(raw_params.get("timeout_s", 120))
    executor = RemoteExecutor(host=hosts[0], machine_type=machine_type)
    outcome = executor.run_bulk(hosts=hosts, script=script, timeout=timeout)

    parsed = {}
    for host, host_result in (outcome.get("results") or {}).items():
        stdout = (host_result or {}).get("stdout") or ""
        try:
            parsed[host] = json.loads(stdout.strip())
        except (json.JSONDecodeError, ValueError, AttributeError):
            parsed[host] = host_result
    return {"results": parsed, "unreachable": outcome.get("unreachable") or []}


_OPS = {
    ">":         lambda a, b: a is not None and a > b,
    ">=":        lambda a, b: a is not None and a >= b,
    "<":         lambda a, b: a is not None and a < b,
    "<=":        lambda a, b: a is not None and a <= b,
    "==":        lambda a, b: a == b,
    "!=":        lambda a, b: a != b,
    "contains":  lambda a, b: isinstance(a, str) and isinstance(b, str) and b in a,
    "icontains": lambda a, b: isinstance(a, str) and isinstance(b, str) and b.lower() in a.lower(),
}


def _normalize_items(node_id: str, source, key_field: str = "key") -> list:
    """Normaliza um resultado (lista ou dict chaveado, ex: agentless_bulk_script.results)
    para uma lista de itens — se a entrada era um dict, a chave fica disponível em
    cada item sob key_field (default "key"). Partilhada por condition_filter e
    render_template, que ambos precisam de iterar sobre o resultado de um passo
    anterior independentemente da forma em que ele veio."""
    if isinstance(source, dict):
        items = []
        for k, v in source.items():
            item = dict(v) if isinstance(v, dict) else {"value": v}
            item.setdefault(key_field, k)
            items.append(item)
        return items
    if isinstance(source, list):
        return source
    raise FlowError(node_id, "valor não resolveu numa lista/dict normalizável em itens")


def _run_condition_filter(node: dict, results: dict, trigger_id: str | None = None) -> list:
    """Filtro determinista (sem LLM) sobre o resultado (lista ou dict) de um
    passo anterior. Normaliza sempre a saída para uma lista de itens — se a
    entrada era um dict (ex: agentless_bulk_script.results, chaveado por host),
    a chave fica disponível em cada item sob params.key_field (default "key")
    — o que permite encadear directamente para agentless_bulk_script.hosts_from
    ou para tool_call.each."""
    raw_params = node.get("params") or {}
    source_ref = raw_params.get("input")
    if not source_ref:
        raise FlowError(node["id"], "nó condition_filter requer params.input")
    source = _substitute(source_ref, results, trigger_id=trigger_id)

    field = raw_params.get("field")
    op = raw_params.get("op")
    value = raw_params.get("value")
    if op not in _OPS:
        raise FlowError(node["id"], f"operador inválido: {op!r} (usa um de {sorted(_OPS)})")

    items = _normalize_items(node["id"], source, raw_params.get("key_field", "key"))

    def _matches(item) -> bool:
        actual = _get_path(item, field) if field else item
        try:
            return bool(_OPS[op](actual, value))
        except TypeError:
            return False

    return [item for item in items if _matches(item)]


def _run_render_template(node: dict, results: dict, trigger_id: str | None = None) -> str:
    """Formatação determinista de texto/relatório (sem LLM) — alternativa ao
    ai_decision para compor mensagens a partir de dados já calculados nos passos
    anteriores. Dois modos:
      - sem params.items_from: renderiza params.template uma vez (mensagem simples).
      - com params.items_from: uma linha (params.row_template) por item, junta com
        params.row_separator (default "\\n"), envolvida em params.header/params.footer
        opcionais; lista vazia -> params.empty_text (default "").

    O modo com items_from também serve para simular ramos condicionais: dois nós
    render_template terminais, cada um alimentado por um condition_filter com a
    condição oposta, devolvem "" no ramo que não se aplica (sem header/footer,
    empty_text por omissão) — e um resultado terminal "" é excluído do
    result_text final por run_flow, o que replica um if/else sem precisar de
    sintaxe condicional dentro do template."""
    raw_params = node.get("params") or {}
    items_from = raw_params.get("items_from")

    if not items_from:
        template = raw_params.get("template")
        if not template:
            raise FlowError(node["id"], "nó render_template sem params.items_from requer params.template")
        return _substitute(template, results, trigger_id=trigger_id)

    row_template = raw_params.get("row_template")
    if not row_template:
        raise FlowError(node["id"], "nó render_template com params.items_from requer params.row_template")

    source = _substitute(items_from, results, trigger_id=trigger_id)
    items = _normalize_items(node["id"], source, raw_params.get("key_field", "key"))
    if not items:
        return raw_params.get("empty_text", "")

    separator = raw_params.get("row_separator", "\n")
    rows = separator.join(_substitute(row_template, results, item, trigger_id) for item in items)

    parts = []
    if raw_params.get("header"):
        parts.append(_substitute(raw_params["header"], results, trigger_id=trigger_id))
    parts.append(rows)
    if raw_params.get("footer"):
        parts.append(_substitute(raw_params["footer"], results, trigger_id=trigger_id))
    return "\n".join(parts)


def _run_ai_decision(node: dict, results: dict, trigger_id: str | None = None) -> str:
    """Nó de decisão por IA — corre com acesso real a tools (call_integration_tool,
    agentless_run/_bulk, query_database, etc.), tal como o caminho legado
    (lachesis_scheduler.execute_task sem flow_definition). Sem isto, o nó apenas
    narrava/inventava a execução em vez de a realizar (ver run_jarvis_loop
    channel="automation" — approval tools correm directamente porque a
    autorização já foi dada ao gravar a automação). Reservado para julgamento
    livre; passos deterministas devem usar agentless_bulk_script/condition_filter/
    render_template."""
    params = _substitute(node.get("params") or {}, results, trigger_id=trigger_id)
    prompt = params.get("prompt")
    if not prompt:
        raise FlowError(node["id"], "nó ai_decision requer params.prompt")

    from api.chat_engine import run_jarvis_loop

    return run_jarvis_loop(
        [{"role": "user", "content": prompt}],
        channel="automation",
    ).strip()


_RUNNERS = {
    "tool_call":            _run_tool_call,
    "database_query":       _run_database_query,
    "agentless_script":      _run_agentless_script,
    "agentless_bulk_script": _run_agentless_bulk_script,
    "query_ad":              _run_query_ad,
    "condition_filter":      _run_condition_filter,
    "render_template":       _run_render_template,
    "ai_decision":           _run_ai_decision,
}


def _topological_order(nodes: list[dict], edges: list[dict]) -> list[dict]:
    by_id = {n["id"]: n for n in nodes}
    incoming = {n["id"]: 0 for n in nodes}
    outgoing: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        if e["from"] in outgoing and e["to"] in incoming:
            outgoing[e["from"]].append(e["to"])
            incoming[e["to"]] += 1

    ready = [n_id for n_id, count in incoming.items() if count == 0]
    order = []
    seen = set()
    while ready:
        n_id = ready.pop(0)
        if n_id in seen:
            continue
        seen.add(n_id)
        order.append(by_id[n_id])
        for nxt in outgoing.get(n_id, []):
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)

    # nós inalcançáveis (grafo desconexo) vão no fim, pela ordem original
    for n in nodes:
        if n["id"] not in seen:
            order.append(n)
    return order, outgoing


def run_flow(flow_definition: dict, trigger_payload=None) -> dict:
    """Executa um flow_definition nó a nó. Devolve {"result_text": ..., "error": <str|None>}.

    trigger_payload: corpo do webhook_in já decodificado (dict/list se era JSON válido,
    senão string) — fica disponível ao nó "trigger" como o seu resultado (mantido
    estruturado, não serializado), para outros nós referenciarem campos via
    {{steps.<trigger_id>.result.<campo>}}.
    """
    nodes = flow_definition.get("nodes") or []
    edges = flow_definition.get("edges") or []
    if not nodes:
        return {"result_text": None, "error": "flow_definition sem nós"}

    order, outgoing = _topological_order(nodes, edges)
    results: dict = {}
    terminal_ids = [n["id"] for n in nodes if not outgoing.get(n["id"])]
    trigger_id = next((n["id"] for n in nodes if n.get("type") == "trigger"), None)

    node = None
    try:
        for node in order:
            n_type = node.get("type")
            if n_type == "trigger":
                results[node["id"]] = trigger_payload or ""
                continue
            runner = _RUNNERS.get(n_type)
            if not runner:
                raise FlowError(node["id"], f"tipo de nó desconhecido: {n_type!r}")
            results[node["id"]] = runner(node, results, trigger_id)
    except FlowError as e:
        return {"result_text": None, "error": str(e)}
    except Exception as e:
        return {"result_text": None, "error": f"[{node.get('id', '?') if node else '?'}] {e}"}

    result_text = "\n\n".join(
        _display(results[n_id]) for n_id in terminal_ids if results.get(n_id)
    ).strip()
    return {"result_text": result_text or None, "error": None}
