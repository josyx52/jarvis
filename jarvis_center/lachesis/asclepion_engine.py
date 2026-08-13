"""
Asclepion Systems — hardening / CIS Control Benchmarking.

Gera checklists de auditoria (LLM, estilo Clotho `generate_tools_catalog`) e
executa-as contra hosts via agentless (RemoteExecutor), avaliando cada check
com o LLM. As checklists são sempre geradas pelo modelo — nunca hardcoded.
"""

import json
import os
import re


_CHECKLIST_SYSTEM = """Você é o Asclepion, o motor de hardening/CIS Benchmarking do Jarvis Fates Engine.

Dado um sistema operativo e a descrição de um benchmark de segurança (ex: "CIS Microsoft
Windows Server 2022 Benchmark v2.0 Level 1"), gere uma checklist de verificações de
configuração para auditar uma máquina Windows.

REGRAS OBRIGATÓRIAS:
- Cada "check_script" deve ser um comando PowerShell ESTRITAMENTE DE LEITURA/DIAGNÓSTICO
  (ex: Get-*, auditpol /get, secedit /export, consultas de registo com Get-ItemProperty,
  consultas WMI/CIM com Get-CimInstance).
- NUNCA gere comandos que alterem o sistema: proibido Set-*, Remove-*, New-*, Add-*,
  Stop-*, Start-*, Restart-*, Install-*, Uninstall-*, "reg add", "reg delete", "net user",
  gestão de serviços/contas, ou qualquer escrita de registo/ficheiro.
- "remediation_hint" é apenas texto explicativo para o utilizador — nunca será executado.

Responda APENAS com JSON válido, sem markdown, no formato:
{
  "checks": [
    {
      "id": "<identificador curto, ex: win-2022-1.1.1>",
      "title": "<título curto>",
      "description": "<1-2 frases>",
      "severity": "low|medium|high",
      "check_script": "<comando PowerShell só-leitura>",
      "expected_result": "<descrição do resultado esperado/conforme>",
      "remediation_hint": "<sugestão de remediação, apenas texto>"
    }
  ]
}
Gere entre 8 e 15 checks relevantes para o benchmark descrito."""


_EVALUATE_SYSTEM = """Você é o Asclepion, o motor de hardening/CIS Benchmarking do Jarvis Fates Engine.

Dado um check de auditoria (título, descrição, resultado esperado) e o resultado da
execução do respectivo comando PowerShell numa máquina (stdout/stderr/exit code),
determine se a máquina está CONFORME ("pass": true) ou NÃO CONFORME ("pass": false)
com o resultado esperado, e escreva uma nota curta (1 frase, português) explicando
a conclusão.

Responda APENAS com JSON válido, sem markdown, no formato:
{"pass": true|false, "note": "<1 frase>"}"""


_SUMMARY_SYSTEM = """Você é o Asclepion, o motor de hardening/CIS Benchmarking do Jarvis Fates Engine.

Dado o nome do perfil, o benchmark, o alvo auditado, a pontuação obtida (% de checks
conformes) e a lista de resultados por check (título, severidade, conforme/não conforme,
nota), escreva um resumo executivo (3-6 frases, em português) destacando o estado geral
e os achados de maior severidade que não estão conformes.

Responda APENAS com texto simples, sem markdown."""


# Verbos/cmdlets que indicam alteração de estado — bloqueados em tempo de execução
# como defesa adicional às restrições do prompt de geração.
_MUTATING_PATTERN = re.compile(
    r"\b(Set-|Remove-|New-|Add-|Stop-|Start-|Restart-|Install-|Uninstall-|Clear-|"
    r"Disable-|Enable-|Rename-|Copy-Item|Move-Item|reg\s+add|reg\s+delete|net\s+user|"
    r"net\s+localgroup)",
    re.IGNORECASE,
)


def _client():
    from anthropic import AnthropicFoundry
    return AnthropicFoundry(
        api_key  = os.getenv("FOUNDRY_API_KEY",  ""),
        base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
    )


def _strip_fences(text: str) -> str:
    return re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()


