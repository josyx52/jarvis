import json
import os
import sys
import time
from datetime import datetime

from rich.console import Console
from rich.text import Text
from rich.markdown import Markdown
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style as PTStyle

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from context import (
    get_db, db_connected, db_url_str,
    get_config_value, set_config_value, ensure_config_table,
)
from commands.chat_tools import TOOL_DEFINITIONS, execute_tool
from commands.llm_client import get_client, get_ollama_models, ollama_running, FoundryLLMClient, CenterLLMClient
from commands.auth import check_auth
from ui.banner import print_banner
from ui import theme

# Ferramentas que executam scripts em máquinas remotas — requerem aprovação explícita
_ACTION_TOOLS = {"agent_run", "agentless_run"}

console = Console()

_SYSTEM = """És o Jarvis, um sistema de operações de infraestrutura AI com capacidade para investigar e resolver problemas em máquinas Windows Server remotas.

Operas como um SRE sénior com acesso directo à base de dados histórica do Jarvis e shell remota em todos os agentes monitorizados.

## Como trabalhar

**Investigação primeiro, acção depois.**
Quando reportado um problema, investiga antes de concluir:
1. Consulta dados históricos (query_events, query_metrics, query_alerts, query_reasoning, query_machine_profile) para perceber o que aconteceu e quando
2. Usa agent_run para obter estado em tempo real — lê logs, verifica serviços, lista processos, corre diagnósticos
3. Continua a investigar até perceber a CAUSA RAIZ, não apenas o sintoma
4. Só quando tens evidências propões e execugas a correcção

**agent_run é a tua shell.** Escreve PowerShell real:
- Logs: `Get-Content C:\\app\\logs\\error.log -Tail 200`
- Serviços: `Get-Service | Where-Object {$_.Status -eq 'Stopped'} | Select Name,Status,StartType`
- Processos: `Get-Process | Sort-Object CPU -Desc | Select-Object -First 20 Name,Id,CPU,WorkingSet`
- Disco: `Get-PSDrive | Where-Object {$_.Provider -like '*FileSystem*'} | Select Name,Used,Free`
- Event log: `Get-EventLog -LogName System -Newest 50 -EntryType Error,Warning | Select TimeGenerated,Source,Message`
- Reparar serviço: `Restart-Service <nome> -Force`

**Deixa os dados guiar.** Não assumes a solução antes de ver a evidência.

**Confirma acções destrutivas.** Antes de reiniciar serviços, modificar ficheiros ou alterar configuração de sistema — explica o que vais fazer e porquê.

## Ferramentas de dados históricos

- **query_metrics** — métricas de CPU/memória/disco por host ao longo do tempo
- **query_events** — eventos de sistema recolhidos pelos agentes (campo: created_at)
- **query_alerts** — alertas gerados pelo Jarvis (campo: created_at, não alert_time)
- **query_problems** — problemas correlacionados (agrupamentos de alertas)
- **analyze_time_pattern** — detecta padrões recorrentes (crashes diários às 16h, etc.)
- **query_agents** — lista todos os hosts monitorizados e estado dos agentes

## Inteligência acumulada pelo Jarvis

O Jarvis aprende continuamente. Estas ferramentas dão acesso ao que já aprendeu:

- **query_reasoning** — histórico de análises AI geradas pelo Jarvis para cada host
  - Miles de análises acumuladas com causa raiz, impacto e recomendações
  - Usa para perceber o histórico de problemas e padrões num host

- **query_machine_profile** — perfil da máquina: papel, ambiente, criticidade, serviços críticos
  - Inclui: role (web_server, database_server, domain_controller, etc.), env (production/staging/dev),
    critical_services (serviços cuja paragem = impacto de negócio), business_impact, tech_stack, risk_flags
  - Se não existir perfil, sugere correr build_machine_profiles

- **build_machine_profiles** — constrói/actualiza perfis de máquina usando AI a partir dos dados históricos
  - Analisa snapshots, serviços, processos, portas, padrão de alertas e reasoning histórico
  - Classifica automaticamente cada host e guarda em server_profiles
  - Pode filtrar por host ou correr para todos

## Segurança — como investigar correctamente

**Ferramenta principal para segurança: `query_security_events`**
Devolve tudo de uma vez: eventos + análises UEBA + perfil comportamental de cada utilizador + perfil de cada máquina afectada + problemas abertos.

**Para cada utilizador identificado num evento ou alerta UEBA, responde a estas perguntas usando os dados devolvidos:**

1. **Quem é este utilizador?**
   - `user_profile.is_human` → é uma pessoa ou conta de serviço/sistema?
   - `user_profile.work_type` → qual a função? admin, developer, service account?
   - `user_profile.profile_summary` → o que o Jarvis já sabe sobre este utilizador?

2. **Este comportamento é normal para ele?**
   - `user_profile.typical_hours` → horário habitual de trabalho
   - `user_profile.active_days` → dias da semana onde costuma estar activo
   - `user_profile.after_hours_activity` → já foi visto fora de horas anteriormente?
   - `user_profile.weekend_activity` → trabalha ao fim-de-semana habitualmente?
   - Se o evento aconteceu fora dos `typical_hours` → é desvio real? Ou padrão já registado?

3. **A máquina onde está está em risco?**
   - `machine_profiles[host].critical_services` → que serviços críticos correm aqui?
   - `machine_profiles[host].business_impact` → qual o impacto se esta máquina cair?
   - `machine_profiles[host].role` → é um servidor ATM? Domain Controller? Power BI Gateway?
   - `open_problems` → há problemas activos neste host que possam estar relacionados?

4. **Se `user_profile` é null** → o Jarvis não tem dados sobre este utilizador. Isso é mais preocupante — usa `agent_run` para investigar em tempo real quem é e o que estava a fazer.

**Sobre contas de sistema Windows:**
Contas como `DWM-N`, `UMFD-N`, `Font Driver Host`, `Window Manager` têm logon/logoff curtos por design. O que determina se são suspeitas NÃO é o tipo de conta — é o contexto: hora, host, frequência, correlação com outros eventos. Usa `agent_run` para ver o estado real antes de escalar.

**Dados de performance são contexto, não evidência:**
CPU a 100% e disco cheio são degradação operacional. Compromisso de segurança tem indicadores diferentes. Menciona performance apenas quando está directamente relacionado com o incidente de segurança.

**Regra de ouro:** um único indicador nunca é suficiente. Só escalas quando tens correlação entre pelo menos 2 fontes independentes (evento + UEBA, ou UEBA + agent_run, ou múltiplos eventos no mesmo utilizador/host).

## UEBA — Comportamento de Utilizadores e Entidades

O Jarvis tem um sistema UEBA que monitoriza actividade de utilizadores nas máquinas:

- **query_security_events** — ferramenta principal para investigação de segurança
  - Agrega eventos de segurança + UEBA + perfis de risco num único resultado
  - Já marca automaticamente: `is_system_account`, `is_authorized_user`, e inclui nota de contexto
  - Inclui a lista de utilizadores autorizados configurada

- **manage_authorized_users** — gere lista de utilizadores/admins autorizados
  - action='list': ver lista actual
  - action='add' username='DOMINIO\\user': marcar utilizador como autorizado (admin, operador)
  - action='remove': retirar da lista
  - **Usar sempre que um utilizador legítimo for sinalizado incorrectamente**

- **query_user_profiles** — perfis comportamentais completos por utilizador/host
  - Contém: horas de login habituais, apps primárias, risco score, anomalias
  - Usar para perceber o baseline de um utilizador específico

- **query_ueba** — análises UEBA detalhadas (usa `analyzed_at` como campo de data)
  - Anomalias: login fora de horas, escalada de privilégios, destinos suspeitos
  - Usar para investigação profunda de um utilizador específico

## Ferramentas de gestão do Centro

- **manage_center** — iniciar, parar, reiniciar ou verificar estado do Jarvis Center
  - status: estado do serviço, saúde da API/DB/Redis
  - start / stop / restart / logs (últimas N linhas)

## Solution Driver — Notificações e Webhooks

- **manage_solution_engine** — activar/desactivar ou verificar estado do Solution Driver
  - Só dispara se activo E houver pelo menos um webhook activo
- **manage_webhook** — configurar para onde o Jarvis envia alertas de problemas
  - add: registar URL de webhook (ex: Power Automate) para receber JSON rico com problema + análise AI
  - list / enable / disable / remove: gerir webhooks existentes
  - O payload inclui: problema, estado da máquina, análise AI, causa raiz

Quando o utilizador perguntar sobre o Solution Driver usa manage_solution_engine.
Quando perguntar sobre webhooks ou notificações usa manage_webhook.

## Regras

- Consulta SEMPRE dados antes de responder sobre estado actual — nunca inventes nem estimes
- Mostra números reais, não resumos vagos
- Responde no idioma do utilizador (português)
- Quando ages numa máquina, reporta exactamente o que fizeste e o resultado
- Quando um host tem perfil de máquina, usa-o para contextualizar a análise (ex: "Este host é domain controller de produção, portanto...")
- Se o utilizador perguntar sobre perfis de utilizador, UEBA ou reasoning — EXISTEM DADOS, usa as ferramentas adequadas
"""

