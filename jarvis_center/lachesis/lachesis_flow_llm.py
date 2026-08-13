"""
Lachesis — compilação de instruções em linguagem natural (Automações) num
flow_definition estruturado, executável deterministicamente.

Segue o padrão de lachesis_tasks_llm.py (AnthropicFoundry, prompt JSON-only),
mas em vez de um descritor de agendamento produz um grafo de nós/arestas.
"""

import json
import os
import re

from clotho.clotho_mentions import describe_integrations_for_prompt, mentions_context_block

_FLOW_SYSTEM = """Você é o compilador de flows do Jarvis Fates Engine (Automações,
dentro do Lachesis).

O utilizador escreve uma instrução em português. Pode referenciar
integrações/tools/bases de dados do Clotho com $nome$ ou $nome.tool$ ou
$db.nome$ — mas MUITAS VEZES NÃO USA essa sintaxe, e em vez disso nomeia a
integração e a tool em prosa (ex: "usa call_integration_tool com
integration_name=\\"zabbix\\"..." ou simplesmente "consulta o Zabbix",
"envia para o Teams"). Foi-lhe fornecido o catálogo completo das integrações
configuradas no Clotho e das suas tools (nome, descrição, params_schema), e
também um bloco extra resolvendo especificamente as referências $nome$ usadas
na instrução, quando existirem. RECONHEÇA nomes de integrações/tools do
catálogo mesmo quando mencionados apenas em prosa (comparação
case-insensitive) — NÃO exija a sintaxe $nome$ para gerar um nó real.
Reconheça também "query_ad"/"consultar o AD"/"Active Directory" como a
primitiva nativa "query_ad" (não é uma integração Clotho, tal como
agentless_run/agentless_run_bulk também não são). Só recorra ao nó
"ai_decision" para a parte que é genuinamente julgamento/texto livre, nunca
para substituir uma chamada a uma tool ou script que a instrução descreve
claramente.

Converta a instrução num flow_definition JSON com esta forma:

{
  "nodes": [
    {"id": "n1", "type": "trigger", "params": {}},
    {"id": "n2", "type": "tool_call", "tool_ref": "IntegName.tool_name", "params": {...}},
    {"id": "n3", "type": "condition_filter", "params": {"input": "{{steps.n2.result.result}}", "field": "name", "op": "icontains", "value": "disk"}},
    {"id": "n4", "type": "agentless_bulk_script", "params": {"hosts_from": "{{steps.n3.result}}", "host_field": "host", "script": "...", "machine_type": "server"}},
    {"id": "n5", "type": "condition_filter", "params": {"input": "{{steps.n4.result.results}}", "field": "UsedPct", "op": ">", "value": 95}},
    {"id": "n6", "type": "tool_call", "tool_ref": "IntegName.tool_name", "each": "{{steps.n5.result}}", "params": {"message": "Host {{item.key}}: {{item.UsedPct}}%"}},
    {"id": "n7", "type": "database_query", "tool_ref": "db.slug", "params": {"sql": "SELECT ..."}},
    {"id": "n8", "type": "agentless_script", "params": {"host": "...", "script": "..."}},
    {"id": "n9", "type": "ai_decision", "params": {"prompt": "..."}}
  ],
  "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}, ...]
}

Tipos de nó disponíveis:
- "trigger": sempre o primeiro, sem params, sem arestas a entrar.
- "tool_call": chama uma tool de uma integração Clotho.
  tool_ref = "IntegName.tool_name" (nomes exactos do catálogo fornecido).
  Para chamar a MESMA tool uma vez por cada item de uma lista obtida num
  passo anterior (ex: enviar uma mensagem por cada host que sobrou de um
  filtro), acrescenta "each": "{{steps.<id>.result}}" ao nó (não dentro de
  params) — dentro de params podes então usar "{{item}}" ou "{{item.campo}}"
  para os dados de cada item da iteração.
- "database_query": tool_ref = "db.nome", params.sql com o SELECT apropriado.
- "agentless_script": corre um script PowerShell num ÚNICO host fixo,
  conhecido em tempo de compilação. params.host, params.script,
  params.machine_type ("workstation" ou "server").
- "agentless_bulk_script": corre o MESMO script PowerShell em paralelo numa
  LISTA de hosts — usa isto sempre que a lista de hosts só se sabe em tempo
  de execução (ex: veio de um alerta Zabbix, de uma query, etc.). params:
  "hosts" (lista fixa) OU "hosts_from" (referência "{{steps.<id>.result...}}"
  a uma lista de um passo anterior) + "host_field" (nome do campo com o
  hostname dentro de cada item, quando os itens são objectos e não strings);
  "script", "machine_type", "timeout_s". Resultado: {"results": {host: <stdout
  parseado como JSON quando possível>}, "unreachable": [...]}.
- "query_ad": consulta directa ao Active Directory via LDAP/ADSI — primitiva
  nativa (não é integração Clotho), sem host remoto, sem aprovação. Usa para
  resolver atributos de utilizador/máquina (OU, operatingSystem, manager,
  etc.) ou para construir a lista de máquinas-alvo de um varrimento
  fleet-wide. params.filter = filtro LDAP (ex:
  "(&(objectClass=computer)(name=WKS*))"); params.attributes = lista opcional
  de atributos a devolver (inclui sempre o atributo que identifica a máquina,
  normalmente "name"); params.limit = opcional, 0 ou omitido = sem limite.
  Resultado sempre {"results": [...], "count": N} — para encadear para
  agentless_bulk_script, usa hosts_from: "{{steps.<id>.result.results}}" com
  host_field igual ao atributo do nome da máquina (normalmente "name").
- "condition_filter": filtro DETERMINISTA (sem gastar tokens de IA) sobre o
  resultado (lista ou dict) de um passo anterior. params.input = referência
  "{{steps.<id>.result...}}"; params.field = caminho dentro de cada item
  (ex: "UsedPct", ou vazio/omitido para comparar o item inteiro);
  params.op = um de "> >= < <= == != contains icontains"; params.value = valor
  a comparar. Usa para: (a) filtrar por palavra-chave em texto (op
  "icontains"), (b) filtrar por limiar numérico (op ">" etc.). Encadeia
  VÁRIOS condition_filter em sequência para condições "E" (ex: descrição
  contém "disk" E o trigger contém "C:" → dois nós condition_filter
  seguidos). A saída é sempre uma lista; se a entrada era um dict (ex: os
  "results" de um agentless_bulk_script, chaveados por host), cada item
  passa a ter a chave original em "key" — útil para encadear directamente
  para outro agentless_bulk_script.hosts_from (host_field="key") ou para
  tool_call.each ("{{item.key}}").
- "render_template": formatação DETERMINISTA de texto/relatório (sem gastar
  tokens de IA) — usa isto, NUNCA "ai_decision", sempre que a instrução pede
  para compor uma mensagem/relatório com layout fixo a partir de dados já
  calculados nos passos anteriores (ex: "envia um email com uma linha por
  host", "gera um resumo dos resultados"). Dois modos: (a) sem
  params.items_from — renderiza params.template uma vez (mensagem simples,
  ex: um único valor/contagem já calculado); (b) com params.items_from
  (referência "{{steps.<id>.result...}}" a uma lista ou dict de um passo
  anterior, mesma normalização do condition_filter, incluindo params.key_field
  para dicts chaveados por host) — renderiza params.row_template uma vez por
  item (usa "{{item.<campo>}}" dentro do template), junta com
  params.row_separator (omitido = "\n"), envolve em params.header/
  params.footer opcionais; lista vazia usa params.empty_text (omitido = "").
  Para simular um "se X então A senão B" (ex: mensagens diferentes consoante
  uma condição), usa DOIS nós render_template terminais, cada um alimentado
  por um condition_filter com a condição oposta (uma filtra o caso
  verdadeiro, a outra o falso) — o ramo que não se aplica resolve
  items_from vazio e devolve "" (sem header/footer/empty_text), que é
  automaticamente excluído do resultado final; o outro ramo produz o texto.
  NUNCA uses ai_decision para isto.
- "ai_decision": julgamento/texto livre por IA, com acesso real a tools
  (call_integration_tool, agentless_run/_bulk, query_database) — reserva
  ESTRITAMENTE para partes GENUINAMENTE ambíguas: classificar algo, detectar
  uma anomalia/divergência entre itens (ex: comparar hashes e sinalizar o que
  foge à maioria), ou redigir texto que exige análise de facto — nunca para
  substituir passos que já descrevem exactamente que tool/script correr e com
  que critério, e nunca para compor uma mensagem/relatório de layout fixo a
  partir de dados já estruturados — isso é sempre "render_template".

Regras gerais:
- Exactamente um nó "trigger", sempre o primeiro.
- Para referenciar dados do PRÓPRIO trigger (ex: campos do corpo recebido por
  um webhook — nome da máquina, evento, severidade, etc.), usa SEMPRE
  "{{trigger}}" ou "{{trigger.<campo>}}" — NUNCA "{{steps.n1.result...}}" nem
  qualquer outra referência pelo id do nó trigger. "{{trigger.<campo>}}" é um
  alias estável que resolve para o nó trigger independentemente do id que lhe
  deres — evita instruções e flows editados à mão partirem-se por causa de um
  id que mudou.
- Um nó pode referenciar o resultado de um nó anterior QUE NÃO SEJA O TRIGGER
  com "{{steps.<id>.result}}", ou perfurar directamente num campo aninhado com
  "{{steps.<id>.result.<caminho.aninhado>}}" (ex: "{{steps.n2.result.result}}"
  para o campo "result" de uma resposta JSON-RPC).
- Para incluir a data/hora actual numa mensagem, usa exactamente "{{now}}"
  (não inventes outras variáveis de tempo como "{{now_utc1}}" ou
  "{{timestamp}}" — só "{{now}}" é resolvido pelo motor).
- Prefere DECOMPOR a instrução em nós reais (tool_call, condition_filter,
  agentless_bulk_script, agentless_script, database_query, query_ad,
  render_template) sempre que ela descreve passos concretos e determináveis
  — só cai num nó "ai_decision" quando a parte em causa É de facto
  julgamento/análise genuína, sem nenhuma tool/script/critério/template
  identificável.
- Ligue os nós em sequência lógica com "edges", do trigger até ao(s) nó(s)
  final(is).
- NUNCA hardcodifiques um valor literal (letra de disco, hostname, ID) dentro de
  um script/ciclo que deveria usar a variável de iteração correspondente (ex:
  num "foreach ($disk in ...)", usa "$($disk.DeviceID)\\Windows\\Temp", nunca
  "C:\\Windows\\Temp" fixo). Caminhos específicos do sistema operativo (pasta
  Windows, WinSxS, SoftwareDistribution, System32\\LogFiles, etc.) só existem na
  drive de sistema — usa $env:SystemDrive e verifica-os UMA VEZ, nunca os
  repitas dentro de um ciclo sobre vários discos/hosts.

Responda com um objecto JSON:
{"flow_definition": {...}, "summary": "resumo humano curto do que o flow faz", "parse_warning": null|"aviso"}

Responda APENAS com esse objecto JSON, sem markdown."""


