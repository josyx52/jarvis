"""
Reset de senha root e consulta LAPS — directo, sem depender dos serviços
externos reset_root_rsi/laps_api (C:\\servicedesk_api).

reset_root_password: usa agentless.remote_executor.RemoteExecutor (WinRM,
Kerberos nativo da identidade do processo — mesma gMSA YOURDOMAIN\\sprd_passreset$
que o reset_root_rsi usa) para correr "net user" na workstation alvo.

get_laps_password: consulta o AD localmente (Get-ADComputer, módulo RSAT
ActiveDirectory já presente nesta máquina) — é exactamente o que o laps_api
fazia, só que sem o salto HTTP.
"""

import json
import os
import secrets
import string
import subprocess

from agentless.remote_executor import RemoteExecutor, RemoteExecutorError as _RemoteExecutorError

LOCAL_USER = os.getenv("ROOT_RESET_LOCAL_USER", "root")
PASSWORD_LENGTH = 14
_PASSWORD_CHARS = string.ascii_letters + string.digits + "!@#$%&*"
_AD_QUERY_TIMEOUT = 20


class RootResetClientError(Exception):
    pass


def _generate_password(length: int = PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(_PASSWORD_CHARS) for _ in range(length))


def reset_root_password(hostname: str) -> dict:
    """Reseta a senha da conta local (LOCAL_USER) na workstation via WinRM directo.
    Devolve {"ok": bool, "senha": str|None, "mensagem": str}."""
    password = _generate_password()
    script = f"net user {LOCAL_USER} '{password}'"

    try:
        executor = RemoteExecutor(host=hostname, machine_type="workstation")
        result = executor.run(script, timeout=30)
    except _RemoteExecutorError as e:
        raise RootResetClientError(f"Falha WinRM em {hostname}: {e}") from e

    if not result.get("ok"):
        raise RootResetClientError(
            result.get("stderr") or f"'net user' falhou em {hostname} (exit_code={result.get('exit_code')})"
        )

    return {"ok": True, "senha": password, "mensagem": "Senha alterada com sucesso via WinRM."}


def get_laps_password(hostname: str) -> dict:
    """Consulta a senha LAPS (ms-Mcs-AdmPwd) directamente no AD, sem passar pelo laps_api.
    Devolve {"ok": bool, "senha": str|None, "mensagem": str}.

    A identidade do processo (gMSA sprd_passreset$) não tem permissão de
    leitura sobre ms-Mcs-AdmPwd em todas as OUs (confirmado via ACL — só
    service-account-example, a conta usada pelo antigo laps_api, tem essa
    entrada). Por decisão explícita, usa-se essa conta como credencial
    alternativa só para esta query (LAPS_READ_USER/LAPS_READ_PASSWORD no
    .env) — nunca interpolada no texto do script, só passada via variável de
    ambiente do subprocesso, para não ficar visível na linha de comandos
    nem em logs do PowerShell. Sem estas variáveis definidas, cai para a
    identidade do processo (comportamento anterior)."""
    laps_user = os.getenv("LAPS_READ_USER", "")
    laps_password = os.getenv("LAPS_READ_PASSWORD", "")
    use_alt_credential = bool(laps_user and laps_password)

    credential_setup = ""
    get_adcomputer_cmd = f"Get-ADComputer '{hostname}' -Properties 'ms-Mcs-AdmPwd','ms-Mcs-AdmPwdExpirationTime'"
    if use_alt_credential:
        credential_setup = """
$__lapsSecPass = ConvertTo-SecureString $env:LAPS_READ_PASSWORD -AsPlainText -Force
$__lapsCred = New-Object System.Management.Automation.PSCredential($env:LAPS_READ_USER, $__lapsSecPass)
"""
        get_adcomputer_cmd += " -Credential $__lapsCred"

    ps_script = f"""
$ErrorActionPreference = 'Stop'
{credential_setup}
$c = {get_adcomputer_cmd}
if ($null -eq $c) {{
  [pscustomobject]@{{ Status = 'NO_HOST' }} | ConvertTo-Json -Compress
  exit 0
}}
$pwd = $c.'ms-Mcs-AdmPwd'
$exp = $c.'ms-Mcs-AdmPwdExpirationTime'
if ([string]::IsNullOrWhiteSpace($pwd)) {{
  [pscustomobject]@{{ Status = 'NO_PASSWORD'; ExpirationTime = $exp }} | ConvertTo-Json -Compress
}} else {{
  [pscustomobject]@{{ Status = 'OK'; Password = $pwd; ExpirationTime = $exp }} | ConvertTo-Json -Compress
}}
""".strip()

    env = os.environ.copy()
    if use_alt_credential:
        env["LAPS_READ_USER"] = laps_user
        env["LAPS_READ_PASSWORD"] = laps_password

    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=_AD_QUERY_TIMEOUT,
            encoding="utf-8", errors="replace",
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise RootResetClientError(f"Consulta LAPS excedeu o tempo limite para {hostname}") from e

    if proc.returncode != 0:
        raise RootResetClientError(proc.stderr.strip() or f"Get-ADComputer falhou para {hostname}")

    out = proc.stdout.strip()
    if not out:
        raise RootResetClientError(f"Get-ADComputer não devolveu resultado para {hostname}")

    try:
        laps = json.loads(out)
    except ValueError as e:
        raise RootResetClientError(f"Resposta inválida do AD para {hostname}") from e

    status = laps.get("Status")
    if status != "OK":
        motivo = {
            "NO_HOST": "Máquina não encontrada no AD.",
            "NO_PASSWORD": "LAPS não tem senha registada para esta máquina.",
        }.get(status, f"LAPS devolveu estado '{status}'.")
        raise RootResetClientError(motivo)

    return {"ok": True, "senha": laps.get("Password"), "mensagem": "Senha LAPS obtida com sucesso."}