_FOUNDRY_MODELS = [
    ("claude-sonnet-4-6",        "recomendado  —  rápido, capaz"),
    ("claude-opus-4-7",          "máxima capacidade"),
    ("claude-haiku-4-5-20251001","ultra-rápido, económico"),
]


def _render_tool_call(name: str, args: dict, result_json: str, sql: str, elapsed_ms: float):
    arg_str = "  ".join(f"{k}={v}" for k, v in list(args.items())[:3]) if args else ""
    console.print(f"  [dim]╭─[/dim] [cyan]{name}[/cyan] [dim]{arg_str}[/dim]", highlight=False)
    if sql:
        console.print(f"  [dim]│  {sql[:80]}[/dim]", highlight=False)
    try:
        result = json.loads(result_json)
        if isinstance(result, list):
            n = len(result)
            console.print(f"  [dim]╰─ → {n} row{'s' if n != 1 else ''}  ({elapsed_ms:.0f}ms)[/dim]", highlight=False)
        elif isinstance(result, dict) and "error" in result:
            console.print(f"  [dim]╰─[/dim] [red]→ {result['error']}[/red]", highlight=False)
        else:
            console.print(f"  [dim]╰─ → ok  ({elapsed_ms:.0f}ms)[/dim]", highlight=False)
    except Exception:
        console.print(f"  [dim]╰─ → ({elapsed_ms:.0f}ms)[/dim]", highlight=False)
    console.print()


