"""
SecurityInvestigator — orquestra a investigação de acesso administrativo agentless.

Fluxo:
  1. Conecta à máquina via WinRM (RemoteExecutor)
  2. Recolhe evidências via SecurityCollector (a partir de credential_time)
  3. Envia ao Claude (FoundryClient) com prompt NIST CSF
  4. Devolve relatório de texto estruturado
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from agentless.remote_executor import RemoteExecutor, RemoteExecutorError
from agentless.collectors.security_collector import SecurityCollector
from brain.foundry_client import FoundryClient


AccessMethod = Literal["laps", "password_reset"]


class SecurityInvestigator:

    def __init__(self):
        self._llm = FoundryClient()

    def investigate(
        self,
        machine: str,
        username: str,
        reason: str,
        access_method: AccessMethod,
        credential_time: datetime,
        machine_type: str = "workstation",
    ) -> dict:
        """
        Devolve:
        {
          "investigation_id": str,
          "machine": str,
          "username": str,
          "credential_time": str,
          "access_method": str,
          "stated_reason": str,
          "verdict": "CONFORME" | "SUSPEITO" | "NÃO CONFORME",
          "report": str,           # texto completo NIST
          "evidence_summary": dict, # contagens de evidências
          "error": str | None,
          "investigated_at": str,
        }
        """
        investigation_id = self._make_id(machine, username, credential_time)
        investigated_at  = datetime.now(timezone.utc).isoformat()

        # 1. Conectar
        try:
            executor = RemoteExecutor(host=machine, machine_type=machine_type)
            if not executor.ping():
                return self._error_result(
                    investigation_id, machine, username, credential_time,
                    access_method, reason, investigated_at,
                    "Não foi possível estabelecer ligação WinRM com a máquina."
                )
        except RemoteExecutorError as e:
            return self._error_result(
                investigation_id, machine, username, credential_time,
                access_method, reason, investigated_at, str(e)
            )

        # 2. Recolher evidências
        collector = SecurityCollector(executor)
        evidence  = collector.collect(username=username, credential_time=credential_time)

        # 3. Resumo de evidências (para retornar à API)
        evidence_summary = {
            "logon_events":        len(evidence.get("logon_events", [])),
            "account_events":      len(evidence.get("account_events", [])),
            "process_events":      len(evidence.get("process_events", [])),
            "prefetch":            len(evidence.get("prefetch", [])),
            "service_events":      len(evidence.get("service_events", [])),
            "task_events":         len(evidence.get("task_events", [])),
            "privilege_events":    len(evidence.get("privilege_events", [])),
            "file_events":         len(evidence.get("file_events", [])),
            "registry_events":     len(evidence.get("registry_events", [])),
            "msi_install_events":  len(evidence.get("msi_install_events", [])),
            "running_processes":   len(evidence.get("running_processes", [])),
            "software_installed":  len(evidence.get("software_installed", [])),
            "recent_files":        len(evidence.get("recent_files", [])),
            "powershell_history":  len(evidence.get("powershell_history", [])),
        }

        # 4. Prompt + Claude
        prompt = self._build_prompt(
            machine=machine,
            username=username,
            reason=reason,
            access_method=access_method,
            credential_time=credential_time,
            evidence=evidence,
        )

        try:
            report_text = self._llm.generate(prompt, temperature=0.1, max_tokens=4096)
        except Exception as e:
            report_text = f"[ERRO NA GERAÇÃO DO RELATÓRIO VIA LLM: {e}]"

        # 5. Extrair veredicto do relatório
        verdict = self._extract_verdict(report_text)

        return {
            "investigation_id": investigation_id,
            "machine":          machine,
            "username":         username,
            "credential_time":  credential_time.isoformat(),
            "access_method":    access_method,
            "stated_reason":    reason,
            "verdict":          verdict,
            "report":           report_text,
            "evidence_summary": evidence_summary,
            "error":            None,
            "investigated_at":  investigated_at,
        }

    # ---- Prompt ----

    def _build_prompt(
        self,
        machine: str,
        username: str,
        reason: str,
        access_method: str,
        credential_time: datetime,
        evidence: dict,
    ) -> str:
        since = credential_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        method_label = "LAPS" if access_method == "laps" else "Reset de Senha"

        logon_events   = evidence.get("logon_events", [])
        account_events = evidence.get("account_events", [])
        proc_events    = evidence.get("process_events", [])
        prefetch       = evidence.get("prefetch", [])
        svc_events     = evidence.get("service_events", [])
        task_events    = evidence.get("task_events", [])
        priv_events    = evidence.get("privilege_events", [])
        file_events    = evidence.get("file_events", [])
        reg_events     = evidence.get("registry_events", [])
        msi_events     = evidence.get("msi_install_events", [])
        sw_installed   = evidence.get("software_installed", [])
        recent_files   = evidence.get("recent_files", [])
        ps_history     = evidence.get("powershell_history", [])
        net_conns      = evidence.get("network_connections", [])

        def fmt(items: list, limit: int = 30) -> str:
            return json.dumps(items[:limit], ensure_ascii=False, indent=2)

        return f"""És um investigador de segurança sénior a redigir um relatório para um gestor de segurança.
O teu objectivo é explicar claramente o que aconteceu nesta máquina durante o período de acesso privilegiado.

