"""
AgentlessChat — responde a perguntas sobre máquinas sem agente via WinRM.

Fluxo:
  1. Recebe pergunta em linguagem natural + máquina alvo
  2. Claude gera o script PowerShell adequado
  3. Executa na máquina via RemoteExecutor (WinRM + Kerberos/NTLM)
  4. Claude interpreta o output e responde em português

Suporta sessões: se session_id for fornecido, mantém histórico de
perguntas/respostas anteriores para contexto na conversa.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from agentless.remote_executor import RemoteExecutor, RemoteExecutorError
from brain.foundry_client import FoundryClient


_SCRIPT_PROMPT = """És um especialista em Windows PowerShell e administração de sistemas.
A tua tarefa é gerar UM ÚNICO script PowerShell que responde a TODAS as perguntas do analista sobre uma máquina Windows.

REGRAS:
- Gera APENAS o script PowerShell — sem explicações, sem markdown, sem ```
- O script deve ser seguro: apenas leitura (Get-*, netstat, whoami, qwinsta, etc.) — nunca modificar o sistema
- Agrupa TODAS as perguntas num único script com secções separadas por Write-Output "===SECÇÃO==="
- Cada secção deve ter o seu header e output limpo
- O output total não deve exceder 300 linhas
- Se alguma pergunta não puder ser respondida com leitura segura, ignora essa parte

Máquina: {machine}
Tipo: {machine_type}
{history_section}Perguntas do analista: {question}

Script PowerShell único que responde a tudo:"""


_ANSWER_PROMPT = """És um analista de sistemas a responder a um colega sobre o estado de uma máquina Windows.
Responde sempre em Português. Sê directo, claro e usa linguagem simples.
Se os dados mostram algo preocupante, destaca-o.
Se o output estiver vazio ou com erros, explica o que isso pode significar.

Máquina: {machine}
{history_section}Pergunta actual: {question}
Script executado: {script}
Output da máquina:
{output}

Responde à pergunta com base no output acima. Máximo 5 frases, sem jargão desnecessário."""


# Sessões em memória com LRU — max 100 sessões activas
_sessions: OrderedDict[str, list[dict]] = OrderedDict()
_sessions_lock = threading.Lock()
_MAX_SESSIONS = 100
_MAX_HISTORY_PER_SESSION = 10


def _get_session_history(session_id: str | None) -> list[dict]:
    if not session_id:
        return []
    with _sessions_lock:
        history = _sessions.get(session_id, [])
        if session_id in _sessions:
            _sessions.move_to_end(session_id, last=True)
        return list(history)


def _add_to_session(session_id: str | None, question: str, answer: str, machine: str):
    if not session_id:
        return
    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = []
        _sessions[session_id].append({
            "question": question,
            "answer": answer,
            "machine": machine,
        })
        if len(_sessions[session_id]) > _MAX_HISTORY_PER_SESSION:
            _sessions[session_id] = _sessions[session_id][-_MAX_HISTORY_PER_SESSION:]
        _sessions.move_to_end(session_id, last=True)
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)


def _format_history(history: list[dict]) -> str:
    if not history:
        return ""
    lines = ["Contexto de perguntas anteriores nesta sessão:"]
    for i, h in enumerate(history, 1):
        lines.append(f"  {i}. Pergunta: {h['question'][:200]}")
        lines.append(f"     Resposta: {h['answer'][:300]}")
    return "\n".join(lines) + "\n\n"


class AgentlessChat:

    def __init__(self):
        self._llm = FoundryClient()

    def ask(
        self,
        machine: str,
        question: str,
        machine_type: str = "workstation",
        session_id: str | None = None,
    ) -> dict:
        """
        Responde a uma pergunta sobre uma máquina sem agente.

        Se session_id for fornecido, mantém histórico de perguntas/respostas
        anteriores para dar contexto ao Claude nas perguntas seguintes.
        """
        if not session_id:
            session_id = str(uuid.uuid4())[:16]
        asked_at = datetime.now(timezone.utc).isoformat()

        history = _get_session_history(session_id)
        history_section = _format_history(history)

        # 1. Gerar script PowerShell
        try:
            script = self._llm.generate(
                _SCRIPT_PROMPT.format(
                    machine=machine,
                    machine_type=machine_type,
                    question=question,
                    history_section=history_section,
                ),
                temperature=0.1,
                max_tokens=1024,
            ).strip()
        except Exception as e:
            return self._error(session_id, machine, question, asked_at,
                               f"Falha ao gerar script: {e}")

        if "PERGUNTA_NAO_SUPORTADA" in script:
            return self._error(session_id, machine, question, asked_at,
                               "Pergunta não suportada — só são permitidas operações de leitura.")

        # 2. Executar na máquina via WinRM
        try:
            executor = RemoteExecutor(host=machine, machine_type=machine_type)
            result   = executor.run(script, timeout=45)
            stdout = (result.get("stdout") or "").strip()
            stderr = (result.get("stderr") or "").strip()
            if stdout and stderr:
                raw_output = stdout + "\n\n[STDERR]\n" + stderr
            elif stdout:
                raw_output = stdout
            elif stderr:
                raw_output = stderr
            else:
                raw_output = "(sem output)"
        except RemoteExecutorError as e:
            return self._error(session_id, machine, question, asked_at, str(e))
        except Exception as e:
            return self._error(session_id, machine, question, asked_at,
                               f"Erro de ligação: {e}")

        # 3. Interpretar resultado
        output_for_llm = raw_output[:10000]
        try:
            answer = self._llm.generate(
                _ANSWER_PROMPT.format(
                    machine=machine,
                    question=question,
                    script=script,
                    output=output_for_llm,
                    history_section=history_section,
                ),
                temperature=0.2,
                max_tokens=512,
            ).strip()
        except Exception as e:
            answer = f"Output recolhido mas falha ao interpretar: {e}\n\n{raw_output[:1000]}"

        _add_to_session(session_id, question, answer, machine)

        return {
            "session_id": session_id,
            "machine":    machine,
            "question":   question,
            "script":     script,
            "raw_output": raw_output[:10000],
            "answer":     answer,
            "ok":         True,
            "error":      None,
            "asked_at":   asked_at,
        }

    def _error(
        self, session_id: str, machine: str, question: str,
        asked_at: str, error: str,
    ) -> dict:
        return {
            "session_id": session_id,
            "machine":    machine,
            "question":   question,
            "script":     "",
            "raw_output": "",
            "answer":     f"Não foi possível responder: {error}",
            "ok":         False,
            "error":      error,
            "asked_at":   asked_at,
        }