def _render_ai(text: str, model: str, provider: str):
    ts = datetime.now().strftime("%H:%M:%S")
    header = Text()
    header.append(f"  [{ts}] ", style="dim")
    header.append("▲ JARVIS", style=theme.DELTA)
    prov_label = "ollama" if provider == "ollama" else "foundry"
    header.append(f"  {model}", style="dim cyan")
    header.append(f"  [{prov_label}]", style="dim")
    console.print(header)
    console.print(f"  [dim]{'─' * 64}[/dim]", highlight=False)
    console.print(Markdown(text))
    console.print()


def _render_user(text: str):
    ts = datetime.now().strftime("%H:%M:%S")
    header = Text()
    header.append(f"\n  [{ts}] ", style="dim")
    header.append("você", style=theme.USER_MSG)
    console.print(header)
    console.print(f"  [dim]{'─' * 64}[/dim]", highlight=False)
    console.print(f"  {text}\n", style="bright_white")


def _show_model_picker(current_model: str, current_provider: str):
    ollama_url  = get_config_value("llm.ollama_url", "http://localhost:11434")
    ollama_ok   = ollama_running(ollama_url)
    ollama_list = get_ollama_models(ollama_url) if ollama_ok else []

    console.print()
    console.print("  [dim cyan]── Foundry (Azure AI / Anthropic) ──[/dim cyan]")
    for m, desc in _FOUNDRY_MODELS:
        active = current_model == m and current_provider == "foundry"
        mark = "[yellow]●[/yellow]" if active else "[dim]○[/dim]"
        console.print(f"    {mark} [cyan]{m}[/cyan]  [dim]{desc}[/dim]", highlight=False)

    console.print()
    if ollama_ok:
        console.print(f"  [dim cyan]── Ollama  ({ollama_url}) ──[/dim cyan]")
        if ollama_list:
            for m in ollama_list:
                active = current_model == m and current_provider == "ollama"
                mark = "[yellow]●[/yellow]" if active else "[dim]○[/dim]"
                console.print(f"    {mark} [cyan]{m}[/cyan]", highlight=False)
        else:
            console.print("  [dim]  nenhum modelo instalado — use: ollama pull <model>[/dim]")
    else:
        console.print(f"  [dim]── Ollama  ({ollama_url})  [red]offline[/red] ──[/dim]")

    console.print()
    console.print("  [dim]Para mudar: /model <id>             (mantém provider actual)[/dim]")
    console.print("  [dim]            /model foundry <id>     (força Foundry)[/dim]")
    console.print("  [dim]            /model ollama <id>      (força Ollama)[/dim]")
    console.print()


