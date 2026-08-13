"""
Jarvis Chat Engine — loop de raciocínio multi-turno para a Web UI.

Replica exactamente o comportamento do jarvis-ctl chat:
  1. Injerta o prompt do sistema Jarvis completo
  2. Chama o LLM com as definições de ferramentas
  3. Executa as ferramentas usando a DB do Center
  4. Faz loop até stop_reason == "end_turn"
  5. Devolve streaming SSE em formato OpenAI-compatible
"""

import base64
import hashlib
import hmac
import json
import os
import subprocess
import threading
import time
import uuid
from collections import OrderedDict

import psycopg2
import psycopg2.extras
import redis as _redis_lib
import requests

_OPENWEBUI_BASE_URL = os.getenv("OPENWEBUI_BASE_URL", "").rstrip("/")

# Tools que requerem aprovação explícita do utilizador
APPROVAL_TOOLS = {"agentless_run", "agentless_run_bulk", "propose_infra_knowledge"}

# Nº máximo de hosts por chamada a agentless_run_bulk. Acima disto, uma única
# linha de comando Invoke-Command -ComputerName @(...) fica demasiado pesada
# (tempo de execução, paralelismo do WinRM) — o LLM deve dividir em lotes.
_BULK_MAX_HOSTS = 100

# Acções pendentes de aprovação — persistidas em Redis para sobreviver a restarts.
# TTL de 2 horas: se o utilizador não aprovar em 2h o token expira naturalmente.
_APPROVAL_TTL = 7200
_APPROVAL_PREFIX = "jarvis:approval:"

# Aprovações pendentes para canais de texto (Teams).
# Chave por chat_id — só pode haver uma acção pendente por conversa de cada vez.
_TEAMS_APPROVAL_PREFIX = "jarvis:teams:pending:"
_TEAMS_APPROVAL_TTL    = 600   # 10 minutos

_redis_client: "_redis_lib.Redis | None" = None
_redis_ok: bool = False
_redis_lock = threading.Lock()

def _get_redis() -> "_redis_lib.Redis | None":
    global _redis_client, _redis_ok
    if _redis_ok and _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            _redis_ok = False
            _redis_client = None
    with _redis_lock:
        if _redis_ok and _redis_client is not None:
            return _redis_client
        try:
            r = _redis_lib.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", "6379")),
                db=int(os.getenv("REDIS_DB", "0")),
                socket_timeout=2,
                socket_connect_timeout=2,
            )
            r.ping()
            _redis_client = r
            _redis_ok = True
        except Exception:
            _redis_client = None
            _redis_ok = False
    return _redis_client

# Fallback em memória para quando Redis não está disponível
_PENDING_ACTIONS: "OrderedDict[str, dict]" = OrderedDict()
_PENDING_ACTIONS_LOCK = threading.Lock()
_PENDING_ACTIONS_MAX = 200


def _store_pending_action(action: dict) -> str:
    token = uuid.uuid4().hex
    r = _get_redis()
    if r is not None:
        try:
            r.setex(f"{_APPROVAL_PREFIX}{token}", _APPROVAL_TTL, json.dumps(action))
            return token
        except Exception:
            pass
    with _PENDING_ACTIONS_LOCK:
        _PENDING_ACTIONS[token] = action
        while len(_PENDING_ACTIONS) > _PENDING_ACTIONS_MAX:
            _PENDING_ACTIONS.popitem(last=False)
    return token


def _store_teams_pending(chat_id: str, action: dict) -> None:
    """Guarda uma acção pendente de aprovação para um chat Teams (TTL 10 min)."""
    r = _get_redis()
    if r is not None:
        try:
            r.setex(f"{_TEAMS_APPROVAL_PREFIX}{chat_id}", _TEAMS_APPROVAL_TTL, json.dumps(action))
            return
        except Exception:
            pass


def pop_teams_pending(chat_id: str) -> dict | None:
    """Recupera e remove a acção pendente de aprovação Teams para um chat."""
    r = _get_redis()
    if r is not None:
        try:
            key = f"{_TEAMS_APPROVAL_PREFIX}{chat_id}"
            raw = r.get(key)
            if raw is not None:
                r.delete(key)
                return json.loads(raw)
        except Exception:
            pass
    return None


def _pop_pending_action(token: str) -> dict | None:
    r = _get_redis()
    if r is not None:
        try:
            key = f"{_APPROVAL_PREFIX}{token}"
            raw = r.get(key)
            if raw is not None:
                r.delete(key)
                return json.loads(raw)
        except Exception:
            pass
    with _PENDING_ACTIONS_LOCK:
        return _PENDING_ACTIONS.pop(token, None)


# ─────────────────────────────────────────────────────────────────────────────
# DETECTORES DE APRENDIZAGEM — em código, não confiados ao juízo do modelo.
#
# "Vale a pena lembrar" não pode depender só do LLM decidir chamar
# propose_infra_knowledge por iniciativa própria — isso falha sempre que ele
# não repara ou não se lembra. Estes detectores correm a seguir a cada
# execução real de agentless_run/agentless_run_bulk e reconhecem, por dados
# (sucesso/falha, host, parâmetros), os dois sinais fortes que já foram
# confirmados como válidos: falha corrigida no mesmo host, e uma correcção
# explícita do utilizador logo a seguir a uma falha.
# ─────────────────────────────────────────────────────────────────────────────

_AGENTLESS_FAIL_PREFIX = "jarvis:agentless_fail:"
_AGENTLESS_FAIL_TTL = 86400  # 24h — falhas mais antigas deixam de ser "recentes"


def _agentless_fail_key(host: str) -> str:
    return f"{_AGENTLESS_FAIL_PREFIX}{host.strip().lower()}"


def _record_agentless_host_outcome(host: str, machine_type: str, script: str, success: bool,
                                    error: str | None = None) -> dict | None:
    """Chamado depois de CADA execução real (por host) de agentless_run/bulk.

    Se falhar: grava um marcador (host, machine_type, erro) em Redis — não é
    ainda uma lição, é só memória de curto prazo do que se tentou.

    Se tiver sucesso E existir um marcador de falha recente para o mesmo
    host: é uma correcção confirmada por dados (não uma suposição do LLM) —
    propõe automaticamente um facto de âmbito 'host' para aprovação humana,
    e devolve um resumo para mostrar ao utilizador. Não depende de o modelo
    reparar ou decidir chamar nada.
    """
    if not host:
        return None
    r = _get_redis()
    key = _agentless_fail_key(host)

    if not success:
        if r is not None:
            try:
                r.setex(key, _AGENTLESS_FAIL_TTL, json.dumps({
                    "machine_type": machine_type,
                    "script_snippet": (script or "")[:200],
                    "error": error,
                }))
            except Exception:
                pass
        return None

    # Sucesso — havia uma falha recente por resolver para este host?
    if r is None:
        return None
    try:
        raw = r.get(key)
        if not raw:
            return None
        prior = json.loads(raw)
        r.delete(key)
    except Exception:
        return None

    if prior.get("machine_type") == machine_type:
        # Mesmo machine_type a funcionar agora — pode ter sido só uma
        # instabilidade pontual da rede, não uma lição sobre COMO aceder.
        return None

    title = f"Execução em {host}"
    instruction = (
        f"machine_type='{prior.get('machine_type')}' falhou"
        + (f" ({prior['error']})" if prior.get("error") else "")
        + f" — usar machine_type='{machine_type}' diretamente para este host."
    )
    action = {
        "tool":        "propose_infra_knowledge",
        "title":       title,
        "instruction": instruction,
        "category":    "infra_general",
        "version":     None,
        "evidence":    f"detectado automaticamente — falha confirmada seguida de sucesso, {_dt_now_str()}",
        "scope_type":  "host",
        "scope_value": host,
    }
    token = _store_pending_action(action)
    return {"host": host, "title": title, "instruction": instruction, "token": token}


def _dt_now_str() -> str:
    from datetime import datetime as _dt
    return _dt.now().strftime("%Y-%m-%d %H:%M")


def _track_agentless_result(tool_name: str, tool_input: dict, result_json: str) -> list[dict]:
    """Extrai o(s) resultado(s) por host de um agentless_run/agentless_run_bulk
    já executado, e corre o detector de correcção em cada um. Devolve uma
    lista de correcções detectadas (0 ou mais), já propostas para aprovação."""
    if tool_name not in ("agentless_run", "agentless_run_bulk"):
        return []
    try:
        data = json.loads(result_json)
    except Exception:
        return []

    notices = []
    machine_type = tool_input.get("machine_type", "server")
    script = tool_input.get("script", "")

    if tool_name == "agentless_run":
        host = tool_input.get("host", "")
        success = bool(data.get("success"))
        error = data.get("stderr") or data.get("error")
        notice = _record_agentless_host_outcome(host, machine_type, script, success, error)
        if notice:
            notices.append(notice)
    else:
        for host, r in (data.get("results") or {}).items():
            success = bool(r.get("success"))
            error = r.get("stderr")
            notice = _record_agentless_host_outcome(host, machine_type, script, success, error)
            if notice:
                notices.append(notice)

    return notices


def _agentless_fail_hint(message_text: str) -> str:
    """Se a mensagem actual do utilizador mencionar um host com uma falha de
    agentless recente por resolver, devolve uma directiva a lembrar o Jarvis
    de considerar gravar a orientação do utilizador como lição — sem isto,
    a única forma de o modelo "reparar" nesse momento seria por iniciativa
    própria, o que não é fiável."""
    if not message_text:
        return ""
    r = _get_redis()
    if r is None:
        return ""
    try:
        text_lower = message_text.lower()
        hits = []
        for key in r.scan_iter(f"{_AGENTLESS_FAIL_PREFIX}*"):
            host = (key.decode() if isinstance(key, bytes) else key)[len(_AGENTLESS_FAIL_PREFIX):]
            if host in text_lower:
                hits.append(host)
        if not hits:
            return ""
        hosts_str = ", ".join(hits)
        return (
            f"\n\n## Falha de agentless recente por resolver\n"
            f"Tentativa(s) recente(s) de aceder a {hosts_str} via agentless falhou/falharam e ainda não "
            f"foi corrigida. Se esta mensagem do utilizador disser como proceder nesse caso (ex: usar "
            f"outra integração, outro caminho de acesso), grava isso já com `propose_infra_knowledge` "
            f"(scope_type='host', scope_value='{hits[0]}') — é um sinal forte, uma instrução directa do "
            f"utilizador não precisa de se repetir para valer a pena lembrar."
        )
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT  (copiado de jarvis-ctl/commands/chat.py)
# ─────────────────────────────────────────────────────────────────────────────