def _client():
    from anthropic import AnthropicFoundry
    return AnthropicFoundry(
        api_key  = os.getenv("FOUNDRY_API_KEY",  ""),
        base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
    )


def _fallback_flow(instruction: str) -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "params": {}},
            {"id": "n2", "type": "ai_decision", "params": {"prompt": instruction}},
        ],
        "edges": [{"from": "n1", "to": "n2"}],
    }


_FLOW_REVIEW_SYSTEM = """Você é revisor de flows compilados do Jarvis Fates Engine (Lachesis).

Recebeu um flow_definition JSON já compilado a partir de uma instrução. A sua tarefa é
rever o grafo (nodes/edges) e os scripts/prompts embutidos por erros comuns de geração —
SEM executar nada, é uma revisão estática, como um code review humano feito só a ler o
código.

Procura especificamente por:
1. Valores hardcoded (letra de disco, hostname, ID) usados dentro de um ciclo/loop de um
   script PowerShell que deveria usar a variável de iteração em vez de um literal fixo
   (ex: "C:\\Windows\\Temp" dentro de "foreach ($disk in ...)" quando devia ser
   "$($disk.DeviceID)\\Windows\\Temp" ou equivalente).
2. Caminhos específicos do sistema operativo (pasta Windows, WinSxS, SoftwareDistribution,
   System32\\LogFiles, etc.) verificados por cada disco/host de uma lista quando só
   deviam ser verificados uma vez, na drive de sistema ($env:SystemDrive) — não
   repetidos para todos os discos/hosts.
3. Referências "{{steps.<id>.result...}}" a nós ("id") que não existem no flow.
4. Referências "{{trigger.<campo>}}" ou nomes de campo inconsistentes com os campos
   reais do payload de amostra fornecido, quando aplicável.

Se encontrar problemas, devolve o flow_definition CORRIGIDO (grafo completo, com a
correcção aplicada — não só o troço alterado). Se não encontrar nenhum problema, devolve
o flow_definition original, inalterado.

Responda com um objecto JSON: {"flow_definition": {...}, "issues_found": ["descrição
curta de cada problema corrigido"]} — "issues_found" vazio ([]) se não houver problemas.
Responda APENAS com esse objecto JSON, sem markdown."""