def _apply_model_change(raw_args: str) -> tuple[str, str]:
    """
    Parse /model [provider] <id>.
    Returns (new_model, new_provider).
    """
    parts    = raw_args.strip().split(None, 1)
    provider = get_config_value("llm.provider", "foundry")
    model    = get_config_value("llm.model",    "claude-sonnet-4-6")

    if not parts:
        return model, provider

    if parts[0].lower() in ("foundry", "ollama"):
        provider = parts[0].lower()
        model    = parts[1].strip() if len(parts) > 1 else model
    else:
        model = parts[0]

    set_config_value("llm.provider", provider)
    set_config_value("llm.model",    model)
    return model, provider


def _render_action_approval(tool_name: str, args: dict, session: PromptSession) -> bool:
    """
    Mostra o comando proposto pelo Jarvis e pede aprovação explícita.
    Devolve True se aprovado, False se negado.
    """
    console.print()
    console.print(f"  [yellow bold]{'─' * 64}[/yellow bold]", highlight=False)
    console.print(f"  [yellow bold]⚡ ACÇÃO PROPOSTA — {tool_name}[/yellow bold]")
    console.print(f"  [yellow bold]{'─' * 64}[/yellow bold]", highlight=False)

    host   = args.get("host", "N/A")
    script = args.get("script", "")

    console.print(f"\n  [dim]Host:[/dim]  [cyan]{host}[/cyan]")

    if script:
        console.print(f"\n  [dim]Script a executar:[/dim]")
        console.print(f"  [dim]{'─' * 48}[/dim]", highlight=False)
        for line in script.strip().splitlines():
            console.print(f"  [bright_white]{line}[/bright_white]", highlight=False)
        console.print(f"  [dim]{'─' * 48}[/dim]", highlight=False)

    # Outros args além de host e script
    extra = {k: v for k, v in args.items() if k not in ("host", "script")}
    if extra:
        for k, v in extra.items():
            console.print(f"  [dim]{k}:[/dim] {v}")

    console.print()
    console.print("  [green bold][A][/green bold][dim] Aprovar[/dim]   [red bold][N][/red bold][dim] Negar[/dim]")

    try:
        answer = session.prompt([("class:prompt", "  Decisão > ")]).strip().lower()
    except (KeyboardInterrupt, EOFError):
        answer = "n"

    approved = answer in ("a", "aprovar", "approve", "y", "yes", "sim", "s")

    if approved:
        console.print(f"  [green]✓ Aprovado — a executar...[/green]\n")
    else:
        console.print(f"  [red]✗ Negado pelo operador.[/red]\n")

    return approved