JARVIS_SYSTEM = """És o Jarvis, um sistema de operações de infraestrutura AI com capacidade para investigar e resolver problemas em máquinas Windows remotas — servidores e workstations.

Operas como um SRE sénior com shell remota em qualquer máquina Windows do domínio.

Ajudas com qualquer pedido do utilizador — infraestrutura, configuração, segurança, Intune, GPO, networking, scripting, e questões técnicas gerais. Nunca recuses um pedido dizendo que está "fora do âmbito" — és um assistente técnico completo, com especialidade em infraestrutura.

## Como trabalhar

**Investigação primeiro, acção depois.**
Quando reportado um problema, investiga antes de concluir:
1. Usa agentless_run para obter estado em tempo real — lê logs, verifica serviços, lista processos, corre diagnósticos
2. Continua a investigar até perceber a CAUSA RAIZ, não apenas o sintoma
3. Só quando tens evidências propões e executas a correcção

**Sem dados históricos/DB nesta conversa, EXCETO via integrações Clotho configuradas.** Se as secções "Integrações configuradas (Clotho)" e/ou "Referências $$ nesta mensagem" estiverem presentes mais abaixo neste prompt, usa `call_integration_tool`/`query_database` para responder com base nessas integrações/bases de dados (alertas, problemas, histórico, etc.). Fora isso, toda a informação tem de vir de comandos executados ao vivo via agentless_run. A única "história" a que tens acesso desta forma é a que está armazenada na própria máquina (event logs, ficheiros de log, histórico de serviços/processos, etc.) — obtida via agentless_run no momento. Se o utilizador pedir dados históricos agregados de várias máquinas, tendências ou análises UEBA para os quais não exista uma integração Clotho configurada, explica que essa informação está disponível noutras ferramentas do Jarvis (não neste chat) e, se fizer sentido, propõe uma alternativa via agentless_run para obter o estado/histórico local da(s) máquina(s) em causa.

## agentless_run — como funciona

**agentless_run é a tua shell remota.** Funciona via WinRM em QUALQUER máquina Windows do domínio — NÃO precisa de agente Jarvis instalado.

- **Workstations** (`machine_type="workstation"`): usa `Invoke-Command` com Kerberos + conta gMSA `sprd_passreset$`. Não precisas de password nem de agente.
- **Servidores** (`machine_type="server"`): usa pywinrm com NTLM e credenciais de serviço configuradas no Jarvis.

**Como escolher machine_type:**
- Se o utilizador indicar explicitamente o tipo (ex: "servidor X", "workstation Y", ou "host (server)"), respeita essa indicação.
- Se não tens a certeza, usa `query_ad` com filtro `(&(objectClass=computer)(name=HOSTNAME))` e atributos `["distinguishedName","operatingSystem"]`. A **OU** no `distinguishedName` indica o tipo (ex: `OU=Servers` → server, `OU=Workstations` ou `OU=Baseline WKS` → workstation). O `operatingSystem` também ajuda ("Windows Server*" → server).
- Em caso de dúvida, usa `machine_type="server"` — NTLM funciona em mais cenários que Kerberos.
- **Não confies apenas no prefixo do hostname** — workstations podem ter qualquer nome, não apenas WKS*.

**WinRM é instável por natureza.** Máquinas offline, firewalls, DNS que não resolve, timeouts — são a norma num parque de milhares de máquinas, não excepções. Não dramatizes falhas de ligação. Reporta factualmente quantas responderam e quantas ficaram inacessíveis, e trabalha com o que tens.

**Quando agentless_run falha**, as causas reais são:
- WinRM não está activado na máquina alvo (`winrm quickconfig` resolve)
- Firewall a bloquear portas 5985/5986
- Máquina offline ou não acessível na rede
- Problema Kerberos (ticket expirado, máquina fora do domínio)
- Para workstations: o hostname deve ser o nome NetBIOS da máquina no AD
- Se recebeste Access Denied com `machine_type="workstation"` num servidor, **troca para `machine_type="server"` e tenta de novo automaticamente** na mesma resposta (sem pedir novo cartão).

**NUNCA** sugiras "instalar o agente Jarvis" como solução para falhas do agentless_run — o agentless_run foi concebido exactamente para não precisar de agente.

**Antes de investigar EDR/XDR, firewall, agentes SIEM ou baseline de workstation** — consulta primeiro a secção `## Conhecimento de infraestrutura` mais abaixo neste prompt (se existir). Se já houver um facto confirmado (ex: qual o EDR padrão e o nome do serviço Windows real), usa-o directamente no script em vez de adivinhar por nomes de produtos genéricos ou legados — nunca partas de uma suposição não verificada (incluindo suposições vindas da própria mensagem do utilizador).

**Antes de correr `agentless_run`/`agentless_run_bulk` num host que já foi mencionado nesta conversa** — verifica se existe a secção `## Conhecimento específico desta conversa` mais abaixo. Se houver uma lição sobre esse host/alvo (ex: "usar machine_type='server' directamente", "sem acesso — usar Splunk para segurança"), aplica-a de imediato em vez de repetir a tentativa que já se sabe que falha.

**Scripts PowerShell — mantém curtos e focados:**
- Cada script deve ter **no máximo 15-20 linhas**. Se precisas de mais, divide em 2 acções separadas.
- Devolve apenas o ESSENCIAL — usa `Select-Object` para limitar campos, `-First N` para limitar linhas.
- NÃO faças scripts "recon completo" com 50+ linhas. Faz investigação focada: 1 pergunta → 1 script curto.
- Exemplos de scripts compactos e eficazes:
  - Sessões: `query user`
  - Logs: `Get-Content C:\\app\\logs\\error.log -Tail 200`
  - Serviços: `Get-Service | Where-Object {$_.Status -eq 'Stopped'} | Select Name,Status,StartType`
  - Processos: `Get-Process | Sort-Object CPU -Desc | Select-Object -First 20 Name,Id,CPU,WorkingSet`
  - Disco: `Get-PSDrive | Where-Object {$_.Provider -like '*FileSystem*'} | Select Name,Used,Free`
  - Event log: `Get-EventLog -LogName System -Newest 50 -EntryType Error,Warning | Select TimeGenerated,Source,Message`
  - Software instalado: `Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*,HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* | Select DisplayName,DisplayVersion | Sort DisplayName`
  - Reparar serviço: `Restart-Service <nome> -Force`

**Verificar se o utilizador está activo/presente numa máquina:**
- Usa `query user` (ou `quser`) — devolve por sessão o estado (Active/Disc) e a coluna **IDLE TIME**, já calculada pelo SO a partir do input real do utilizador. Funciona perfeitamente via `Invoke-Command` porque consulta a tabela de sessões via RPC, não o desktop local.
- **NUNCA** tentes medir idle time com `GetLastInputInfo`/P-Invoke ou outro hack de "último input" — isso mede o desktop da própria sessão remota do WinRM (não-interactiva), não a sessão do utilizador, e dá valores sem sentido.
- Combina com `Get-Process | Where-Object {$_.MainWindowTitle -ne ""}` para veres em que o utilizador estava a trabalhar.
- Se `query user` não devolver sessões (ninguém com sessão activa), conclui isso directamente — não inventes outro método para "confirmar presença física".

**Consultar o Active Directory — `query_ad`:**
- `query_ad` é uma consulta directa ao AD via LDAP (ADSI) — corre localmente onde o Jarvis está, **sem** host remoto, **sem** WinRM, **sem** Domain Controller, e **sem** aprovação (é só leitura).
- Recebe um filtro LDAP (`filter`) e, opcionalmente, a lista de atributos a devolver (`attributes`) e um `limit`. Devolve uma lista de objectos com esses atributos.
- Usa para: resolver "quem é o utilizador X" (departamento, manager, email, grupos, último logon) ou contexto sobre uma máquina (OU, SO, último logon).
- Filtros típicos:
  - Utilizador: `(&(objectClass=user)(sAMAccountName=NOME_UTILIZADOR))`
  - Computador por nome/padrão: `(&(objectClass=computer)(name=PADRAO*))`
  - **TODAS as workstations Windows 10/11**: `(&(objectClass=computer)(operatingSystem=Windows 1*))` — filtro simples e abrangente
  - **Servidores**: `(&(objectClass=computer)(operatingSystem=Windows Server*))`
- **Filtros simples primeiro.** Não tentes filtros complexos com `distinguishedName` ou exclusões de OUs — filtra depois no texto. Um filtro simples que devolve mais resultados é melhor que um filtro complexo que perde metade.
- Atributos como `lastLogonTimestamp` já vêm convertidos para data/hora legível — não precisas de converter FileTime manualmente.
- **Só chames `query_ad` quando precisares mesmo de informação que ainda não tens.** Se o utilizador já deu a lista de máquinas/utilizadores, ou a informação já está na conversa, não repitas a consulta. Não uses para histórico de eventos ou estado de máquina — isso é sempre `agentless_run`/`agentless_run_bulk`.
- O `count` devolvido por `query_ad` reflecte sempre o total real de máquinas/utilizadores que correspondem ao filtro (não há tecto artificial). Usa esse número para calcular quantos lotes de `agentless_run_bulk` vais precisar (ceil(count/100)).
- **Se o count for muito grande** (>500 máquinas) para uma pergunta de inventário de software, avisa o utilizador: "O parque tem N máquinas. Verificar software instalado exige acesso remoto a cada uma (em lotes de 100). Queres avançar ou preferes limitar a um grupo específico?"

## agentless_run_bulk — perguntas sobre um conjunto de máquinas

Nem toda pergunta é sobre uma máquina. Algumas só fazem sentido olhando para um GRUPO/CONJUNTO de máquinas ao mesmo tempo — comparar, contar, cruzar resultados entre elas. Repara nisto pelo conteúdo da pergunta: ela refere-se a um conjunto/categoria/grupo de máquinas (por nome, padrão, OU, tipo, função, etc.), não a uma máquina específica — independentemente de como está formulada. Aplica-se da mesma forma a servidores e a workstations.

**Como obter a lista de máquinas-alvo:**
- Se o utilizador já deu a lista explícita de máquinas, usa-a directamente.
- Caso contrário, usa `query_ad` para construir a lista — filtra `objectClass=computer` pelo critério que o utilizador indicou (nome/padrão no `name`, OU/`distinguishedName`, `operatingSystem`, `description`, etc.). Não adivinhes nomes de máquinas nem peças ao utilizador para listar máquina a máquina. O mecanismo é o mesmo para servidores e workstations; muda apenas o filtro AD usado.

**Como executar:**
- Usa `agentless_run_bulk` com a lista de hosts e UM script — corre em paralelo em todas as máquinas, como uma ÚNICA acção aprovada pelo utilizador (um só cartão de aprovação cobre todas as máquinas dessa chamada).
- O script corrido em cada máquina deve devolver o MÍNIMO necessário (uma linha/objecto compacto por máquina). Não tentes agregar, comparar ou cruzar dados dentro do script — recebes o resultado de cada máquina separadamente e fazes tu a agregação final (juntar, comparar, contar, filtrar) depois de teres todos os resultados.
- Máquinas que aparecem em `unreachable` (offline, fora da rede, sem WinRM, etc.) são esperadas num parque de máquinas — não é um erro a investigar nem motivo para repetir a acção; trabalha com os resultados que respondem e refere quantas/quais ficaram de fora.
- Marca essas máquinas como offline/não-respondeu na tua lista de resultados e continua — **nunca pares nem interrompas a tarefa por causa disso**. No relatório final inclui sempre as três contagens: total-alvo, responderam, offline.

**Limite de 100 hosts por chamada — divide em lotes:**
- Cada chamada a `agentless_run_bulk` aceita no máximo 100 hosts. Se a lista de máquinas-alvo for maior, divide-a em lotes sequenciais de até 100 (ex: 620 máquinas → 7 lotes).
- Cada lote é uma chamada `agentless_run_bulk` separada, com o seu próprio cartão de aprovação — isto é esperado e normal. Vai pedindo aprovação lote a lote (não tentes contornar o limite juntando hosts numa única chamada).
- Acumula os resultados (`results` e `unreachable`) de todos os lotes à medida que vão chegando, e só fazes a agregação final (contagens, cruzamentos, CSV, etc.) depois do último lote.
- **Importante**: assim que receberes o resultado de um lote, escreve de imediato no texto visível ao utilizador as contagens exactas desse lote (ex: "Lote 2/5: 7 responderam de 99") e o total acumulado até esse ponto (ex: "Acumulado: 8/196"), antes de pedires o cartão do lote seguinte. Cada pedido a esta API só recebe o texto das respostas anteriores (não os dados brutos das ferramentas) — se não escreveres os números de cada lote, perdes essa informação e não conseguirás fazer a agregação final correctamente.
- Mantém o objectivo original da tarefa (nº total de máquinas-alvo, critério, formato de saída pedido) ao longo de toda a conversa e de todos os lotes — não o reduzas nem o percas por causa de mensagens intermédias do utilizador. Ao descreveres o que já fizeste, baseia-te apenas nas chamadas de ferramentas e resultados reais desta conversa, nunca em suposições.

**Antes do(s) cartão(ões) de aprovação de uma acção em massa**, explica em texto ao utilizador:
- Quantas máquinas serão alvo no total e como foram identificadas (lista dada pelo utilizador ou consulta AD/OU)
- Em quantos lotes vai ser dividido (se > 100 máquinas)
- O que o script vai fazer em cada máquina
- Tempo estimado (cada máquina demora tipicamente alguns segundos a cerca de 1 minuto; o tempo total cresce com o número de máquinas, mesmo com paralelismo)
- Que parte dos resultados pode ficar incompleta (máquinas que não respondem) e que isso é normal

Isto dá ao utilizador — e a ti próprio — uma noção real do peso/impacto da acção antes dela correr.

**Deixa os dados guiar.** Não assumes a solução antes de ver a evidência.

**Confirma acções destrutivas.** Antes de reiniciar serviços, modificar ficheiros ou alterar configuração de sistema — explica o que vais fazer e porquê.

## Tarefas agendadas e monitoring — `manage_task`

**NÃO podes esperar, fazer pausa, nem voltar mais tarde neste chat.** Cada mensagem é um pedido HTTP independente — não tens memória entre mensagens nem capacidade de callback.

Quando o utilizador pedir:
- "verifica daqui a 1 hora" → usa `manage_task` para criar uma tarefa Lachesis com a instrução
- "monitoriza isto" → cria tarefa com schedule interval ou once
- "amanhã às 09:00 faz um relatório" → cria tarefa once

A tarefa fica no scheduler do Lachesis e executa automaticamente. O resultado fica guardado e visível na UI do Fates Engine.

**NUNCA digas "volto daqui a X minutos" nem "fico de olho"** — não tens essa capacidade. Sê honesto: cria a tarefa e explica que ela será executada automaticamente pelo scheduler.

## Estratégias inteligentes

**Se uma abordagem falhar, muda de estratégia.** Nunca repitas a mesma tool com os mesmos parâmetros mais do que 2 vezes. Se call_integration_tool falha, tenta outra tool ou abordagem. Se agentless_run falha com Access Denied, troca machine_type na mesma resposta.

**Antes de operações em massa**, testa com 1-2 máquinas primeiro. Se falharem (DNS, WinRM, permissões), diagnostica antes de lançar para 100.

**Combina operações** quando possível. Em vez de 5 cards para ler 5 ficheiros num servidor, escreve 1 script que lê os 5 de uma vez. Objectivo: máximo 3-5 cards por tarefa, não 15-20.

## Casa do Conhecimento — `propose_infra_knowledge`

Sempre que aprenderes algo que vale a pena lembrar depois, usa `propose_infra_knowledge` — não deixes a informação perder-se no fim da conversa. Fica sempre pendente de aprovação humana antes de se tornar permanente.

**Três âmbitos possíveis** (campo `scope_type`):
- `global` — aplica-se a toda a infraestrutura (ex: "o EDR padrão é CrowdStrike Falcon"). Fica sempre presente no teu raciocínio, em qualquer conversa.
- `host` — só sobre uma máquina específica (ex: "o agentless não consegue chegar a DC-X, mas ele está activo — para questões de segurança usar Splunk, para infra usar Zabbix"). `scope_value` = o hostname exacto.
- `target` — só sobre um alvo do Explorador (ex: "o alvo X cai todos os dias às 10h pelo motivo Y"). `scope_value` = o nome do alvo.

Factos com âmbito `host`/`target` só te são mostrados quando essa máquina/alvo for mencionado numa conversa futura — não pesam no prompt o resto do tempo.

**Quando propor — o critério é a força do sinal, não um evento fixo.** Propõe sempre que um destes acontecer:
- O utilizador dá-te uma instrução directa e explícita sobre como lidar com algo (ex: "já que não consegues chegar ao DC, usa o Splunk para questões de segurança") — uma vez já chega, é um sinal forte.
- Uma falha clara seguida de uma correcção que resolveu o problema (ex: `machine_type=workstation` deu Access Denied, `machine_type=server` funcionou — propõe isso com âmbito `host` ou, se for um padrão de OU inteira, `global`/categoria `infra_general`).
- Um padrão que se repete de forma consistente ao longo de várias observações, sem ninguém pedir.

Não proponhas suposições nem factos de uma amostra demasiado pequena/atípica para serem generalizáveis — só o que confirmaste directamente com dados reais ou que o utilizador confirmou explicitamente.

**Amostragem científica (para factos `global` aprendidos por padrão, não por instrução directa)** — quando o utilizador te pedir para ires activamente à procura de padrões (ex: "vai ao AD e analisa que EDR está instalado nos servidores", "verifica se o baseline de workstation é consistente"), em vez de esperar que isso apareça por acaso numa investigação, aplica sempre esta disciplina:
- **Amostra representativa** — usa `query_ad` para construir a população-alvo (por OU, padrão de nome, ou `operatingSystem`), nunca "as primeiras máquinas que responderem". Se a população for grande (>50), amostra pelo menos 30-50 máquinas ou 20% dela (o que for maior); se for pequena, cobre a maioria.
- **agentless_run_bulk** para testar a amostra com um único script compacto.
- **Não-respostas não enviesam o resultado** — máquinas inacessíveis contam-se à parte; a consistência calcula-se só sobre as que responderam.
- **Limiar de confiança** — só propor um facto se ≥80% das que responderam concordarem, E houver pelo menos 15-20 respostas (ou toda a amostra disponível, se a população for menor). Abaixo disso, relata o que encontraste mas não proponhas via `propose_infra_knowledge` ainda — sugere ao utilizador repetir mais tarde para acumular mais evidência.
- **Evidência com números reais** — o campo `evidence` de `propose_infra_knowledge` tem sempre o formato "N confirmam / N responderam (critério da amostra, data)", nunca uma afirmação sem os números por trás.
- Se o utilizador pedir para isto se repetir sozinho ("faz isto todas as semanas"), usa `manage_task` para criar a tarefa agendada — a mesma disciplina de amostragem aplica-se em cada execução.

**Sempre que uma verificação falhar, der timeout, ou não devolver dados** (WinRM inacessível, host não encontrado num índice/lookup de integração, etc.) — nunca reportes isso como "confirmado ausente/inativo/desligado". Reporta como "não foi possível confirmar" e explica exactamente qual verificação falhou e porquê. Ausência de dados não é o mesmo que confirmação de ausência.

## Memória pessoal — `save_memory` / `recall_memory`

Distinta da Casa do Conhecimento (que é sobre infraestrutura, com aprovação humana): isto é sobre ESTE utilizador — quem é, como prefere trabalhar contigo, o que já te disse antes. Não fica pendente de aprovação — grava-se de imediato, tal como o ChatGPT/Claude fazem, e o utilizador revê/apaga quando quiser em Definições > Personalização > Memória.

**Usa `save_memory` sempre que reconheceres um destes sinais** (não esperes que o utilizador peça explicitamente "grava isto"):
- Qualquer instrução sobre **estilo/tom/formato de resposta** ("sê breve", "sem resumo final", "responde sempre em português") — isto é SEMPRE `save_memory`, mesmo que soe como uma regra geral. Nunca `propose_infra_knowledge` — essa é só para infraestrutura/máquinas/alvos, nunca para como conversas com o utilizador.
- O utilizador diz-te directamente para te lembrares de algo ("a partir de agora...", "sempre que eu pedir X, faz Y").
- Corrige a tua abordagem ("não, prefiro que...") — grava a correcção, não só a repares nesta conversa.
- Confirma que uma forma de trabalhar específica resultou bem, mesmo sem correcção prévia.
- Partilha algo sobre o seu papel, equipa, ou responsabilidades que devia moldar como lhe respondes no futuro (ex: "sou responsável pelos servidores SVBES*").

Grava em texto simples, autónomo e específico — não vago. Não gravues factos óbvios, efémeros, ou já derivável do próprio código/histórico.

**Usa `recall_memory`** quando precisares activamente de contexto que pode já ter sido dado numa conversa anterior — por exemplo, o utilizador refere algo que pressupõe contexto partilhado antes, ou estás prestes a perguntar-lhe algo que ele já pode ter respondido. Não abuses disto em cada mensagem — só quando genuinamente ajuda.

## Regras

- Toda a informação sobre estado actual vem de agentless_run — nunca inventes nem estimes
- Mostra números/resultados reais devolvidos pelos comandos, não resumos vagos
- Responde no idioma do utilizador (português)
- Quando ages numa máquina, reporta exactamente o que fizeste e o resultado
- **Nunca peças uma nova acção (cartão de aprovação) sem primeiro reportares em texto visível o resultado da acção anterior** — o que encontraste, sucesso ou erro, dados relevantes. Os resultados das ferramentas não ficam disponíveis nos turnos seguintes desta API; só o texto que escreveres sobrevive. Se pedires acção após acção sem nunca relatares o que encontraste, perdes essa informação e acabas a repetir os mesmos pedidos sem avançar.
- NÃO te identifies como ChatGPT, Claude ou qualquer outro produto — és o Jarvis
"""


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_openwebui_jwt(token: str, secret: str) -> dict | None:
    """Verifica (stdlib, sem PyJWT) um JWT HS256 emitido pelo Open WebUI
    (X-OpenWebUI-User-Jwt). Devolve o payload ou None se inválido/expirado."""
    try:
        h_b64, p_b64, s_b64 = token.split(".")
        sig = hmac.new(secret.encode(), f"{h_b64}.{p_b64}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(sig, _b64url_decode(s_b64)):
            return None
        payload = json.loads(_b64url_decode(p_b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def _build_system_prompt(base: str = JARVIS_SYSTEM, message_text: str = "", has_fates_access: bool = True) -> str:
    from datetime import datetime as _dt
    out = base + f"\n\n## Data e hora actual\n{_dt.now().strftime('%Y-%m-%d %H:%M:%S')} (WAT, UTC+1)\n"
    try:
        from lachesis.lachesis_store import get_behaviors_prompt_suffix
        out += get_behaviors_prompt_suffix(message_text)
    except Exception:
        pass
    try:
        out += _agentless_fail_hint(message_text)
    except Exception:
        pass
    if has_fates_access:
        try:
            from clotho.clotho_mentions import describe_integrations_for_prompt, mentions_context_block
            out += describe_integrations_for_prompt()
            out += mentions_context_block(message_text)
        except Exception:
            pass
    return out


def _last_user_text(messages: list) -> str:
    """Extrai o texto da última mensagem role=user (para expansão de menções $nome$)."""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                return "".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            return content if isinstance(content, str) else ""
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# TOOL DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "agentless_run",
        "description": "Execute a PowerShell script on a remote machine via WinRM. Use for real-time diagnostics, log reading, service management. The result includes '_known_profile' when this host has recent recurring failures or a preferred machine_type from prior executions — apply that guidance before retrying blindly. '_known_profile.last_investigation', when present, is the consolidated context of the last investigation session on this host (why it was started, how many executions, what was run and found) — use it to avoid repeating an investigation that already reached a conclusion recently.",
        "input_schema": {
            "type": "object",
            "required": ["host", "script"],
            "properties": {
                "host":         {"type": "string", "description": "Target hostname or IP"},
                "script":       {"type": "string", "description": "PowerShell script to execute"},
                "machine_type": {"type": "string", "enum": ["workstation", "server"], "description": "Check the target's OU via query_ad if unsure. 'server' uses NTLM, 'workstation' uses Kerberos/gMSA. Default: server"},
                "timeout_s":    {"type": "integer", "description": "Timeout in seconds. Default: 60"},
            },
        },
    },
    {
        "name": "query_ad",
        "description": "Query Active Directory directly via LDAP/ADSI — runs locally where Jarvis runs, no remote host, no WinRM, no Domain Controller needed, no approval required (read-only). Use to resolve user/computer attributes (department, manager, OU, last logon, etc.) or to build a list of target machines for a fleet-wide question (filter by name pattern, OU, type, etc.). Do NOT use for live machine state, logs, processes or services — that's agentless_run/agentless_run_bulk. Only call this when you actually need AD info you don't already have.",
        "input_schema": {
            "type": "object",
            "required": ["filter"],
            "properties": {
                "filter":     {"type": "string", "description": "LDAP filter, e.g. \"(&(objectClass=computer)(name=PATTERN*))\" or \"(&(objectClass=user)(sAMAccountName=username))\""},
                "attributes": {"type": "array", "items": {"type": "string"}, "description": "AD attributes to return (e.g. name, operatingSystem, lastLogonTimestamp, distinguishedName, department, manager, mail, memberOf). Defaults to a small useful set."},
                "limit":      {"type": "integer", "description": "Max results. 0 ou omitido = sem limite (devolve todos os resultados que correspondem ao filtro)."},
            },
        },
    },
    {
        "name": "agentless_run_bulk",
        "description": "Execute the SAME PowerShell script across MULTIPLE remote machines via WinRM, in parallel, as a single approved action. Use for fleet-wide questions that span a set of machines. Each host returns its own result; unreachable hosts are reported separately and do not fail the whole action. Max 100 hosts per call — for larger sets, split into sequential batches of up to 100 and aggregate the results yourself afterwards.",
        "input_schema": {
            "type": "object",
            "required": ["hosts", "script"],
            "properties": {
                "hosts":        {"type": "array", "items": {"type": "string"}, "description": "Target hostnames or IPs"},
                "script":       {"type": "string", "description": "PowerShell script to execute on each host. Keep the output minimal/compact (e.g. one row/object per host) — final aggregation across hosts is done afterwards."},
                "machine_type": {"type": "string", "enum": ["workstation", "server"], "description": "Check the target's OU via query_ad if unsure. 'server' uses NTLM, 'workstation' uses Kerberos/gMSA. Default: server"},
                "timeout_s":    {"type": "integer", "description": "Timeout in seconds for the whole batch. Default: 120"},
            },
        },
    },
    {
        "name": "query_agentless_history",
        "description": (
            "Consulta o log bruto de auditoria de execuções agentless (agentless_execution_log) — "
            "só leitura, sem aprovação. Sem 'host', devolve as execuções mais recentes em TODAS as "
            "máquinas (ex: 'qual foi a última máquina em que correste algo?', 'o que fizeste nas "
            "últimas horas?'). Com 'host', o histórico dessa máquina específica (ex: 'o que já foi "
            "feito no SRV-EXAMPLE01?'). Isto é o registo bruto por execução — para um resumo já "
            "consolidado de uma investigação específica, usa '_known_profile.last_investigation' "
            "que já vem no resultado de agentless_run, não precisas de chamar esta tool para isso."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host":        {"type": "string", "description": "Hostname exacto. Omite para ver execuções recentes em todas as máquinas."},
                "since_hours": {"type": "integer", "description": "Só execuções das últimas N horas. Omite para não filtrar por tempo."},
                "limit":       {"type": "integer", "description": "Máximo de linhas a devolver (default 20, máximo 100)."},
            },
        },
    },
]

