"""
ConversationEngine — gere a conversa fluida entre analista e Jarvis.

Quando o analista responde via /webhook/reply/{conv_id}:
  1. Carrega: conversa + todas as mensagens anteriores + estado actual da máquina
  2. Verifica se o problema ainda está aberto ou foi resolvido
  3. Constrói prompt com contexto completo para Claude
  4. Claude pode usar tool calling (agentless_run, query_ad) para investigar
  5. Claude devolve JSON com: resposta + intenção detectada + se executar
  6. Se aprovação: cria agent_command e actualiza conversa
  7. Envia resposta de volta para todos os webhooks da conversa

A IA detecta intenção de forma natural — sem keyword matching frágil.
Suporta tool calling para permitir ao Claude investigar activamente.
"""

import json
import os
import re
import subprocess
import threading

from brain.foundry_client import FoundryClient
from storage.solution_store import SolutionStore
from engines.notification_engine import NotificationEngine
from agentless.agentless_chat import AgentlessChat

_agentless_chat = AgentlessChat()

# ── Tool definitions para o ConversationEngine ───────────────────────────────
# Subset das tools do ChatEngine — apenas leitura + agentless_run (com
# aprovação implícita, pois o analista já está numa conversa sobre um problema).
_CONV_TOOLS = [
    {
        "name": "agentless_run",
        "description": "Execute a PowerShell script on the conversation's target host or another host via WinRM. Use for real-time diagnostics.",
        "input_schema": {
            "type": "object",
            "required": ["host", "script"],
            "properties": {
                "host":         {"type": "string", "description": "Target hostname"},
                "script":       {"type": "string", "description": "PowerShell script (read-only, max 15 lines)"},
                "machine_type": {"type": "string", "enum": ["workstation", "server"], "description": "Default: server"},
                "timeout_s":    {"type": "integer", "description": "Default: 60"},
            },
        },
    },
    {
        "name": "query_ad",
        "description": "Query Active Directory via LDAP. Read-only, no approval needed.",
        "input_schema": {
            "type": "object",
            "required": ["filter"],
            "properties": {
                "filter":     {"type": "string", "description": "LDAP filter"},
                "attributes": {"type": "array", "items": {"type": "string"}},
                "limit":      {"type": "integer"},
            },
        },
    },
]

_CONV_MAX_ROUNDS = 5
_CONV_MAX_RUNS_PER_HOST: dict[str, int] = {}
_CONV_HOST_LIMIT = 5