def _review_flow(flow_definition: dict, instruction: str, sample_payload: dict | None = None) -> dict:
    """Segunda passagem, estática (sem executar nada): revê o flow acabado de compilar
    à procura de erros comuns de geração — valores hardcoded que deviam ser variáveis
    dentro de um ciclo, referências a nós inexistentes, nomes de campo inconsistentes
    com o payload de amostra. Análoga a um code review humano, não a um teste real
    (testar a sério implicaria correr scripts agentless/WinRM potencialmente
    destrutivos a cada compilação, o que não é seguro fazer às cegas).

    Devolve {"flow_definition": <corrigido ou original>, "issues_found": [...]}.
    Em caso de falha (API, parsing), devolve o flow_definition original sem alterações.
    """
    user_content = (
        f"Instrução original:\n{instruction}\n\n"
        f"flow_definition compilado a rever:\n{json.dumps(flow_definition, ensure_ascii=False, indent=2)}"
    )
    if sample_payload:
        user_content += (
            "\n\nPayload de amostra do trigger:\n"
            f"{json.dumps(sample_payload, ensure_ascii=False, indent=2)}"
        )

    try:
        resp = _client().messages.create(
            model      = "claude-sonnet-4-6",
            system     = _FLOW_REVIEW_SYSTEM,
            messages   = [{"role": "user", "content": user_content}],
            max_tokens = 3000,
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        parsed = json.loads(text)
        reviewed = parsed.get("flow_definition")
        issues = parsed.get("issues_found") or []
        if isinstance(reviewed, dict) and reviewed.get("nodes"):
            return {"flow_definition": reviewed, "issues_found": issues}
    except Exception:
        pass
    return {"flow_definition": flow_definition, "issues_found": []}


_SUMMARIZE_SYSTEM = """Você é o compilador de flows do Jarvis Fates Engine (Automações,
dentro do Lachesis) — desta vez a trabalhar ao contrário do habitual.

Foi-lhe dado um flow_definition JSON (grafo de nós/arestas já editado
directamente pelo utilizador num canvas visual). Escreva a instrução em
português, em linguagem natural, que descreve exactamente o que este flow
faz, na mesma ordem dos passos (trigger → acções → decisão final), como se
o utilizador a tivesse escrito à mão. Referencie tools/integrações/bases de
dados com $nome$ ou $nome.tool$ ou $db.nome$ sempre que um nó "tool_call"
ou "database_query" tiver um tool_ref. Para um nó "tool_call" com "each",
descreve explicitamente que a acção se repete uma vez por item da lista
obtida no passo referenciado. Para "agentless_bulk_script", descreve que o
script corre em paralelo em todos os hosts da lista dinâmica (indicando de
onde vem essa lista). Para "condition_filter", descreve a condição em
linguagem natural (ex: "só os que tiverem X acima de Y", "só os que
mencionarem Z").

Responda APENAS com o texto da instrução, sem markdown, sem JSON, sem
comentários adicionais."""


def summarize_flow_to_text(flow_definition: dict) -> str:
    """Gera uma instrução em texto livre a partir de um flow_definition editado no canvas.

    É o inverso de compile_instruction_to_flow — usado para o painel de texto
    acompanhar edições feitas directamente no flow visual.
    """
    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _SUMMARIZE_SYSTEM,
        messages   = [{"role": "user", "content": json.dumps(flow_definition, ensure_ascii=False)}],
        max_tokens = 800,
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    return re.sub(r"^```(?:\w+)?|```$", "", text, flags=re.MULTILINE).strip()


def compile_instruction_to_flow(
    instruction: str, task_type: str = "agentless", sample_payload: dict | None = None
) -> dict:
    """Compila uma instrução em texto livre num flow_definition.

    sample_payload: exemplo real do corpo que o trigger (ex: webhook_in) vai
    receber — quando fornecido, os nomes de campo reais são incluídos no
    prompt para o compilador gerar referências {{trigger.<campo>}} correctas
    em vez de adivinhar nomes de campo.

    Devolve {"flow_definition": {...}, "summary": "...", "parse_warning": <str|None>}.
    """
    catalog_block = describe_integrations_for_prompt()
    mentions_block = mentions_context_block(instruction)

    user_content = f"Tipo de tarefa: {task_type}\n\nInstrução:\n{instruction}"
    if sample_payload:
        user_content += (
            "\n\nPayload de amostra do trigger (é exactamente isto que vai chegar via "
            "{{trigger}}/{{trigger.<campo>}} quando este webhook for chamado a sério — "
            "usa estes nomes de campo reais, não invente outros):\n"
            f"{json.dumps(sample_payload, ensure_ascii=False, indent=2)}"
        )
    if catalog_block:
        user_content += f"\n{catalog_block}"
    if mentions_block:
        user_content += f"\n{mentions_block}"

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _FLOW_SYSTEM,
        messages   = [{"role": "user", "content": user_content}],
        max_tokens = 3000,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    parse_warning = None
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parse_warning = "Não foi possível compilar o flow; foi gerado um flow mínimo (instrução única)."

    flow_definition = None
    summary = None
    if isinstance(parsed, dict):
        flow_definition = parsed.get("flow_definition")
        summary = parsed.get("summary")
        parse_warning = parse_warning or parsed.get("parse_warning")

    if not isinstance(flow_definition, dict) or not flow_definition.get("nodes"):
        parse_warning = parse_warning or "Flow compilado era inválido; foi aplicado um flow mínimo (instrução única)."
        flow_definition = _fallback_flow(instruction)
        summary = summary or instruction[:200]
    else:
        review = _review_flow(flow_definition, instruction, sample_payload)
        flow_definition = review["flow_definition"]
        if review["issues_found"]:
            fixes = "; ".join(review["issues_found"])
            parse_warning = f"{parse_warning} " if parse_warning else ""
            parse_warning += f"Revisão automática corrigiu: {fixes}"

    return {
        "flow_definition": flow_definition,
        "summary": summary or instruction[:200],
        "parse_warning": parse_warning,
    }