# Tools de integração Clotho — só disponíveis para utilizadores com acesso ao
# Fates Engine (ver verify_openwebui_jwt / has_fates_access).
_FATES_TOOLS = [
    {
        "name": "call_integration_tool",
        "description": "Invoca uma tool gerada pelo Clotho para uma integração configurada (ex: Zabbix, Checkpoint, Palo Alto) — consulta problemas, hosts, alertas, etc. via a API externa já testada.",
        "input_schema": {
            "type": "object",
            "required": ["integration_name", "tool_name"],
            "properties": {
                "integration_name": {"type": "string", "description": "Nome da integração configurada no Clotho (ex: 'Zabbix BAI')"},
                "tool_name":        {"type": "string", "description": "Nome da tool dentro do catálogo da integração"},
                "params":           {"type": "object", "description": "Parâmetros a passar à tool, conforme o params_schema definido"},
            },
        },
    },
    {
        "name": "query_database",
        "description": "Executa uma consulta SQL só-leitura (SELECT) numa base de dados gerida pelo Clotho (ligação configurada ou tabela importada de CSV). Usa para responder perguntas sobre dados estruturados.",
        "input_schema": {
            "type": "object",
            "required": ["database_name", "sql"],
            "properties": {
                "database_name": {"type": "string", "description": "Nome da base de dados configurada no Clotho (ver $db.nome$)"},
                "sql":            {"type": "string", "description": "Consulta SQL SELECT só-leitura"},
            },
        },
    },
]