def _normalize_check(check: dict) -> dict | None:
    if not isinstance(check, dict) or not check.get("check_script"):
        return None
    return {
        "id": str(check.get("id") or "")[:64],
        "title": str(check.get("title") or "")[:200],
        "description": str(check.get("description") or "")[:500],
        "severity": str(check.get("severity") or "medium").lower()[:10],
        "check_script": str(check.get("check_script") or "")[:2000],
        "expected_result": str(check.get("expected_result") or "")[:500],
        "remediation_hint": str(check.get("remediation_hint") or "")[:1000],
    }


def generate_checklist(profile: dict) -> dict:
    details = [
        f"Sistema operativo: {profile.get('os_type')}",
        f"Benchmark: {profile['benchmark']}",
    ]

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _CHECKLIST_SYSTEM,
        messages   = [{"role": "user", "content": "\n".join(details)}],
        max_tokens = 4096,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = _strip_fences(text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}

    checks = []
    for check in (data.get("checks") or []):
        normalized = _normalize_check(check)
        if normalized:
            checks.append(normalized)

    return {"checks": checks}


def evaluate_check(check: dict, exec_result: dict) -> dict:
    details = [
        f"Check: {check['title']}",
        f"Descrição: {check.get('description', '')}",
        f"Resultado esperado: {check.get('expected_result', '')}",
        f"Exit code: {exec_result.get('exit_code')}",
        f"Stdout: {exec_result.get('stdout', '')[:1500]}",
        f"Stderr: {exec_result.get('stderr', '')[:500]}",
    ]

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _EVALUATE_SYSTEM,
        messages   = [{"role": "user", "content": "\n".join(details)}],
        max_tokens = 200,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = _strip_fences(text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}

    return {
        "pass": bool(data.get("pass", False)),
        "note": str(data.get("note") or "")[:300],
    }


def summarize_run(profile: dict, results: list[dict], score: float, target: str) -> str:
    lines = [
        f"Perfil: {profile['name']}",
        f"Benchmark: {profile['benchmark']}",
        f"Alvo: {target}",
        f"Score: {score}%",
    ]
    for r in results:
        estado = "conforme" if r["pass"] else "NÃO CONFORME"
        lines.append(f"- [{r['severity']}] {r['title']}: {estado} — {r['note']}")

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _SUMMARY_SYSTEM,
        messages   = [{"role": "user", "content": "\n".join(lines)}],
        max_tokens = 400,
    )

    return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()[:1500]


def _run_check_script(target: str, script: str, machine_type: str, timeout_s: int = 45) -> dict:
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from agentless.remote_executor import RemoteExecutor
    executor = RemoteExecutor(host=target, machine_type=machine_type)
    return executor.run(script, timeout=timeout_s)


def _execute_one_check(profile: dict, target: str, check: dict) -> dict:
    if _MUTATING_PATTERN.search(check["check_script"]):
        return {
            "id": check["id"], "title": check["title"], "severity": check["severity"],
            "pass": False, "output": "", "note": "bloqueado: script potencialmente destrutivo",
        }

    try:
        exec_result = _run_check_script(target, check["check_script"], profile.get("machine_type", "workstation"))
    except Exception as e:
        return {
            "id": check["id"], "title": check["title"], "severity": check["severity"],
            "pass": False, "output": "", "note": f"erro de execução: {e}",
        }

    evaluation = evaluate_check(check, exec_result)
    return {
        "id": check["id"], "title": check["title"], "severity": check["severity"],
        "pass": evaluation["pass"], "output": exec_result.get("stdout", "")[:2000],
        "note": evaluation["note"],
    }


def run_profile(profile: dict, target: str, store) -> dict:
    """Executa a checklist do perfil contra um alvo. Devolve o registo asclepion_runs."""
    run = store.create_asclepion_run(profile["id"], target)

    checks = (profile.get("checklist") or {}).get("checks") or []
    try:
        results = [_execute_one_check(profile, target, check) for check in checks]
        passed = sum(1 for r in results if r["pass"])
        score = round((passed / len(results)) * 100, 2) if results else 0.0
        summary = summarize_run(profile, results, score, target)
        return store.update_asclepion_run(run["id"], "ok", score, results, summary, None)
    except Exception as e:
        return store.update_asclepion_run(run["id"], "error", None, None, None, str(e))