def run_chat():
    ensure_config_table()

    connected = db_connected()
    print_banner(console, connected=connected, db_url=db_url_str())

    if not connected:
        console.print("  [red]● Database unreachable — start Jarvis center first.[/red]\n")

    # ── Autenticação ─────────────────────────────────────────────────────────
    if not check_auth():
        sys.exit(1)

    provider = get_config_value("llm.provider", "foundry")
    model    = get_config_value("llm.model",    "claude-sonnet-4-6")
    client   = get_client(model)

    console.print(f"  [dim]Provider:[/dim] [cyan]{provider}[/cyan]  [dim]Model:[/dim] [cyan]{model}[/cyan]")
    console.print("  [dim]Comandos:[/dim] [cyan]/model[/cyan]  [cyan]/clear[/cyan]  [cyan]/exit[/cyan]")
    console.print(f"  [dim]{'─' * 64}[/dim]\n", highlight=False)

    session  = PromptSession(
        history = InMemoryHistory(),
        style   = PTStyle.from_dict({"prompt": "ansiyellow bold", "": "ansiwhite"}),
    )
    messages:     list = []
    tokens_total: int  = 0

    while True:
        try:
            raw = session.prompt([("class:prompt", "  ▲ > ")]).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n  [dim]Jarvis desligado.[/dim]\n")
            break

        if not raw:
            continue

        if raw.lower() in ("/exit", "/quit", "exit", "quit"):
            console.print("\n  [dim]Jarvis desligado.[/dim]\n")
            break

        if raw.lower() == "/clear":
            console.clear()
            print_banner(console, connected=db_connected(), db_url=db_url_str())
            messages = []
            continue

        if raw.lower().startswith("/model"):
            args = raw[6:].strip()
            if not args:
                _show_model_picker(model, provider)
                continue

            model, provider = _apply_model_change(args)
            client   = get_client(model)
            messages = []  # clear history on provider/model change
            console.print(f"\n  [green]● {provider}  [bold]{model}[/bold]  (histórico limpo)[/green]\n")
            continue

        _render_user(raw)
        messages.append({"role": "user", "content": raw})

        # ── LLM call loop ─────────────────────────────────────────────────
        with console.status("  [dim cyan]▓▓▓▒░  a consultar...[/dim cyan]", spinner="dots"):
            try:
                resp = client.chat(messages, _SYSTEM, TOOL_DEFINITIONS)
                tokens_total += resp.input_tokens + resp.output_tokens
            except Exception as e:
                console.print(f"\n  [red]● LLM error: {e}[/red]\n")
                messages.pop()
                continue

        while resp.stop_reason == "tool_use":
            results = []
            for tc in resp.tool_calls:
                # Acções que modificam máquinas remotas requerem aprovação explícita
                if tc.name in _ACTION_TOOLS:
                    approved = _render_action_approval(tc.name, tc.args, session)
                    if not approved:
                        import json as _json
                        result_json = _json.dumps({
                            "denied": True,
                            "message": "Acção negada pelo operador. O Jarvis não executou este comando.",
                        })
                        console.print(f"  [dim]╭─[/dim] [cyan]{tc.name}[/cyan] [red]NEGADO[/red]", highlight=False)
                        console.print(f"  [dim]╰─ → operador negou a execução[/dim]\n", highlight=False)
                        results.append((tc.id, result_json))
                        continue

                t0          = time.time()
                result_json, sql = execute_tool(tc.name, tc.args)
                elapsed     = (time.time() - t0) * 1000
                _render_tool_call(tc.name, tc.args, result_json, sql, elapsed)
                results.append((tc.id, result_json))

            client.append_tool_round(messages, resp, results)

            with console.status("  [dim cyan]▓▓▓▒░  a analisar...[/dim cyan]", spinner="dots"):
                try:
                    resp = client.chat(messages, _SYSTEM, TOOL_DEFINITIONS)
                    tokens_total += resp.input_tokens + resp.output_tokens
                except Exception as e:
                    console.print(f"\n  [red]● LLM error: {e}[/red]\n")
                    break

        if resp.text:
            _render_ai(resp.text, model, provider)

        # append final assistant message to history
        if isinstance(client, (FoundryLLMClient, CenterLLMClient)):
            messages.append({"role": "assistant", "content": resp._raw})
        else:
            messages.append({"role": "assistant", "content": resp.text})

        console.print(f"  [dim]tokens sessão: {tokens_total:,}[/dim]\n", highlight=False)