TOOL_DEFINITIONS = TOOL_DEFINITIONS + _FATES_TOOLS + [
    {
        "name": "manage_task",
        "description": (
            "Cria ou lista tarefas agendadas no Lachesis (scheduler do Jarvis). "
            "Usa para agendar verificações futuras, relatórios periódicos, ou follow-ups. "
            "Quando o utilizador pedir 'verifica daqui a 1 hora' ou 'monitoriza isto', "
            "cria uma tarefa com a instrução e o agendamento adequados."
        ),
        "input_schema": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list"],
                    "description": "create = nova tarefa, list = listar tarefas existentes",
                },
                "name":        {"type": "string", "description": "Nome curto da tarefa (só para create)"},
                "instruction": {"type": "string", "description": "Instrução completa que o Jarvis executará quando a tarefa disparar (só para create). Deve ser autónoma — incluir hostname, o que verificar, etc."},
                "schedule_description": {"type": "string", "description": "Quando executar, em linguagem natural (ex: 'daqui a 1 hora', 'todos os dias às 09:00', 'amanhã às 10:00'). Só para create."},
            },
        },
    },
    {
        "name": "propose_infra_knowledge",
        "description": (
            "Propõe um facto reutilizável para a Casa do Conhecimento — SÓ sobre "
            "infraestrutura/máquinas/alvos (ex: EDR/XDR padrão, firewall, agente Zabbix, "
            "'o agentless não consegue chegar ao DC-X', 'o alvo X cai todos os dias às 10h'). "
            "NÃO uses isto para preferências do utilizador sobre COMO conversar contigo "
            "(estilo de resposta, tom, formato) — isso é sempre `save_memory`, nunca esta "
            "tool, mesmo que pareça uma 'instrução directa e explícita'. Usa "
            "`propose_infra_knowledge` sempre que um sinal for forte o suficiente para valer "
            "a pena lembrar depois sobre a INFRAESTRUTURA — uma instrução directa e explícita "
            "do utilizador sobre um sistema/máquina/alvo (uma vez já chega), uma falha "
            "clara seguida de correcção, ou um padrão que se repete sem ninguém pedir. Não "
            "deixes essa informação perder-se no fim da conversa. Requer aprovação humana "
            "antes de ficar permanente; até lá fica pendente."
        ),
        "input_schema": {
            "type": "object",
            "required": ["title", "instruction"],
            "properties": {
                "title":       {"type": "string", "description": "Nome curto do facto, ex: 'EDR padrão' ou 'DC-X inacessível via agentless'"},
                "instruction": {"type": "string", "description": "O facto em si e como usá-lo em investigações futuras, ex: 'CrowdStrike Falcon (serviço CSFalconService) — não Symantec/Cylance.' ou 'Sem acesso directo — para perguntas de segurança usar Splunk, para infra usar Zabbix.'"},
                "category":    {"type": "string", "enum": ["edr_xdr", "firewall", "siem_agent", "workstation_baseline", "application", "infra_general"], "description": "Categoria do facto (só relevante para scope_type='global')"},
                "version":     {"type": "string", "description": "Versão do produto, se relevante/confirmada"},
                "evidence":    {"type": "string", "description": "Como foi confirmado, ex: 'confirmado em 6/8 servidores em 2026-07-14' ou 'utilizador confirmou em 2026-07-16'"},
                "scope_type":  {"type": "string", "enum": ["global", "host", "target"], "description": "'global' = aplica-se a toda a infraestrutura (default). 'host' = só a uma máquina específica (usa scope_value=hostname). 'target' = só a um alvo do Explorador (usa scope_value=nome do alvo)."},
                "scope_value": {"type": "string", "description": "Obrigatório se scope_type for 'host' ou 'target' — o hostname exacto ou nome do alvo, para o facto ser reconhecido em conversas futuras que o mencionem."},
            },
        },
    },
    {
        "name": "save_exploration_tip",
        "description": (
            "Guarda a conclusão de uma investigação de um tip do Explorer (Scope Collector / "
            "Fates Engine) na tabela de tips. Usa sempre no final de uma investigação iniciada "
            "a partir de um tip marcado como 'precisa de investigação' — a conclusão não fica "
            "guardada em lado nenhum se não chamares esta tool antes da conversa terminar."
        ),
        "input_schema": {
            "type": "object",
            "required": ["scope_target_id", "title", "summary", "priority"],
            "properties": {
                "scope_target_id":   {"type": "integer", "description": "ID do scope_target investigado (vem no contexto inicial da conversa)"},
                "title":             {"type": "string", "description": "Título curto da conclusão"},
                "summary":           {"type": "string", "description": "Resumo em 1-2 frases"},
                "detail":            {"type": "string", "description": "Explicação técnica completa e recomendação, incluindo o que a investigação agentless confirmou"},
                "priority":          {"type": "string", "enum": ["low", "medium", "high", "critical"], "description": "Prioridade da conclusão"},
                "supersedes_tip_id": {"type": "integer", "description": "ID do tip original que motivou esta investigação (opcional) — é marcado como 'resolved'"},
            },
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Grava uma memória pessoal sobre ESTE utilizador — não sobre infraestrutura "
            "(isso é propose_infra_knowledge, e exige aprovação; esta tool não exige). "
            "Usa SEMPRE esta tool, nunca propose_infra_knowledge, quando o que o utilizador "
            "pede for sobre COMO tu conversas com ele: estilo/tom/formato de resposta "
            "('sê breve', 'sem resumo final', 'responde sempre em português'), preferências "
            "de trabalho, ou factos sobre o seu papel/equipa. Usa também sempre que, na "
            "conversa, surgir algo que vale a pena lembrar-te depois: o utilizador diz-te "
            "directamente para lembrares algo, ou corrige a tua abordagem, ou confirma que "
            "uma forma de trabalhar funcionou bem. Não esperes que o utilizador peça "
            "explicitamente 'grava isto' — grava assim que reconheceres o sinal. Fica "
            "disponível de imediato (sem aprovação) em conversas futuras; o utilizador pode "
            "sempre rever ou apagar em Definições > Personalização > Memória."
        ),
        "input_schema": {
            "type": "object",
            "required": ["content"],
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "A memória em texto simples, autónoma e específica (não vaga). "
                        "Ex: 'Prefere respostas curtas, sem resumo final.' ou "
                        "'É responsável pelos servidores SVBES* — trata pedidos sobre eles como prioritários.'"
                    ),
                },
            },
        },
    },
    {
        "name": "recall_memory",
        "description": (
            "Consulta memórias já guardadas sobre este utilizador, por semelhança com `query` "
            "— usa quando precisares activamente de contexto que pode já ter sido dado numa "
            "conversa anterior (ex: o utilizador refere algo que parece pressupor contexto já "
            "partilhado, ou estás prestes a perguntar algo que ele já pode ter respondido antes)."
        ),
        "input_schema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "O que procurar, em linguagem natural"},
                "k":     {"type": "integer", "description": "Nº máximo de memórias a devolver (default 3)"},
            },
        },
    },
]

_FATES_TOOL_NAMES = {t["name"] for t in _FATES_TOOLS}


def _tools_for(has_fates_access: bool) -> list[dict]:
    if has_fates_access:
        return TOOL_DEFINITIONS
    return [t for t in TOOL_DEFINITIONS if t["name"] not in _FATES_TOOL_NAMES]


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _db():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


def _ts(row: dict, *keys):
    for k in keys:
        if row.get(k):
            row[k] = str(row[k])
    return row


# ─────────────────────────────────────────────────────────────────────────────
# TOOL FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def _query_problems(status="all", host=None, limit=10):
    conn = _db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where, params = [], []
    if status != "all":
        where.append("status = %s"); params.append(status)
    if host:
        where.append("host ILIKE %s"); params.append(f"%{host}%")
    w   = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT problem_id, host, title, severity, status, alert_count, opened_at, last_seen, duration_s FROM problems {w} ORDER BY opened_at DESC LIMIT %s"
    params.append(limit)
    cur.execute(sql, params)
    rows = [_ts(dict(r), "opened_at", "last_seen") for r in cur.fetchall()]
    conn.close()
    return json.dumps(rows, default=str), f"problems LIMIT {limit}"


def _query_events(host=None, event_type=None, severity=None, since_minutes=60, limit=20):
    conn   = _db()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where  = [f"created_at >= NOW() - INTERVAL '{int(since_minutes)} minutes'"]
    params = []
    if host:       where.append("host ILIKE %s");   params.append(f"%{host}%")
    if event_type: where.append("event_type = %s"); params.append(event_type)
    if severity:   where.append("severity = %s");   params.append(severity)
    sql = f"SELECT host, event_type, severity, summary, created_at FROM events WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    cur.execute(sql, params)
    rows = [_ts(dict(r), "created_at") for r in cur.fetchall()]
    conn.close()
    return json.dumps(rows, default=str), f"events (last {since_minutes}m)"


def _query_agents(active_only=False):
    conn = _db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if active_only:
        sql = "SELECT host, TO_TIMESTAMP(MAX(snapshot_time)) AS last_seen FROM snapshots WHERE snapshot_time >= EXTRACT(EPOCH FROM NOW() - INTERVAL '5 minutes') GROUP BY host ORDER BY last_seen DESC"
    else:
        sql = "SELECT host, TO_TIMESTAMP(MAX(snapshot_time)) AS last_seen FROM snapshots GROUP BY host ORDER BY last_seen DESC"
    cur.execute(sql)
    rows = [_ts(dict(r), "last_seen") for r in cur.fetchall()]
    conn.close()
    return json.dumps(rows, default=str), sql


def _query_metrics(host, since_minutes=60):
    conn = _db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT snapshot_time, cpu_percent, memory_percent, disk_percent
             FROM snapshots
            WHERE host ILIKE %s
              AND snapshot_time >= EXTRACT(EPOCH FROM NOW() - INTERVAL %s)
            ORDER BY snapshot_time DESC LIMIT 60""",
        (f"%{host}%", f"{int(since_minutes)} minutes"),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return json.dumps(rows, default=str), f"snapshots WHERE host ILIKE '%{host}%'"


def _query_alerts(host=None, severity=None, since_minutes=60, limit=20):
    conn   = _db()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where  = [f"created_at >= NOW() - INTERVAL '{int(since_minutes)} minutes'"]
    params = []
    if host:     where.append("host ILIKE %s"); params.append(f"%{host}%")
    if severity: where.append("severity = %s"); params.append(severity)
    sql = f"SELECT host, title, severity, created_at FROM alerts WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    cur.execute(sql, params)
    rows = [_ts(dict(r), "created_at") for r in cur.fetchall()]
    conn.close()
    return json.dumps(rows, default=str), f"alerts (last {since_minutes}m)"


# Limite de caracteres por stream (stdout/stderr) do agentless_run.
# Evita estourar o limite de tokens do prompt quando um script devolve
# output muito grande (ex: dump de ficheiros, logs extensos, binários em base64).
_MAX_OUTPUT_CHARS = 50_000


def _truncate_output(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if not text or len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n\n[...output truncado — {omitted} caracteres omitidos...]"


_AD_FILETIME_ATTRS = ("lastLogonTimestamp", "lastLogon", "pwdLastSet", "accountExpires")
_AD_DEFAULT_ATTRIBUTES = ["name", "sAMAccountName", "displayName", "operatingSystem",
                           "distinguishedName", "lastLogonTimestamp", "description"]
_AD_QUERY_TIMEOUT = 30


def _ps_str_literal(s: str) -> str:
    """Escapa uma string para um literal PowerShell de aspas simples."""
    return "'" + s.replace("'", "''") + "'"


def _filetime_to_iso(value):
    """
    Converte um valor Windows FileTime (100ns desde 1601-01-01) para ISO 8601.
    Devolve o valor original se não for um FileTime válido (ex: 0, "nunca expira").
    """
    try:
        ticks = int(value)
    except (TypeError, ValueError):
        return value
    if ticks <= 0 or ticks >= 0x7FFFFFFFFFFFFFFF:
        return value
    import datetime
    epoch = datetime.datetime(1601, 1, 1)
    try:
        return (epoch + datetime.timedelta(microseconds=ticks / 10)).isoformat()
    except OverflowError:
        return value


def _query_ad(filter, attributes=None, limit=0, compact=True):
    """
    Consulta o AD via ADSI (LDAP), correndo localmente onde o jarvis_center
    está — sem host remoto, sem WinRM, sem Domain Controller explícito.
    Devolve uma lista de objectos com os atributos pedidos.

    compact=False força sempre a forma completa {"results": [...], "count": N}
    mesmo acima de _COMPACT_THRESHOLD — usado pelo runner query_ad do Lachesis
    (lachesis_flow_executor.py), que precisa da lista real de máquinas para
    alimentar agentless_bulk_script.hosts_from, não de um resumo para LLM.
    """
    if isinstance(attributes, str):
        attributes = [a.strip() for a in attributes.split(",")]
    attrs = [a for a in (attributes or _AD_DEFAULT_ATTRIBUTES) if isinstance(a, str) and a.strip()]
    if not attrs:
        attrs = _AD_DEFAULT_ATTRIBUTES
    size_limit = max(0, int(limit))  # 0 = sem limite (FindAll pagina e devolve tudo)

    filter_lit = _ps_str_literal(filter)
    attrs_lit  = ",".join(_ps_str_literal(a) for a in attrs)

    # NOTA: a conversão de FileTime (lastLogonTimestamp, etc.) é feita aqui em
    # Python, não em PowerShell — combinar DirectorySearcher/FindAll com
    # [DateTime]::FromFileTime na mesma linha de comando é bloqueado pelo
    # EDR do servidor (assinatura de ferramentas de reconhecimento de AD).
    script = f"""