REGRAS DE ESCRITA:
- Escreve sempre em Português, com linguagem clara e directa
- Evita jargão técnico desnecessário — se usares termos técnicos, explica brevemente o que significam
- Sê factual: diz o que as evidências mostram, não o que podes supor
- Quando não há evidências de algo, diz claramente "não há registo de..."
- O relatório deve ser legível por um gestor sem formação técnica profunda

CONTEXTO DO ACESSO
Máquina:          {machine}
Utilizador:       {username}
Método de acesso: {method_label}
Credencial gerada: {since}
Motivo declarado: {reason}

EVIDÊNCIAS RECOLHIDAS

[ENTRADAS E SAÍDAS DA MÁQUINA]
{fmt(logon_events, 50)}

[ALTERAÇÕES À CONTA (mudanças de senha, conta modificada)]
IMPORTANTE: cada evento tem o campo "performed_by":
  - "investigated_user" = acção feita PELO utilizador investigado ({username})
  - "other_actor" = acção feita por outro utilizador/sistema SOBRE a conta de {username}
Só atribui acções ao utilizador investigado quando performed_by = "investigated_user".
{fmt(account_events, 30)}

[PROGRAMAS EXECUTADOS - registo do sistema]
{fmt(proc_events, 50)}

[PROGRAMAS EXECUTADOS - ficheiros prefetch (sem necessidade de configuração especial)]
{fmt(prefetch, 50)}

[COMANDOS POWERSHELL EXECUTADOS]
{fmt(ps_history, 50)}

[SERVIÇOS INSTALADOS OU MODIFICADOS]
{fmt(svc_events, 30)}

[TAREFAS AGENDADAS]
{fmt(task_events, 20)}

[INSTALAÇÕES VIA WINDOWS INSTALLER]
{fmt(msi_events, 30)}

[SOFTWARE INSTALADO DURANTE A SESSÃO]
{fmt(sw_installed, 30)}

[FICHEIROS EXECUTÁVEIS CRIADOS OU COPIADOS]
{fmt(recent_files, 30)}

[ALTERAÇÕES AO REGISTO DO SISTEMA]
{fmt(reg_events, 20)}

[FICHEIROS ACEDIDOS]
{fmt(file_events, 20)}

[LIGAÇÕES DE REDE ACTIVAS (com nome do processo)]
{fmt(net_conns, 30)}

TAREFA:
Analisa se o que foi feito na máquina é consistente com o motivo declarado: "{reason}"

Escreve o relatório com EXACTAMENTE esta estrutura — não alteres os títulos:

RELATÓRIO DE INVESTIGAÇÃO DE ACESSO PRIVILEGIADO
Máquina: {machine} | Utilizador: {username} | Método: {method_label}
Período investigado: a partir de {since}
Motivo declarado: {reason}

VEREDICTO: [CONFORME / SUSPEITO / NÃO CONFORME]

O QUE ACONTECEU
[Narrativa directa em 3-5 frases. Explica o que o utilizador fez na máquina durante o período de acesso,
como se estivesses a contar a história a um colega. Refere o que foi instalado, executado, acedido.
Se não há evidências de acção, diz isso claramente.]

LINHA DE TEMPO
[Lista cronológica de TODAS as acções com hora exacta. Formato obrigatório para cada linha:
HH:MM:SS — [o que aconteceu, em linguagem simples]
Ordena do mais antigo para o mais recente. Inclui TUDO: logins, programas abertos, instalações,
alterações de conta, comandos PowerShell, ficheiros criados. Sem excepções.]

O MOTIVO DECLARADO BATE CERTO?
[Compara directamente o que foi declarado com o que foi encontrado nas evidências.
Indica explicitamente se há provas que confirmam ou contradizem o motivo.]

O QUE CHAMOU A ATENÇÃO
[Lista apenas o que é genuinamente preocupante ou inesperado. Se não há nada, escreve "Nada a assinalar."
Para cada item: explica o que é, porque é preocupante, e o que pode significar — em linguagem simples.]

O QUE RECOMENDAMOS
[Acções concretas e prioritizadas. Indica quem deve fazer o quê.
Se tudo estiver conforme, escreve "Sem acções necessárias."]
"""

    # ---- Helpers ----

    def _extract_verdict(self, report: str) -> str:
        upper = report.upper()
        if "NÃO CONFORME" in upper or "NAO CONFORME" in upper:
            return "NÃO CONFORME"
        if "SUSPEITO" in upper:
            return "SUSPEITO"
        if "CONFORME" in upper:
            return "CONFORME"
        return "INDETERMINADO"

    def _make_id(self, machine: str, username: str, credential_time: datetime) -> str:
        ts = credential_time.strftime("%Y%m%d%H%M%S")
        slug = f"{machine}-{username}-{ts}".lower().replace(" ", "-")
        return slug[:80]

    def _error_result(
        self,
        investigation_id: str,
        machine: str,
        username: str,
        credential_time: datetime,
        access_method: str,
        reason: str,
        investigated_at: str,
        error: str,
    ) -> dict:
        return {
            "investigation_id": investigation_id,
            "machine":          machine,
            "username":         username,
            "credential_time":  credential_time.isoformat(),
            "access_method":    access_method,
            "stated_reason":    reason,
            "verdict":          "ERRO",
            "report":           f"Investigação falhada: {error}",
            "evidence_summary": {},
            "error":            error,
            "investigated_at":  investigated_at,
        }