class ConversationEngine:

    def __init__(self, store: SolutionStore):
        self.llm   = FoundryClient()
        self._store = store
        self._notif = NotificationEngine(store)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ── Ponto de entrada (chamado pelo endpoint /webhook/reply) ───────────────

    def process_reply(self, conv_id: str, analyst_message: str):
        """
        Processa resposta do analista em background thread.
        Thread-safe por conv_id — cada conversa tem o seu próprio lock.
        """
        with self._locks_guard:
            if conv_id not in self._locks:
                self._locks[conv_id] = threading.Lock()
            lock = self._locks[conv_id]

        if not lock.acquire(blocking=False):
            print(f"[CONV] {conv_id} já em processamento, a ignorar reply duplicado")
            return
        try:
            self._handle(conv_id, analyst_message)
        finally:
            lock.release()

    # ── Processamento da resposta ─────────────────────────────────────────────

    def _handle(self, conv_id: str, analyst_message: str):
        conv = self._store.get_conversation(conv_id)
        if not conv:
            print(f"[CONV] conversa {conv_id} não encontrada")
            return

        if conv["status"] in ("resolved", "expired"):
            print(f"[CONV] {conv_id} já está {conv['status']} — ignorar")
            return

        self._store.update_conversation_status(conv_id, "in_progress")

        messages       = self._store.get_messages(conv_id)
        current_state  = self._store.get_latest_snapshot(conv["host"])
        snapshot_at    = conv.get("snapshot_at_detection") or {}

        system_prompt = self._build_prompt(conv, messages, analyst_message,
                                           current_state, snapshot_at)

        # Construir mensagens Anthropic com histórico + nova mensagem
        anth_msgs = []
        for m in messages[-20:]:
            role = "user" if m["role"] == "analyst" else "assistant"
            anth_msgs.append({"role": role, "content": m["content"]})
        anth_msgs.append({"role": "user", "content": analyst_message})

        # Tool calling loop — permite ao Claude investigar activamente
        from anthropic import AnthropicFoundry
        client = AnthropicFoundry(
            api_key  = os.getenv("FOUNDRY_API_KEY", ""),
            base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
        )

        host_run_counts: dict[str, int] = {}

        for _round in range(_CONV_MAX_ROUNDS):
            try:
                resp = client.messages.create(
                    model      = "claude-sonnet-4-6",
                    system     = system_prompt,
                    messages   = anth_msgs,
                    tools      = _CONV_TOOLS,
                    max_tokens = 2000,
                )
            except Exception as e:
                print(f"[CONV] LLM error em {conv_id} round {_round}: {e}")
                self._store.update_conversation_status(conv_id, "awaiting_analyst")
                return

            if resp.stop_reason != "tool_use":
                # Resposta final — extrair texto
                raw_text = "".join(b.text for b in resp.content if hasattr(b, "text"))
                break

            # Executar tools chamadas pelo Claude
            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue

                if block.name == "agentless_run":
                    host = block.input.get("host", "")
                    host_run_counts[host] = host_run_counts.get(host, 0) + 1
                    if host_run_counts[host] > _CONV_HOST_LIMIT:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps({"error": f"Limite de {_CONV_HOST_LIMIT} execuções atingido para {host}. Agrega queries num só script."}),
                            "is_error": True,
                        })
                        continue
                    result_json = self._exec_agentless_run(block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_json,
                    })
                elif block.name == "query_ad":
                    result_json = self._exec_query_ad(block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_json,
                    })
                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps({"error": f"Tool desconhecida: {block.name}"}),
                        "is_error": True,
                    })

            anth_msgs.append({"role": "assistant", "content": resp.content})
            anth_msgs.append({"role": "user", "content": tool_results})
        else:
            raw_text = "Atingi o limite de investigação. Com base no que recolhi, eis o que encontrei."

        # Extrair resultado estruturado
        result = self._parse_response(raw_text)

        response          = result.get("response", raw_text)
        intent            = result.get("analyst_intent", "question")
        execute_now       = result.get("execute_now", False)
        conv_should_close = result.get("close_conversation", False)

        self._store.add_message(conv_id, "analyst", analyst_message)
        self._store.add_message(conv_id, "jarvis", response,
                                metadata={"intent": intent, "execute_now": execute_now})

        new_status = "awaiting_analyst"

        if intent == "approve_action" and execute_now:
            script = conv.get("proposed_script", "")
            if script:
                cmd_id = self._store.save_command(
                    host       = conv["host"],
                    script     = script,
                    risk_level = conv.get("risk_level", "medium"),
                    problem_id = conv.get("problem_id", ""),
                    conv_id    = conv_id,
                )
                print(f"[CONV] {conv_id}: acção aprovada -> cmd_id={cmd_id}")
                new_status = "action_approved"
            else:
                print(f"[CONV] {conv_id}: aprovação sem script — ignorar execução")

        elif intent == "reject_action":
            new_status = "action_rejected"
            print(f"[CONV] {conv_id}: acção rejeitada pelo analista")

        elif intent == "close" or conv_should_close:
            new_status = "resolved"
            print(f"[CONV] {conv_id}: conversa encerrada pelo analista")

        self._store.update_conversation_status(conv_id, new_status)
        conv["status"] = new_status
        self._notif.send_reply(conv, response)

    # ── Tool execution helpers ───────────────────────────────────────────────

    @staticmethod
    def _exec_agentless_run(args: dict) -> str:
        try:
            from agentless.remote_executor import RemoteExecutor
            executor = RemoteExecutor(
                host=args.get("host", ""),
                machine_type=args.get("machine_type", "server"),
            )
            result = executor.run(
                args.get("script", ""),
                timeout=args.get("timeout_s", 60),
            )
            return json.dumps({
                "stdout":    (result.get("stdout") or "")[:10000],
                "stderr":    (result.get("stderr") or "")[:5000],
                "exit_code": result.get("exit_code", -1),
                "success":   result.get("ok", False),
            })
        except Exception as e:
            return json.dumps({"error": str(e)})

    @staticmethod
    def _exec_query_ad(args: dict) -> str:
        try:
            from api.chat_engine import _query_ad
            result_json, _ = _query_ad(
                filter=args.get("filter", ""),
                attributes=args.get("attributes"),
                limit=args.get("limit", 0),
            )
            return result_json
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ── Agentless — detecção e execução automática ───────────────────────────

    _WORD_RE = re.compile(r'\b([A-Z][A-Z0-9\-]{3,15})\b')

    def _resolve_machine_type(self, machine: str) -> str:
        """Consulta o AD para determinar se é server ou workstation via OU."""
        try:
            import subprocess
            script = (
                f"$r = ([adsisearcher]'(&(objectClass=computer)(name={machine}))').FindOne(); "
                f"if ($r) {{ $r.Properties['distinguishedName'][0] }} else {{ 'NOT_FOUND' }}"
            )
            r = subprocess.run(
                ["powershell.exe", "-NonInteractive", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            )
            dn = (r.stdout or "").strip().lower()
            if "not_found" in dn or not dn:
                return "server"
            if any(kw in dn for kw in ("ou=workstation", "ou=baseline wks", "ou=laptops", "ou=desktops")):
                return "workstation"
            return "server"
        except Exception:
            return "server"

    def _try_agentless(self, analyst_message: str, conv_host: str) -> str | None:
        """
        Se a mensagem mencionar uma máquina, tenta responder via WinRM.
        Usa query_ad para validar se o nome existe como computador no AD,
        em vez de depender de regex de prefixos.
        """
        words = self._WORD_RE.findall(analyst_message.upper())
        if not words:
            return None

        conv_hostname = conv_host.split("::")[0].upper()

        machine = None
        for word in words:
            if word == conv_hostname:
                continue
            try:
                import subprocess
                check = subprocess.run(
                    ["powershell.exe", "-NonInteractive", "-NoProfile", "-Command",
                     f"([adsisearcher]'(&(objectClass=computer)(name={word}))').FindOne() -ne $null"],
                    capture_output=True, text=True, timeout=5,
                    encoding="utf-8", errors="replace",
                )
                if "True" in (check.stdout or ""):
                    machine = word
                    break
            except Exception:
                continue

        if not machine:
            return None

        machine_type = self._resolve_machine_type(machine)

        try:
            result = _agentless_chat.ask(
                machine      = machine,
                question     = analyst_message,
                machine_type = machine_type,
            )
            if result["ok"]:
                return (
                    f"DADOS RECOLHIDOS VIA WINRM DA MÁQUINA {machine}:\n"
                    f"Script executado: {result['script']}\n"
                    f"Output:\n{result['raw_output'][:3000]}"
                )
            else:
                return f"MÁQUINA {machine}: tentativa WinRM falhou — {result['error']}"
        except Exception as e:
            return f"MÁQUINA {machine}: erro agentless — {e}"

    # ── Prompt ───────────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        conv:             dict,
        messages:         list[dict],
        analyst_message:  str,
        current_state:    dict,
        snapshot_at:      dict,
    ) -> str:

        hostname = conv["host"].split("::")[0]

        cpu_then = (snapshot_at or {}).get("cpu_percent", "?")
        cpu_now  = current_state.get("cpu_percent", "?")
        mem_then = (snapshot_at or {}).get("memory_percent", "?")
        mem_now  = current_state.get("memory_percent", "?")
        as_of    = current_state.get("as_of", "desconhecido")

        return f"""És o Jarvis, sistema de observabilidade com IA a conversar com um analista de infraestrutura via webhook.

HOST ALVO: {hostname}
PROBLEMA: {conv.get("problem_id")}
SOLUÇÃO PROPOSTA: {conv.get("solution_summary", "")}
SCRIPT PROPOSTO ({conv.get("risk_level","?")} risk):
{conv.get("proposed_script","(sem script)") or "(manual — sem script automático)"}

ESTADO DA MÁQUINA NO MOMENTO DA DETECÇÃO:
  CPU: {cpu_then}%  |  Memória: {mem_then}%

ESTADO ACTUAL DA MÁQUINA (às {as_of}):
  CPU: {cpu_now}%  |  Memória: {mem_now}%

## Capacidades
Tens acesso a duas ferramentas:
- **agentless_run**: executa PowerShell remoto em qualquer máquina Windows do domínio via WinRM
- **query_ad**: consulta o Active Directory (read-only, sem aprovação)

Se o analista pedir para verificar algo, investigar logs, ver processos, ou obter dados em tempo real — **usa agentless_run** em vez de responder com suposições.

## Como responder
1. Se o analista pede investigação → usa as tools para recolher dados, depois responde com factos
2. Se o analista pergunta sobre estado → compara detecção vs actual, usa agentless_run se precisar de dados frescos
3. Se o analista aprova a acção → detecta intenção "approve_action"
4. Responde sempre em português, conciso (2-5 frases), técnico mas acessível

## Formato de resposta final
Quando tiveres toda a informação necessária, a tua ÚLTIMA resposta (sem tool calls) deve ser JSON:
{{
  "response": "<resposta para o analista>",
  "analyst_intent": "<question|approve_action|reject_action|request_info|close>",
  "execute_now": <true se approve_action E risk_level=low ou medium>,
  "close_conversation": <true se problema resolvido ou analista encerrou>
}}

Se não precisares de tools, responde directamente com o JSON acima."""

    # ── Parse da resposta ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_response(raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start < 0 or end <= start:
            return {"response": raw, "analyst_intent": "question", "execute_now": False}
        return json.loads(raw[start:end])