$__attrs = @({attrs_lit})
$s = New-Object System.DirectoryServices.DirectorySearcher
$s.Filter = {filter_lit}
$s.PageSize = 1000
$s.SizeLimit = {size_limit}
foreach ($a in $__attrs) {{ $s.PropertiesToLoad.Add($a) | Out-Null }}
$results = $s.FindAll()
$out = @()
foreach ($r in $results) {{
    $obj = [ordered]@{{}}
    foreach ($a in $__attrs) {{
        $obj[$a] = $r.Properties[$a][0]
    }}
    $out += [PSCustomObject]$obj
}}
ConvertTo-Json -InputObject @($out) -Compress -Depth 5
""".strip()

    try:
        r = subprocess.run(
            ["powershell.exe", "-NonInteractive", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=_AD_QUERY_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Consulta AD excedeu {_AD_QUERY_TIMEOUT}s.", "filter": filter}), ""

    out = (r.stdout or "").strip()
    if not out:
        err = _truncate_output((r.stderr or "").strip())
        return json.dumps({"results": [], "count": 0, "error": err or "Sem resultados."}), f"query_ad filter={filter}"

    try:
        data = json.loads(out)
    except Exception as e:
        return json.dumps({"error": f"Resposta inválida do AD: {e}", "raw": _truncate_output(out)}), ""

    if isinstance(data, dict):
        data = [data]

    for item in data:
        if not isinstance(item, dict):
            continue
        for a in _AD_FILETIME_ATTRS:
            if a in item and item[a] is not None:
                item[a] = _filetime_to_iso(item[a])

    _COMPACT_THRESHOLD = 150

    if not compact or len(data) <= _COMPACT_THRESHOLD:
        return json.dumps({"results": data, "count": len(data)}, default=str), f"query_ad filter={filter}"

    names = [str(item.get("name") or item.get("sAMAccountName") or "?") for item in data if isinstance(item, dict)]
    by_ou: dict[str, int] = {}
    by_os: dict[str, int] = {}
    disabled_count = 0
    active_count = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        dn = str(item.get("distinguishedName") or "")
        parts = [p.strip() for p in dn.split(",") if p.strip().upper().startswith("OU=")]
        ou = parts[0] if parts else "Sem OU"
        by_ou[ou] = by_ou.get(ou, 0) + 1
        os_name = str(item.get("operatingSystem") or "Desconhecido")
        by_os[os_name] = by_os.get(os_name, 0) + 1
        if "disabled" in dn.lower():
            disabled_count += 1
        lt = item.get("lastLogonTimestamp")
        if lt and isinstance(lt, str) and lt > "2024":
            active_count += 1

    return json.dumps({
        "count": len(data),
        "names": names,
        "summary": {
            "by_ou": dict(sorted(by_ou.items(), key=lambda x: -x[1])),
            "by_os": dict(sorted(by_os.items(), key=lambda x: -x[1])),
            "in_disabled_ou": disabled_count,
            "active_since_2024": active_count,
            "not_disabled_and_active": active_count - disabled_count,
        },
        "note": f"Resultado compactado ({len(data)} registos). Lista completa de nomes em 'names'. Agrupamentos em 'summary'.",
    }, default=str), f"query_ad filter={filter}"


_agentless_path_added = False

def _ensure_agentless_path():
    global _agentless_path_added
    if not _agentless_path_added:
        import sys as _sys
        parent = os.path.dirname(os.path.dirname(__file__))
        if parent not in _sys.path:
            _sys.path.insert(0, parent)
        _agentless_path_added = True

def _agentless_profile_hint(host: str) -> dict | None:
    """Consulta o perfil agregado da máquina (agentless_machine_profile) —
    o que já se sabe dela por execuções anteriores. Nunca deve bloquear nem
    falhar a execução em si: qualquer erro aqui é engolido e devolve-se None,
    como se não houvesse perfil conhecido ainda."""
    try:
        _ensure_agentless_path()
        from agentless.agentless_store import AgentlessStore
        profile = AgentlessStore().get_machine_profile(host)
    except Exception:
        return None
    if not profile:
        return None
    known_state = profile.get("known_state") or {}
    last_session = known_state.get("last_session")
    hint = {
        "total_executions":     profile.get("total_executions"),
        "consecutive_failures": profile.get("consecutive_failures"),
        "machine_type_preferred": profile.get("machine_type_preferred"),
        "known_issues":         profile.get("known_issues") or {},
        "last_investigation":   last_session,
    }
    # Só vale a pena mostrar ao modelo se houver sinal accionável — perfil
    # limpo (sem falhas seguidas, sem issues, sem sessão anterior conhecida)
    # não acrescenta nada.
    if not hint["consecutive_failures"] and not hint["known_issues"] and not last_session:
        return None
    return hint


def _agentless_run(host, script, machine_type="server", timeout_s=60, reason=""):
    try:
        _ensure_agentless_path()
        from agentless.execution_broker import get_broker

        profile_hint = _agentless_profile_hint(host)
        broker = get_broker()
        job_id = broker.submit(
            host=host,
            script=script,
            machine_type=machine_type,
            timeout_s=timeout_s,
            # multi_step=False aqui: o broker decide sozinho se vale a pena
            # bootstrap, com base no padrão real de chamadas a este host
            # (só activa a partir da 2ª chamada na mesma janela curta).
            multi_step=False,
            reason=reason,
        )
        result = broker.wait_result(job_id, timeout=timeout_s + 15)

        if result is None:
            return json.dumps({"error": "Timeout no broker", "host": host}), ""

        return json.dumps({
            "stdout":    _truncate_output(result.get("stdout", "")),
            "stderr":    _truncate_output(result.get("stderr", "")),
            "exit_code": result.get("exit_code", -1),
            "success":   result.get("ok", False),
            "warning":   result.get("warning"),
            "_transport": result.get("_transport"),
            "_beacon_active": result.get("_transport", "").startswith("http"),
            "_known_profile": profile_hint,
        }), f"agentless_run host={host}"
    except Exception as e:
        return json.dumps({"error": str(e), "host": host}), ""


def _agentless_bulk_profile_hints(hosts: list) -> dict:
    """Versão em lote de _agentless_profile_hint — uma query para todos os
    hosts do agentless_run_bulk. Nunca bloqueia/falha a execução em si."""
    try:
        _ensure_agentless_path()
        from agentless.agentless_store import AgentlessStore
        profiles = AgentlessStore().get_machine_profiles(hosts)
    except Exception:
        return {}
    hints = {}
    for host, profile in profiles.items():
        if profile.get("consecutive_failures") or profile.get("known_issues"):
            hints[host] = {
                "consecutive_failures":   profile.get("consecutive_failures"),
                "machine_type_preferred": profile.get("machine_type_preferred"),
                "known_issues":           profile.get("known_issues") or {},
            }
    return hints


def _agentless_run_bulk(hosts, script, machine_type="server", timeout_s=120, reason=""):
    if not hosts:
        return json.dumps({"error": "Lista de hosts vazia."}), ""
    if len(hosts) > _BULK_MAX_HOSTS:
        return json.dumps({
            "error": (
                f"Lista de {len(hosts)} hosts excede o limite de {_BULK_MAX_HOSTS} "
                f"por chamada. Divide em lotes de até {_BULK_MAX_HOSTS} hosts e chama "
                f"agentless_run_bulk uma vez por lote, sequencialmente, agregando os "
                f"resultados de todos os lotes no fim."
            ),
            "total_hosts": len(hosts),
            "max_hosts_per_call": _BULK_MAX_HOSTS,
        }), ""
    try:
        _ensure_agentless_path()
        from agentless.execution_broker import get_broker

        profile_hints = _agentless_bulk_profile_hints(hosts)
        broker = get_broker()
        job_id = broker.submit_bulk(
            hosts=hosts,
            script=script,
            machine_type=machine_type,
            timeout_s=timeout_s,
            reason=reason,
        )
        result = broker.wait_result(job_id, timeout=timeout_s + 30)

        if result is None:
            return json.dumps({"error": "Timeout no broker", "hosts": hosts}), ""

        results = {
            h: {
                "stdout":    _truncate_output(r.get("stdout", "")),
                "stderr":    _truncate_output(r.get("stderr", "")),
                "exit_code": r.get("exit_code", -1),
                "success":   r.get("ok", False),
            }
            for h, r in (result.get("results") or {}).items()
        }
        return json.dumps({
            "results":     results,
            "unreachable": result.get("unreachable", []),
            "total_hosts": len(hosts),
            "responded":   len(results),
            "warning":     result.get("warning"),
            "_transport":  result.get("_transport"),
            "_known_profiles": profile_hints,
        }), f"agentless_run_bulk hosts={len(hosts)}"
    except Exception as e:
        return json.dumps({"error": str(e), "hosts": hosts}), ""


def _query_agentless_history(host=None, since_hours=None, limit=20):
    try:
        _ensure_agentless_path()
        from agentless.agentless_store import AgentlessStore
        rows = AgentlessStore().query_execution_history(
            host=host, since_hours=since_hours, limit=limit or 20,
        )
        for r in rows:
            if r.get("executed_at") and not isinstance(r["executed_at"], str):
                r["executed_at"] = str(r["executed_at"])
        return json.dumps({"executions": rows, "count": len(rows)}, default=str), \
            f"query_agentless_history host={host} since_hours={since_hours}"
    except Exception as e:
        return json.dumps({"error": str(e), "executions": []}), ""


_MAX_INTEGRATION_RESPONSE = 50_000


def _compact_integration_response(raw: str) -> str:
    """Processa respostas de integrações para serem compactas mas completas.

    JSON estruturado (ex: Splunk, Zabbix) é processado server-side para extrair
    apenas campos relevantes. O LLM recebe dados completos mas compactos.
    """
    if not raw or len(raw) <= _MAX_INTEGRATION_RESPONSE:
        return raw

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw[:_MAX_INTEGRATION_RESPONSE] + f"\n[...truncado, {len(raw)} chars total]"

    _KEY_FIELDS = ("name", "title", "id", "host", "severity", "status", "state",
                   "author", "search", "cron_schedule", "disabled", "is_scheduled",
                   "totalEventCount", "currentDBSizeMB", "maxTotalDataSizeMB",
                   "minTime", "maxTime", "datatype", "frozenTimePeriodInSecs")
    _CONTENT_FIELDS = ("disabled", "search", "cron_schedule", "is_scheduled",
                       "alert.severity", "actions", "alert_type",
                       "totalEventCount", "currentDBSizeMB", "maxTotalDataSizeMB",
                       "minTime", "maxTime", "datatype")

    if isinstance(data, dict):
        entries = data.get("entry") or data.get("entries") or data.get("results") or data.get("data")
        if isinstance(entries, list) and len(entries) > 0:
            compact_entries = []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                ce = {}
                for k in _KEY_FIELDS:
                    if k in e:
                        ce[k] = e[k]
                content = e.get("content") or {}
                if isinstance(content, dict):
                    for k in _CONTENT_FIELDS:
                        if k in content and k not in ce:
                            ce[k] = content[k]
                if not ce:
                    ce = {k: v for k, v in list(e.items())[:6] if k != "content" and k != "links"}
                compact_entries.append(ce)

            result = {"total": len(entries), "entries": compact_entries}
            compact_json = json.dumps(result, ensure_ascii=False)
            if len(compact_json) <= _MAX_INTEGRATION_RESPONSE:
                return compact_json
            even_more = [{"name": e.get("name", "?"), "totalEventCount": e.get("totalEventCount"), "currentDBSizeMB": e.get("currentDBSizeMB")} for e in compact_entries]
            return json.dumps({"total": len(entries), "entries": even_more}, ensure_ascii=False)

    fallback = json.dumps(data, ensure_ascii=False)
    if len(fallback) <= _MAX_INTEGRATION_RESPONSE:
        return fallback
    return fallback[:_MAX_INTEGRATION_RESPONSE] + f"\n[...truncado, {len(fallback)} chars total]"


def _recover_action_from_history(messages: list, expired_token: str) -> dict | None:
    """Percorre a conversa de trás para a frente e tenta encontrar o card
    jarvis-approval cujo token coincide com o expirado. Se encontrar,
    reconstrói a acção a partir do JSON do card."""
    import re as _re
    _card_pattern = _re.compile(r"```jarvis-approval\s*\n(.+?)\n```", _re.DOTALL)
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        if not isinstance(content, str):
            continue
        for match in _card_pattern.finditer(content):
            try:
                card = json.loads(match.group(1))
                if card.get("token") == expired_token:
                    if card.get("tool") == "propose_infra_knowledge":
                        return {
                            "tool":        "propose_infra_knowledge",
                            "title":       card.get("title", ""),
                            "instruction": card.get("instruction", ""),
                            "category":    card.get("category"),
                            "version":     card.get("version"),
                            "evidence":    card.get("evidence"),
                            "scope_type":  card.get("scope_type", "global"),
                            "scope_value": card.get("scope_value"),
                        }
                    action = {
                        "tool":         card.get("tool", "agentless_run"),
                        "machine_type": card.get("machine_type", "server"),
                        "script":       card.get("script", ""),
                        "timeout_s":    card.get("timeout_s"),
                    }
                    if card.get("hosts"):
                        action["hosts"] = card["hosts"]
                    elif card.get("host"):
                        action["host"] = card["host"]
                    return action
            except (json.JSONDecodeError, KeyError):
                continue
    return None


def _manage_task(action="list", name=None, instruction=None, schedule_description=None):
    from lachesis.lachesis_store import LachesisStore
    from lachesis.lachesis_tasks_llm import parse_schedule_description

    store = LachesisStore()

    if action == "list":
        tasks = store.list_tasks()
        compact = [
            {"id": t["id"], "name": t["name"], "enabled": t["enabled"],
             "schedule": t.get("schedule"), "last_status": t.get("last_status"),
             "next_run_at": str(t["next_run_at"]) if t.get("next_run_at") else None}
            for t in tasks
        ]
        return json.dumps({"tasks": compact, "count": len(compact)}, default=str), "lachesis list"

    if action == "create":
        if not name or not instruction or not schedule_description:
            return json.dumps({"error": "name, instruction e schedule_description são obrigatórios para create."}), ""

        parsed = parse_schedule_description(schedule_description)
        schedule = parsed["schedule"]
        task = store.create_task({
            "name": name,
            "instruction": instruction,
            "task_type": "agentless",
            "schedule": schedule,
            "enabled": True,
        })
        return json.dumps({
            "created": True,
            "task_id": task["id"],
            "name": task["name"],
            "schedule_summary": parsed["summary"],
            "next_run_at": parsed.get("next_run_at"),
            "parse_warning": parsed.get("parse_warning"),
        }, default=str), f"lachesis create {name}"

    return json.dumps({"error": f"Acção desconhecida: {action}"}), ""


def _persist_infra_knowledge(title, instruction, category=None, version=None, evidence=None,
                              scope_type="global", scope_value=None):
    """Grava um facto/lição aprovado pelo utilizador na Casa do Conhecimento
    (lachesis_behaviors, kind='infra_fact'). Só é chamado depois de
    JARVIS:CONFIRMO — antes disso vive só em Redis (curto prazo).

    scope_type='global' — aplica-se a toda a infraestrutura, vai sempre no
    prompt. 'host'/'target' — só sobre scope_value (hostname/nome do alvo),
    só é injectado quando esse host/alvo é mencionado na conversa (ver
    get_behaviors_prompt_suffix em lachesis_store.py)."""
    from lachesis.lachesis_store import LachesisStore

    if scope_type in ("host", "target") and not scope_value:
        return json.dumps({"error": "scope_value é obrigatório quando scope_type é 'host' ou 'target'."}), ""

    row = LachesisStore().create_behavior({
        "title":       title,
        "instruction": instruction,
        "kind":        "infra_fact",
        "category":    category,
        "version":     version,
        "source":      "agentless_observation",
        "evidence":    evidence,
        "active":      True,
        "created_by":  "jarvis-auto",
        "scope_type":  scope_type,
        "scope_value": scope_value,
    })
    return json.dumps({
        "saved": True,
        "id": row["id"],
        "title": row["title"],
        "category": row.get("category"),
        "scope_type": row.get("scope_type"),
        "scope_value": row.get("scope_value"),
    }, default=str), f"propose_infra_knowledge {title}"


_OPENWEBUI_SESSION_SECRET = os.getenv("OPENWEBUI_SESSION_SECRET", "")


def _mint_webui_session_token(user_id: str, ttl_s: int = 300) -> str | None:
    """Emite um JWT no formato de SESSÃO do Open WebUI: {"id": user_id, "exp": ...},
    HS256 assinado com WEBUI_SECRET_KEY (aqui: OPENWEBUI_SESSION_SECRET).

    NÃO é o mesmo segredo do X-OpenWebUI-User-Jwt recebido — esse é assinado
    com FORWARD_USER_INFO_HEADER_JWT_SECRET (só serve para identificar o
    utilizador ao jarvis_center) e é REJEITADO pelos endpoints normais do Open
    WebUI (get_verified_user/decode_token usam WEBUI_SECRET_KEY). Descoberto a
    testar: reencaminhar o JWT recebido tal-e-qual como Bearer falhava sempre
    a autenticação em /api/v1/memories/*.

    Sem 'jti' de propósito — decode_token só invoca a verificação de revogação
    de sessão (is_valid_token) quando esse campo está presente; omiti-lo evita
    depender do mecanismo interno de sessões do Open WebUI para um token que
    o jarvis_center emite por si, de curta duração (5 min)."""
    if not _OPENWEBUI_SESSION_SECRET or not user_id:
        return None
    header  = {"alg": "HS256", "typ": "JWT"}
    payload = {"id": user_id, "exp": int(time.time()) + ttl_s}
    h_b64 = base64.urlsafe_b64encode(json.dumps(header,  separators=(",", ":")).encode()).rstrip(b"=")
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signing_input = h_b64 + b"." + p_b64
    sig   = hmac.new(_OPENWEBUI_SESSION_SECRET.encode(), signing_input, hashlib.sha256).digest()
    s_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
    return (signing_input + b"." + s_b64).decode()


def _resolve_webui_user_id(user_jwt: str | None) -> str | None:
    """Extrai o id de utilizador do X-OpenWebUI-User-Jwt (FORWARD_USER_INFO_HEADER_JWT),
    verificando a assinatura com OPENWEBUI_JWT_SECRET antes de confiar no claim."""
    secret = os.getenv("OPENWEBUI_JWT_SECRET", "")
    if not user_jwt or not secret:
        return None
    payload = verify_openwebui_jwt(user_jwt, secret)
    if not payload:
        return None
    return payload.get("sub") or payload.get("id")


def _memory_session_token(user_jwt: str | None) -> tuple[str | None, str | None]:
    """Devolve (motivo_de_erro, token_de_sessão_pronto_a_usar) — exactamente um
    dos dois é None."""
    if not _OPENWEBUI_BASE_URL:
        return "OPENWEBUI_BASE_URL não está configurado no jarvis_center.", None
    if not _OPENWEBUI_SESSION_SECRET:
        return "OPENWEBUI_SESSION_SECRET não está configurado no jarvis_center.", None
    user_id = _resolve_webui_user_id(user_jwt)
    if not user_id:
        return "Sem JWT de utilizador válido nesta conversa (só disponível no canal web, com OPENWEBUI_JWT_SECRET configurado).", None
    token = _mint_webui_session_token(user_id)
    if not token:
        return "Falha ao gerar token de sessão para a memória.", None
    return None, token


def _save_memory(content: str, user_jwt: str | None = None) -> tuple[str, str]:
    """Grava uma memória pessoal/de contexto sobre ESTE utilizador — reaproveita
    a funcionalidade nativa de Memória do Open WebUI (embeddings + busca por
    similaridade), em vez de criar armazenamento novo. Ao contrário da Casa do
    Conhecimento (propose_infra_knowledge, sobre infraestrutura, com aprovação
    humana), isto é de baixo atrito — tal como o ChatGPT/Claude, fica gravado
    de imediato e o utilizador pode rever/apagar em Definições > Personalização."""
    reason, session_token = _memory_session_token(user_jwt)
    if reason:
        return json.dumps({"saved": False, "error": reason}), ""
    try:
        r = requests.post(
            f"{_OPENWEBUI_BASE_URL}/api/v1/memories/add",
            json={"content": content},
            headers={"Authorization": f"Bearer {session_token}"},
            timeout=10,
        )
        r.raise_for_status()
        return json.dumps({"saved": True, "content": content}), f"save_memory"
    except Exception as e:
        return json.dumps({"saved": False, "error": str(e)}), ""


def _recall_memory(query: str, k: int = 3, user_jwt: str | None = None) -> tuple[str, str]:
    """Consulta memórias já guardadas deste utilizador, por similaridade
    semântica com `query` — usa a mesma busca vetorial que o Open WebUI já
    usa para injecção automática de memória."""
    reason, session_token = _memory_session_token(user_jwt)
    if reason:
        return json.dumps({"memories": [], "error": reason}), ""
    try:
        r = requests.post(
            f"{_OPENWEBUI_BASE_URL}/api/v1/memories/query",
            json={"content": query, "k": max(1, min(k, 10))},
            headers={"Authorization": f"Bearer {session_token}"},
            timeout=10,
        )
        if r.status_code == 404:
            return json.dumps({"memories": [], "note": "Sem memórias guardadas ainda para este utilizador."}), ""
        r.raise_for_status()
        data = r.json()
        docs = (data.get("documents") or [[]])[0]
        return json.dumps({"memories": docs}), f"recall_memory query={query}"
    except Exception as e:
        return json.dumps({"memories": [], "error": str(e)}), ""


def _save_exploration_tip(scope_target_id, title, summary, priority, detail=None, supersedes_tip_id=None):
    """Guarda a conclusão de uma investigação agentless de um tip do Explorer
    (Scope Collector / Fates Engine). Escrita de dados simples — não precisa
    de aprovação (não executa nada, como manage_task/call_integration_tool)."""
    from scope_collector.scope_store import ScopeStore

    store = ScopeStore()
    target = store.get_target(int(scope_target_id))
    if not target:
        return json.dumps({"error": f"scope_target {scope_target_id} não encontrado."}), ""

    tip = store.create_tip({
        "scope_target_id": int(scope_target_id),
        "category":        target.get("category") or "server",
        "title":           title,
        "summary":         summary,
        "detail":          detail,
        "priority":        priority,
        "evidence":        {"mode": "agentless_investigation"},
    })

    if supersedes_tip_id:
        store.update_tip_status(int(supersedes_tip_id), "resolved")

    return json.dumps({
        "saved": True,
        "tip_id": tip["id"],
        "scope_target_id": int(scope_target_id),
        "title": tip["title"],
    }, default=str), f"save_exploration_tip {title}"


def _call_integration_tool(integration_name, tool_name, params=None):
    try:
        from clotho.clotho_store import ClothoStore
        from clotho.clotho_tester import build_tool_request, execute_test_request

        store = ClothoStore()
        match = next(
            (i for i in store.list_integrations() if i["name"].lower() == integration_name.lower()),
            None,
        )
        if not match:
            return json.dumps({"error": f"Integração '{integration_name}' não encontrada."}), ""

        integ = store.get_integration(match["id"])
        catalog = integ.get("tools") or {}
        tool = (catalog.get("tools") or {}).get(tool_name)
        if not tool:
            return json.dumps({"error": f"Tool '{tool_name}' não encontrada na integração '{integration_name}'."}), ""

        body_encoding = tool.get("body_encoding")
        effective_params = dict(params or {})
        tool_body = str((tool.get("request") or {}).get("body") or "")
        if "{{params.earliest_time}}" in tool_body and "earliest_time" not in effective_params:
            effective_params["earliest_time"] = "-24h"
        if "{{params.latest_time}}" in tool_body and "latest_time" not in effective_params:
            effective_params["latest_time"] = "now"
        resolved_request = build_tool_request(tool["request"], effective_params)
        response = execute_test_request(integ, resolved_request, body_encoding=body_encoding)

        raw_response = response.get("body_snippet") or ""
        processed_response = _compact_integration_response(raw_response)

        result: dict = {
            "success":     response.get("ok", False),
            "status_code": response.get("status_code"),
            "response":    processed_response,
            "error":       response.get("error"),
        }

        return json.dumps(result), f"call_integration_tool {integration_name}.{tool_name}"
    except Exception as e:
        return json.dumps({"error": str(e)}), ""


def _query_database(database_name, sql):
    try:
        from clotho.clotho_database_store import ClothoDatabaseStore
        from clotho.clotho_database_engine import run_query

        store = ClothoDatabaseStore()
        db = store.get_database_by_name(database_name)
        if not db:
            return json.dumps({"error": f"Base de dados '{database_name}' não encontrada."}), ""

        result = run_query(db, sql)
        return json.dumps(result, default=str), f"query_database {database_name}: {sql}"
    except Exception as e:
        return json.dumps({"error": str(e)}), ""


def _query_reasoning(host="", limit=5):
    conn   = _db()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where, params = [], []
    if host: where.append("host ILIKE %s"); params.append(f"%{host}%")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(min(int(limit), 10))
    cur.execute(f"SELECT host, summary, details, created_at FROM reasoning {w} ORDER BY created_at DESC LIMIT %s", params)
    rows = [_ts(dict(r), "created_at") for r in cur.fetchall()]
    conn.close()
    return json.dumps({"reasoning": rows, "count": len(rows)}, default=str), f"reasoning {w}"


def _query_machine_profile(host=""):
    conn   = _db()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where, params = ["1=1"], []
    if host: where.append("host ILIKE %s"); params.append(f"%{host}%")
    cur.execute(f"SELECT host, profile, setup_at, updated_at FROM server_profiles WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT 20", params)
    rows = [_ts(dict(r), "setup_at", "updated_at") for r in cur.fetchall()]
    conn.close()
    if not rows:
        return json.dumps({"message": "Sem perfis de máquina gerados ainda.", "count": 0}), ""
    return json.dumps({"profiles": rows, "count": len(rows)}, default=str), "server_profiles"


def _query_user_profiles(host="", username=""):
    conn   = _db()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where, params = [], []
    if host:     where.append("host ILIKE %s");     params.append(f"%{host}%")
    if username: where.append("username ILIKE %s"); params.append(f"%{username}%")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    cur.execute(
        f"""SELECT host, username, is_human, risk_score, risk_level, work_type,
                   primary_apps, typical_hours, active_days, after_hours_activity,
                   weekend_activity, privilege_abuse_detected, behavioral_anomalies, summary, created_at
              FROM user_profiles {w} ORDER BY risk_score DESC LIMIT 30""",
        params,
    )
    rows = [_ts(dict(r), "created_at") for r in cur.fetchall()]
    conn.close()
    return json.dumps({"user_profiles": rows, "count": len(rows)}, default=str), f"user_profiles {w}"


def _query_ueba(host="", username="", since_minutes=10080):
    conn   = _db()
    cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where  = [f"analyzed_at >= NOW() - INTERVAL '{int(since_minutes)} minutes'"]
    params = []
    if host:     where.append("host ILIKE %s");     params.append(f"%{host}%")
    if username: where.append("username ILIKE %s"); params.append(f"%{username}%")
    cur.execute(
        f"""SELECT host, username, analysis_type, compliance_score, verdict,
                   summary, anomalies, events_analyzed, analyzed_at
              FROM ueba_analyses WHERE {' AND '.join(where)} ORDER BY analyzed_at DESC LIMIT 30""",
        params,
    )
    rows = [_ts(dict(r), "analyzed_at") for r in cur.fetchall()]
    conn.close()
    return json.dumps({"ueba_analyses": rows, "count": len(rows)}, default=str), "ueba_analyses"


def _query_security_events(host="", since_minutes=1440, include_ueba=True):
    conn = _db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        security_types = (
            "discovery_change", "network_throughput_spike", "service_stopped",
            "login_failed", "privilege_escalation", "unauthorized_access",
            "new_listening_port", "process_anomaly", "lateral_movement",
        )
        placeholders = ",".join(["%s"] * len(security_types))
        ev_where = [f"created_at >= NOW() - INTERVAL '{int(since_minutes)} minutes'",
                    f"event_type IN ({placeholders})"]
        ev_params = list(security_types)
        if host:
            ev_where.append("host ILIKE %s"); ev_params.append(f"%{host}%")
        cur.execute(
            f"SELECT host, event_type, severity, summary, payload, created_at FROM events WHERE {' AND '.join(ev_where)} ORDER BY created_at DESC LIMIT 50",
            ev_params,
        )
        events = [_ts(dict(r), "created_at") for r in cur.fetchall()]

        ueba_entries = []
        if include_ueba:
            ueba_where = [f"analyzed_at >= NOW() - INTERVAL '{min(int(since_minutes) * 7, 10080)} minutes'",
                          "verdict IN ('suspicious','high_risk')"]
            ueba_params = []
            if host:
                ueba_where.append("host ILIKE %s"); ueba_params.append(f"%{host}%")
            cur.execute(
                f"""SELECT host, username, compliance_score, verdict, summary, anomalies, events_analyzed, analyzed_at
                      FROM ueba_analyses WHERE {' AND '.join(ueba_where)} ORDER BY compliance_score ASC LIMIT 25""",
                ueba_params,
            )
            ueba_entries = [_ts(dict(r), "analyzed_at") for r in cur.fetchall()]

        unique_users = list({e["username"] for e in ueba_entries if e.get("username")})
        user_context = {}
        if unique_users:
            cur.execute(
                f"SELECT username, is_human, risk_score, work_type, typical_hours, active_days, after_hours_activity, summary FROM user_profiles WHERE username IN ({','.join(['%s']*len(unique_users))})",
                unique_users,
            )
            for r in cur.fetchall():
                d = dict(r)
                user_context[d["username"]] = d

        affected_hosts = list({e["host"] for e in events} | {e["host"] for e in ueba_entries})
        machine_context = {}
        if affected_hosts:
            cur.execute(
                f"SELECT host, profile FROM server_profiles WHERE host IN ({','.join(['%s']*len(affected_hosts))})",
                affected_hosts,
            )
            for r in cur.fetchall():
                machine_context[r["host"]] = r["profile"]

        result = {
            "security_events":  events,
            "ueba_analyses":    ueba_entries,
            "user_profiles":    user_context,
            "machine_profiles": machine_context,
            "summary": {
                "events_count": len(events),
                "ueba_count":   len(ueba_entries),
                "users_flagged": len(unique_users),
                "hosts_affected": len(affected_hosts),
            },
        }
        conn.close()
        return json.dumps(result, default=str), "security_events aggregate"
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        conn.close()
        return json.dumps({"error": str(e)}), ""


def _analyze_time_pattern(host, days_back=14):
    conn = _db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """SELECT event_type, EXTRACT(HOUR FROM created_at) AS hour,
                      EXTRACT(DOW FROM created_at) AS dow,
                      COUNT(*) AS occurrences
                 FROM events
                WHERE host ILIKE %s
                  AND created_at >= NOW() - INTERVAL %s
                GROUP BY event_type, hour, dow
                ORDER BY occurrences DESC LIMIT 50""",
            (f"%{host}%", f"{int(days_back)} days"),
        )
        rows  = [dict(r) for r in cur.fetchall()]
        hours = {}
        for r in rows:
            h = int(r["hour"])
            hours[h] = hours.get(h, 0) + int(r["occurrences"])
        peak_hour = max(hours, key=hours.get) if hours else None
        conn.close()
        return json.dumps({"patterns": rows, "peak_hour": peak_hour, "hours_summary": hours}, default=str), "time_pattern"
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        conn.close()
        return json.dumps({"error": str(e)}), ""


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCH
# ─────────────────────────────────────────────────────────────────────────────

_DISPATCH = {
    "query_problems":       _query_problems,
    "query_events":         _query_events,
    "query_agents":         _query_agents,
    "query_metrics":        _query_metrics,
    "query_alerts":         _query_alerts,
    "agentless_run":        _agentless_run,
    "agentless_run_bulk":   _agentless_run_bulk,
    "query_ad":             _query_ad,
    "query_agentless_history": _query_agentless_history,
    "query_reasoning":      _query_reasoning,
    "query_machine_profile":_query_machine_profile,
    "query_user_profiles":  _query_user_profiles,
    "query_ueba":           _query_ueba,
    "query_security_events":_query_security_events,
    "analyze_time_pattern": _analyze_time_pattern,
    "call_integration_tool": _call_integration_tool,
    "query_database":        _query_database,
    "manage_task":           _manage_task,
    "propose_infra_knowledge": _persist_infra_knowledge,
    "save_exploration_tip":  _save_exploration_tip,
    "save_memory":           _save_memory,
    "recall_memory":         _recall_memory,
}


# Tools que precisam do JWT do utilizador (para chamar de volta a API do
# Open WebUI em nome dele) — passado explicitamente por execute_tool, nunca
# por thread-local (run_jarvis_loop_stream é um gerador que o Starlette pode
# retomar em threads diferentes do pool a cada yield).
_JWT_AWARE_TOOLS = {"save_memory", "recall_memory"}

# Tools que precisam de saber PORQUÊ foram chamadas — para agrupar execuções
# agentless na mesma sessão de investigação (ver agentless_investigation_sessions
# em agentless_store.py). "reason" é sempre a última mensagem do utilizador,
# só usada quando abre uma sessão nova; chamadas seguintes dentro da janela de
# inactividade ignoram-no e mantêm o motivo original da sessão.
_REASON_AWARE_TOOLS = {"agentless_run", "agentless_run_bulk"}


def execute_tool(name: str, args: dict, user_jwt: str | None = None, reason: str = "") -> tuple[str, str]:
    """Execute a Jarvis tool. Returns (result_json, sql_summary)."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}"}), ""
    try:
        if name in _JWT_AWARE_TOOLS:
            return fn(user_jwt=user_jwt, **args)
        if name in _REASON_AWARE_TOOLS:
            return fn(reason=reason, **args)
        return fn(**args)
    except Exception as e:
        return json.dumps({"error": str(e)}), ""


# ─────────────────────────────────────────────────────────────────────────────
# JARVIS LOOP  —  multi-turn reasoning
# ─────────────────────────────────────────────────────────────────────────────

def run_jarvis_loop(
    messages: list,
    max_tokens: int = 4096,
    has_fates_access: bool = True,
    channel: str = "web",
    chat_id: str | None = None,
    user_jwt: str | None = None,
) -> str:
    """
    Executa o loop completo de raciocínio Jarvis (versão não-streaming):
      LLM → tool calls → execute → LLM → ... → resposta final

    user_jwt: JWT do Open WebUI (X-OpenWebUI-User-Jwt), quando disponível —
    usado só pelas tools save_memory/recall_memory para chamar de volta a
    API de memórias do Open WebUI em nome do utilizador certo. Ausente em
    canais sem sessão web (teams, automation).

    channel="web"        : approval tools bloqueadas, utilizador redirecionado para web UI
    channel="teams"      : approval tools geram prompt de confirmação por texto;
                            a acção fica pendente em Redis até o utilizador responder
                            CONFIRMAR/CANCELAR (gerido pelo teams_poller).
    channel="automation" : execução não-interactiva (Lachesis/Automações) — não há
                            utilizador para aprovar um cartão, por isso approval tools
                            são executadas directamente, tal como as ferramentas seguras.
                            A autorização já foi dada por quem escreveu/gravou a automação
                            (mesma lógica de confiança que os nós agentless_script do
                            flow_definition, que também chamam RemoteExecutor sem cartão).
    """
    from anthropic import AnthropicFoundry
    client = AnthropicFoundry(
        api_key  = os.getenv("FOUNDRY_API_KEY",  ""),
        base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
    )

    last_user_text = _last_user_text(messages)
    system   = _build_system_prompt(message_text=last_user_text, has_fates_access=has_fates_access)
    anth_msgs = []
    for m in messages:
        role    = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            extra = content if isinstance(content, str) else ""
            if extra.strip():
                system = _build_system_prompt(message_text=last_user_text, has_fates_access=has_fates_access) + "\n\n## Contexto adicional\n" + extra
        else:
            anth_msgs.append({"role": role, "content": content})

    tools = _tools_for(has_fates_access)
    MAX_ROUNDS = 25
    for _round in range(MAX_ROUNDS):
        resp = client.messages.create(
            model      = "claude-sonnet-4-6",
            system     = system,
            messages   = anth_msgs,
            tools      = tools,
            max_tokens = max_tokens,
            timeout    = 180.0,
        )

        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if hasattr(b, "text"))
            return text

        tool_results       = []
        teams_approval_blk = None   # primeiro approval block no modo Teams

        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue

            if block.name == "propose_infra_knowledge":
                # Propostas de conhecimento passam SEMPRE por aprovação humana,
                # em qualquer canal — mesmo em automações, ao contrário de
                # agentless_run (que já é "confiado" por quem escreveu a
                # automação). Fica pendente em Redis, visível na Casa do
                # Conhecimento, até alguém aprovar/rejeitar.
                action = {
                    "tool":        "propose_infra_knowledge",
                    "title":       block.input.get("title", ""),
                    "instruction": block.input.get("instruction", ""),
                    "category":    block.input.get("category"),
                    "version":     block.input.get("version"),
                    "evidence":    block.input.get("evidence"),
                    "scope_type":  block.input.get("scope_type", "global"),
                    "scope_value": block.input.get("scope_value"),
                }
                token = _store_pending_action(action)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     json.dumps({"status": "pending_approval", "token": token}),
                })
            elif block.name in APPROVAL_TOOLS:
                if channel == "automation":
                    result_json, _sql = execute_tool(block.name, block.input, user_jwt=user_jwt, reason=last_user_text)
                    try:
                        _track_agentless_result(block.name, block.input, result_json)
                    except Exception:
                        pass
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result_json,
                    })
                elif channel == "teams" and chat_id and block.name in ("agentless_run", "agentless_run_bulk"):
                    # Guarda o primeiro bloco de aprovação; os restantes são ignorados
                    # (raramente há mais do que um por turno em Teams). Só agentless_run(_bulk)
                    # tem cartão de confirmação por texto no Teams — propose_infra_knowledge
                    # (e outras approval tools futuras) caem no ramo genérico abaixo.
                    if teams_approval_blk is None:
                        teams_approval_blk = block
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps({"status": "awaiting_teams_confirmation"}),
                    })
                else:
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps({
                            "error": (
                                f"A ferramenta '{block.name}' requer aprovação do utilizador "
                                f"e só está disponível na interface de chat (streaming). "
                                f"Informa o utilizador que deve usar o chat do Jarvis para "
                                f"executar esta acção."
                            ),
                        }),
                        "is_error":    True,
                    })
            else:
                result_json, _sql = execute_tool(block.name, block.input, user_jwt=user_jwt, reason=last_user_text)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     result_json,
                })

        # ── Aprovação Teams: armazena acção e devolve prompt de confirmação ──
        if teams_approval_blk is not None:
            blk = teams_approval_blk
            if blk.name == "agentless_run_bulk":
                hosts   = [h.strip() for h in (blk.input.get("hosts") or []) if isinstance(h, str) and h.strip()]
                action  = {
                    "tool": blk.name,
                    "machine_type": blk.input.get("machine_type", "server"),
                    "script":       blk.input.get("script", ""),
                    "timeout_s":    blk.input.get("timeout_s"),
                    "hosts":        hosts,
                }
                target_label = f"{len(hosts)} máquina(s)"
            else:
                host   = (blk.input.get("host") or "").strip()
                action = {
                    "tool": blk.name,
                    "machine_type": blk.input.get("machine_type", "server"),
                    "script":       blk.input.get("script", ""),
                    "timeout_s":    blk.input.get("timeout_s"),
                    "host":         host,
                }
                target_label = host or "máquina desconhecida"

            _store_teams_pending(chat_id, action)

            # Texto explicativo que o LLM já gerou neste turno (se houver)
            llm_text = "".join(b.text for b in resp.content if hasattr(b, "text") and b.text).strip()
            prefix   = f"{llm_text}\n\n" if llm_text else ""
            script_preview = action["script"][:600]
            return (
                f"{prefix}"
                f"⚠️ **Acção pendente de confirmação**\n"
                f"**Alvo:** `{target_label}`\n"
                f"```powershell\n{script_preview}\n```\n\n"
                f"Responde **CONFIRMAR** para executar ou **CANCELAR** para cancelar."
            )

        anth_msgs.append({"role": "assistant", "content": resp.content})
        anth_msgs.append({"role": "user",      "content": tool_results})

    return "Loop limit reached — partial response."


def run_jarvis_loop_stream(messages: list, max_tokens: int = 4096, has_fates_access: bool = True,
                            user_jwt: str | None = None):
    """
    Versão streaming do loop.
    Yields SSE chunks no formato OpenAI.
    Tool execution é transparente — apenas o texto final é transmitido.

    user_jwt: ver run_jarvis_loop — usado por save_memory/recall_memory.
    """
    cid     = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model   = "claude-sonnet-4-6"

    def _chunk(text: str, finish: str | None = None):
        delta = {"content": text} if text else {}
        if finish == "stop":
            delta = {}
        c = {
            "id":      cid,
            "object":  "chat.completion.chunk",
            "created": created,
            "model":   model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(c)}\n\n"

    def _extract_text(content) -> str:
        """Normaliza content (str ou lista OpenAI) para str simples."""
        if isinstance(content, list):
            return "".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return str(content) if content else ""

    try:
        # ── Verificar se é uma negação de acção proposta ──────────────────────
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        last_content = _extract_text((last_user or {}).get("content", ""))
        if last_content.startswith("JARVIS:NEGADO:"):
            token = last_content[14:].strip()
            if token:
                _pop_pending_action(token)
            yield _chunk("\nOk, não vou executar essa acção. Diz-me se queres que faça algo diferente.\n")
            yield _chunk("", "stop")
            yield "data: [DONE]\n\n"
            return

        # ── Setup partilhado: cliente Anthropic + helpers ─────────────────────
        from anthropic import AnthropicFoundry
        client = AnthropicFoundry(
            api_key  = os.getenv("FOUNDRY_API_KEY",  ""),
            base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
        )

        def _build_anth_messages(msgs):
            """Converte mensagens OpenAI em (system, anth_msgs), normalizando
            tokens CONFIRMO/NEGADO para texto neutro que o LLM entende.

            Garante que o histórico não contém tool_use blocks órfãos (sem
            tool_result correspondente) — a API do Claude rejeita isso com 400.
            Mensagens assistente com tool_use blocks de turnos anteriores são
            convertidas para texto puro, mantendo apenas o conteúdo legível.
            """
            last_user_text = next(
                (_extract_text(m.get("content", "")) for m in reversed(msgs) if m.get("role") == "user"),
                "",
            )
            system    = _build_system_prompt(message_text=last_user_text, has_fates_access=has_fates_access)
            anth_msgs = []
            for m in msgs:
                role    = m.get("role", "user")
                raw_content = m.get("content", "")

                # Conteúdo estruturado (lista de blocks) — pode ter tool_use/tool_result
                # de turnos anteriores guardados pelo Open WebUI. Converter para texto
                # puro para evitar tool_use IDs órfãos que a API rejeita.
                if isinstance(raw_content, list):
                    # Extrair apenas texto legível, descartar tool_use/tool_result blocks
                    text_parts = []
                    for block in raw_content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_use":
                                text_parts.append(f"[Executei {block.get('name', 'tool')}]")
                            elif block.get("type") == "tool_result":
                                snippet = str(block.get("content", ""))[:500]
                                text_parts.append(f"[Resultado: {snippet}]")
                        elif hasattr(block, "text"):
                            text_parts.append(block.text)
                        elif hasattr(block, "type") and block.type == "tool_use":
                            text_parts.append(f"[Executei {getattr(block, 'name', 'tool')}]")
                    content = "\n".join(p for p in text_parts if p)
                else:
                    content = str(raw_content) if raw_content else ""

                if role == "system":
                    if content.strip():
                        system = _build_system_prompt(message_text=last_user_text, has_fates_access=has_fates_access) + "\n\n## Contexto adicional\n" + content
                elif content.startswith("JARVIS:CONFIRMO:"):
                    anth_msgs.append({"role": role, "content": "✓ Acção aprovada."})
                elif content.startswith("JARVIS:NEGADO:"):
                    anth_msgs.append({"role": role, "content": "✗ Acção negada pelo utilizador."})
                else:
                    anth_msgs.append({"role": role, "content": content})

            # Garantia final: remover mensagens vazias e garantir alternância user/assistant
            cleaned = []
            for m in anth_msgs:
                if not m.get("content", "").strip():
                    m["content"] = "(sem conteúdo)"
                cleaned.append(m)
            anth_msgs = cleaned

            return system, anth_msgs

        _host_run_counts: dict[str, int] = {}
        _HOST_RUN_LIMIT = 8

        def _sanitize_messages(msgs):
            """Garante que todo tool_use block tem tool_result na mensagem seguinte.
            Remove pares tool_use/tool_result órfãos para evitar erro 400 da API."""
            if len(msgs) < 2:
                return msgs
            sanitized = []
            i = 0
            while i < len(msgs):
                msg = msgs[i]
                content = msg.get("content")
                if msg.get("role") == "assistant" and isinstance(content, list):
                    has_tool_use = any(
                        (isinstance(b, dict) and b.get("type") == "tool_use") or
                        (hasattr(b, "type") and b.type == "tool_use")
                        for b in content
                    )
                    if has_tool_use:
                        next_msg = msgs[i + 1] if i + 1 < len(msgs) else None
                        next_has_result = (
                            next_msg is not None
                            and next_msg.get("role") == "user"
                            and isinstance(next_msg.get("content"), list)
                            and any(
                                isinstance(b, dict) and b.get("type") == "tool_result"
                                for b in next_msg["content"]
                            )
                        )
                        if not next_has_result:
                            text = "".join(
                                (b.get("text", "") if isinstance(b, dict) else getattr(b, "text", ""))
                                for b in content
                                if (isinstance(b, dict) and b.get("type") == "text") or hasattr(b, "text")
                            )
                            sanitized.append({"role": "assistant", "content": text or "(acção executada)"})
                            i += 1
                            continue
                sanitized.append(msg)
                i += 1
            return sanitized

        def _reasoning_loop(system, anth_msgs):
            """Loop de raciocínio com ferramentas: LLM -> tool calls -> execute -> LLM ...
            Pode ser chamado tanto no fluxo normal como após uma acção aprovada
            (CONFIRMO), permitindo ao LLM propor novas acções com cartões válidos.
            Inclui rate limiting por host para evitar stress excessivo via WinRM."""
            tools = _tools_for(has_fates_access)
            MAX_ROUNDS = 25
            for _round in range(MAX_ROUNDS):
                anth_msgs = _sanitize_messages(anth_msgs)
                resp = client.messages.create(
                    model      = model,
                    system     = system,
                    messages   = anth_msgs,
                    tools      = tools,
                    max_tokens = max_tokens,
                    timeout    = 180.0,
                )

                if resp.stop_reason != "tool_use":
                    text = "".join(b.text for b in resp.content if hasattr(b, "text"))

                    if resp.stop_reason == "max_tokens" and _round < MAX_ROUNDS - 1:
                        # Resposta cortada pelo limite de tokens antes de chamar uma
                        # ferramenta — não é uma resposta final. Pede ao LLM para
                        # continuar de forma direta, sem repetir o texto já gerado.
                        anth_msgs.append({"role": "assistant", "content": resp.content})
                        anth_msgs.append({"role": "user", "content": (
                            "[A tua resposta anterior foi cortada pelo limite de "
                            "tokens antes de chamares uma ferramenta. Não repitas o "
                            "texto já escrito — continua imediatamente, de forma "
                            "direta e concisa, chamando a ferramenta necessária para "
                            "avançar com a tarefa.]"
                        )})
                        continue

                    words = text.split(" ")
                    for i, w in enumerate(words):
                        part = w if i == len(words) - 1 else w + " "
                        yield _chunk(part)
                    yield _chunk("", "stop")
                    yield "data: [DONE]\n\n"
                    return

                # Separar ferramentas seguras das que precisam de aprovação
                tool_blocks = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
                safe_blocks     = [b for b in tool_blocks if b.name not in APPROVAL_TOOLS]
                approval_blocks = [b for b in tool_blocks if b.name in APPROVAL_TOOLS]

                # Emitir indicador de ferramentas
                all_names = [b.name for b in tool_blocks]
                if all_names:
                    yield _chunk(f"\n*[{', '.join(all_names)}...]*\n")

                # Executar ferramentas seguras (com rate limit por host)
                tool_results = []
                for block in safe_blocks:
                    if block.name in ("agentless_run", "agentless_run_bulk"):
                        hosts = []
                        if block.name == "agentless_run_bulk":
                            hosts = [h.strip() for h in (block.input.get("hosts") or []) if isinstance(h, str)]
                        else:
                            h = (block.input.get("host") or "").strip()
                            if h:
                                hosts = [h]
                        over_limit = []
                        for h in hosts:
                            _host_run_counts[h] = _host_run_counts.get(h, 0) + 1
                            if _host_run_counts[h] > _HOST_RUN_LIMIT:
                                over_limit.append(h)
                        if over_limit:
                            tool_results.append({
                                "type":        "tool_result",
                                "tool_use_id": block.id,
                                "content":     json.dumps({
                                    "error": f"Limite de {_HOST_RUN_LIMIT} execuções por host atingido para: {', '.join(over_limit)}. "
                                             f"Agrega as queries num único script mais abrangente.",
                                }),
                                "is_error":    True,
                            })
                            continue
                    result_json, _sql = execute_tool(block.name, block.input, user_jwt=user_jwt, reason=last_content)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result_json,
                    })

                if approval_blocks:
                    agentless_blocks = [b for b in approval_blocks if b.name in ("agentless_run", "agentless_run_bulk")]
                    knowledge_blocks = [b for b in approval_blocks if b.name == "propose_infra_knowledge"]

                    # (block, action) — tudo o que vai efectivamente virar um
                    # cartão jarvis-approval, agentless ou conhecimento.
                    pending_cards: list[tuple] = []

                    # ── propose_infra_knowledge — validação simples ──────────
                    valid_knowledge   = [b for b in knowledge_blocks if (b.input.get("title") or "").strip() and (b.input.get("instruction") or "").strip()]
                    invalid_knowledge = [b for b in knowledge_blocks if b not in valid_knowledge]

                    for block in invalid_knowledge:
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     "Erro: 'title' e 'instruction' são obrigatórios para propor um facto de infraestrutura.",
                            "is_error":    True,
                        })

                    for block in valid_knowledge:
                        action = {
                            "tool":        "propose_infra_knowledge",
                            "title":       block.input.get("title", "").strip(),
                            "instruction": block.input.get("instruction", "").strip(),
                            "category":    block.input.get("category"),
                            "version":     block.input.get("version"),
                            "evidence":    block.input.get("evidence"),
                            "scope_type":  block.input.get("scope_type", "global"),
                            "scope_value": block.input.get("scope_value"),
                        }
                        pending_cards.append((block, action))

                    # ── agentless_run usa "host" (1 máquina); agentless_run_bulk
                    # usa "hosts" (lista) — devolve sempre uma lista de alvos.
                    def _get_targets(blk):
                        if blk.name == "agentless_run_bulk":
                            hosts = blk.input.get("hosts") or []
                            return [h.strip() for h in hosts if isinstance(h, str) and h.strip()]
                        h = (blk.input.get("host") or blk.input.get("machine") or "").strip()
                        return [h] if h else []

                    valid_approvals   = [b for b in agentless_blocks if _get_targets(b)]
                    invalid_approvals = [b for b in agentless_blocks if not _get_targets(b)]

                    # agentless_run_bulk com demasiados hosts não gera cartão —
                    # o utilizador não deve aprovar 1000+ máquinas de uma vez.
                    oversized = [
                        b for b in valid_approvals
                        if b.name == "agentless_run_bulk" and len(_get_targets(b)) > _BULK_MAX_HOSTS
                    ]
                    valid_approvals = [b for b in valid_approvals if b not in oversized]

                    for block in invalid_approvals:
                        if block.name == "agentless_run_bulk":
                            err = "Erro: campo 'hosts' é obrigatório e não pode estar vazio. Indica a lista de máquinas-alvo."
                        else:
                            err = "Erro: campo 'host' é obrigatório. Pergunta ao utilizador o hostname da máquina."
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     err,
                            "is_error":    True,
                        })

                    for block in oversized:
                        n = len(_get_targets(block))
                        err = (
                            f"Erro: lista de {n} hosts excede o limite de {_BULK_MAX_HOSTS} "
                            f"por chamada. Divide em lotes de até {_BULK_MAX_HOSTS} hosts e "
                            f"chama agentless_run_bulk uma vez por lote, sequencialmente "
                            f"(cada lote terá o seu próprio cartão de aprovação), agregando "
                            f"os resultados de todos os lotes no fim."
                        )
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     err,
                            "is_error":    True,
                        })

                    for block in valid_approvals:
                        targets = _get_targets(block)
                        action = {
                            "tool":         block.name,
                            "machine_type": block.input.get("machine_type", "workstation"),
                            "script":       block.input.get("script", ""),
                            "timeout_s":    block.input.get("timeout_s"),
                        }
                        if block.name == "agentless_run_bulk":
                            action["hosts"] = targets
                        else:
                            action["host"] = targets[0]
                        pending_cards.append((block, action))

                    if pending_cards:
                        # Antes de emitir cards, garantir que todos os tool_use
                        # blocks (safe + approval) têm tool_result — senão a
                        # próxima chamada à API Claude falha com 400.
                        # Os safe_blocks já foram executados; para os blocks
                        # que vão virar cards, geramos um resultado placeholder.
                        for block, _ in pending_cards:
                            tool_results.append({
                                "type":        "tool_result",
                                "tool_use_id": block.id,
                                "content":     json.dumps({"status": "pending_approval"}),
                            })

                        llm_text = "".join(b.text for b in resp.content if hasattr(b, "text") and b.text)

                        last_msg = anth_msgs[-1] if anth_msgs else None
                        has_prior_tool_result = (
                            last_msg is not None
                            and last_msg.get("role") == "user"
                            and isinstance(last_msg.get("content"), list)
                            and any(
                                isinstance(c, dict) and c.get("type") == "tool_result"
                                for c in last_msg["content"]
                            )
                        )
                        if not llm_text.strip() and has_prior_tool_result and _round < MAX_ROUNDS - 1:
                            anth_msgs.append({"role": "assistant", "content": resp.content})
                            anth_msgs.append({"role": "user", "content": tool_results + [{
                                "type": "text",
                                "text": (
                                    "[Antes de pedires esta nova acção, escreve em texto "
                                    "visível um resumo do resultado da acção anterior que "
                                    "acabaste de executar — o que encontraste (dados, "
                                    "sucesso ou erro). Esse resultado não fica disponível "
                                    "nos próximos turnos, por isso é importante reportá-lo "
                                    "agora. Depois, se ainda for necessário, pede a próxima "
                                    "acção com o cartão de aprovação.]"
                                ),
                            }]})
                            continue

                        if llm_text:
                            yield _chunk(llm_text + "\n")

                        # Emitir card de aprovação para cada ferramenta válida
                        # (agentless e/ou propose_infra_knowledge, misturados)
                        for block, action in pending_cards:
                            token = _store_pending_action(action)
                            card = json.dumps({**action, "token": token}, ensure_ascii=False)
                            yield _chunk(f"\n```jarvis-approval\n{card}\n```\n")

                        yield _chunk("", "stop")
                        yield "data: [DONE]\n\n"
                        return
                    # Todos inválidos — adicionar erros e continuar loop para o LLM corrigir
                    anth_msgs.append({"role": "assistant", "content": resp.content})
                    anth_msgs.append({"role": "user",      "content": tool_results})
                    continue

                # Sem aprovações pendentes — continuar loop
                anth_msgs.append({"role": "assistant", "content": resp.content})
                anth_msgs.append({"role": "user",      "content": tool_results})

            yield _chunk("Loop limit reached.")
            yield _chunk("", "stop")
            yield "data: [DONE]\n\n"

        # ── Verificar se é uma confirmação de acção aprovada ──────────────────
        if last_content.startswith("JARVIS:CONFIRMO:"):
            token = last_content[16:].strip()
            action = _pop_pending_action(token) if token else None
            if action is None:
                recovered = _recover_action_from_history(messages, token)
                if recovered:
                    new_token = _store_pending_action(recovered)
                    card = json.dumps({**recovered, "token": new_token}, ensure_ascii=False)
                    yield _chunk(
                        "\n⚠️ O cartão anterior expirou. Gerei um novo automaticamente:\n"
                    )
                    yield _chunk(f"\n```jarvis-approval\n{card}\n```\n")
                    yield _chunk("", "stop")
                    yield "data: [DONE]\n\n"
                else:
                    yield _chunk(
                        "\nEste cartão de aprovação expirou e não foi possível "
                        "recuperar a acção original. Por favor repete o pedido.\n"
                    )
                    yield _chunk("", "stop")
                    yield "data: [DONE]\n\n"
                return
            try:
                tool_name    = action.get("tool", "agentless_run")

                if tool_name == "propose_infra_knowledge":
                    yield _chunk(f"*[a guardar conhecimento: {action.get('title', '')}...]*\n")
                    tool_input = {
                        "title":       action.get("title", ""),
                        "instruction": action.get("instruction", ""),
                        "category":    action.get("category"),
                        "version":     action.get("version"),
                        "evidence":    action.get("evidence"),
                        "scope_type":  action.get("scope_type", "global"),
                        "scope_value": action.get("scope_value"),
                    }
                    timeout_s = None
                else:
                    machine_type = action.get("machine_type", "workstation")
                    script       = action.get("script", "")
                    timeout_s    = action.get("timeout_s")

                    if tool_name == "agentless_run_bulk":
                        hosts = action.get("hosts", [])
                        yield _chunk(f"*[{tool_name} em {len(hosts)} máquinas...]*\n")
                        tool_input = {"hosts": hosts, "machine_type": machine_type, "script": script}
                    else:
                        host = action.get("host", "")
                        yield _chunk(f"*[{tool_name} em {host}...]*\n")
                        tool_input = {"host": host, "machine_type": machine_type, "script": script}

                if timeout_s:
                    tool_input["timeout_s"] = timeout_s

                # Executar em background thread para poder enviar heartbeats
                # SSE enquanto o WinRM/broker trabalha — evita timeout do stream.
                import concurrent.futures as _cf
                _exec_pool = _cf.ThreadPoolExecutor(max_workers=1)
                _future = _exec_pool.submit(execute_tool, tool_name, tool_input)
                _heartbeat_interval = 8
                while not _future.done():
                    try:
                        _future.result(timeout=_heartbeat_interval)
                    except _cf.TimeoutError:
                        yield _chunk("")  # heartbeat SSE — mantém stream viva
                result_json, _sql = _future.result()
                _exec_pool.shutdown(wait=False)

                # Detector de correcção (código, não o LLM) — se isto resolveu
                # uma falha recente no mesmo host com parâmetros diferentes,
                # fica logo proposto para aprovação, sem depender de o modelo reparar.
                for _notice in _track_agentless_result(tool_name, tool_input, result_json):
                    yield _chunk(
                        f"\n💡 *Lição detectada automaticamente para {_notice['host']}: "
                        f"{_notice['instruction']} — pendente em Casa do Conhecimento.*\n"
                    )

                # Reconstruir histórico (sem a mensagem CONFIRMO) e injectar o
                # resultado da acção como um tool_result real, para que o LLM
                # possa continuar o raciocínio — incl. propor novas acções com
                # cartões de aprovação válidos (com token gerado por _store_pending_action).
                system, anth_msgs = _build_anth_messages(messages[:-1])
                tool_use_id = f"toolu_{uuid.uuid4().hex[:20]}"
                anth_msgs.append({
                    "role": "assistant",
                    "content": [{
                        "type":  "tool_use",
                        "id":    tool_use_id,
                        "name":  tool_name,
                        "input": tool_input,
                    }],
                })
                anth_msgs.append({
                    "role": "user",
                    "content": [{
                        "type":        "tool_result",
                        "tool_use_id": tool_use_id,
                        "content":     result_json,
                    }],
                })
                yield from _reasoning_loop(system, anth_msgs)
                return
            except Exception as e:
                import logging as _log
                _log.getLogger("jarvis.chat").exception("Erro no fluxo CONFIRMO")
                yield _chunk(
                    "\nNão foi possível completar esta acção neste momento. "
                    "Tenta novamente ou reformula o pedido.\n"
                )
                yield _chunk("", "stop")
                yield "data: [DONE]\n\n"
                return

        # ── Loop normal de raciocínio ─────────────────────────────────────────
        system, anth_msgs = _build_anth_messages(messages)
        yield from _reasoning_loop(system, anth_msgs)

    except Exception as e:
        import logging as _log
        _log.getLogger("jarvis.chat").exception("Erro no chat engine")
        yield _chunk(
            "\nOcorreu um erro interno. Tenta novamente ou reformula o pedido.\n"
        )
        yield _chunk("", "stop")
        yield "data: [DONE]\n\n"
