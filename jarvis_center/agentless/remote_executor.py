"""
RemoteExecutor — executa PowerShell em máquinas remotas via WinRM.

Dois perfis de autenticação:

  workstation  → Invoke-Command do PowerShell com Kerberos nativo do Windows.
                 O processo corre como gMSA sprd_passreset$; o PowerShell usa
                 automaticamente a identidade do processo (sem credenciais explícitas).

  server       → pywinrm com NTLM e credenciais explícitas.
                 Env vars: AGENTLESS_SERVER_USER, AGENTLESS_SERVER_PASS

Mecanismo de vigilante (watchdog):
  Quando um script demora mais que o timeout local, o executor não assume falha.
  Em vez disso, o script principal é sempre envolvido num wrapper que escreve o seu
  resultado num ficheiro temp na máquina remota. Se o timeout local disparar, o
  vigilante faz ligações WinRM separadas para verificar se o ficheiro de resultado
  apareceu — e só reporta sucesso ou falha quando o resultado real chegar.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

import winrm


# Servidor — conta com password explícita
_SERVER_USER = os.getenv("AGENTLESS_SERVER_USER", "")
_SERVER_PASS = os.getenv("AGENTLESS_SERVER_PASS", "")

_DEFAULT_TIMEOUT      = 60
_DEFAULT_BULK_TIMEOUT = 120
_BULK_MAX_WORKERS     = 10
_WATCHDOG_POLL   = 10   # segundos entre cada verificação do vigilante
_WATCHDOG_MAX    = 300  # máximo de espera total do vigilante (5 min)

_DOMAIN_SUFFIX = os.getenv("AGENTLESS_DOMAIN_SUFFIX", "example.com")


_RESOLVE_TIMEOUT = 2  # segundos por tentativa DNS


def _resolve_host(host: str) -> str:
    """
    Tenta resolver o hostname. Se falhar, tenta NOME.<domínio> em maiúsculas
    e depois em minúsculas. Devolve o primeiro que resolver ou o original.
    Timeout curto para não bloquear operações bulk.
    """
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_RESOLVE_TIMEOUT)
    try:
        for candidate in (host, f"{host.upper()}.{_DOMAIN_SUFFIX}", f"{host.lower()}.{_DOMAIN_SUFFIX}"):
            try:
                socket.getaddrinfo(candidate, None)
                return candidate
            except OSError:
                pass
        return host
    finally:
        socket.setdefaulttimeout(old_timeout)


def _resolve_hosts_parallel(hosts: list[str], max_workers: int = 20) -> dict[str, str]:
    """Resolve vários hostnames em paralelo. Devolve {original: resolved}."""
    result = {}
    with ThreadPoolExecutor(max_workers=min(len(hosts), max_workers)) as ex:
        futures = {ex.submit(_resolve_host, h): h for h in hosts}
        for fut in as_completed(futures, timeout=max(10, _RESOLVE_TIMEOUT * 3)):
            original = futures[fut]
            try:
                result[original] = fut.result()
            except Exception:
                result[original] = original
    for h in hosts:
        if h not in result:
            result[h] = h
    return result


class RemoteExecutorError(Exception):
    pass


class RemoteExecutor:

    def __init__(self, host: str, machine_type: str = "workstation"):
        self.host         = _resolve_host(host)
        self.machine_type = machine_type

        if machine_type != "workstation":
            self._session = self._build_ntlm_session(self.host)
        else:
            self._session = None

    def _build_ntlm_session(self, host: str) -> winrm.Session:
        if not _SERVER_USER or not _SERVER_PASS:
            raise RemoteExecutorError(
                "Credenciais de servidor não configuradas. "
                "Defina AGENTLESS_SERVER_USER e AGENTLESS_SERVER_PASS."
            )
        return winrm.Session(
            target=host,
            auth=(_SERVER_USER, _SERVER_PASS),
            transport="ntlm",
            server_cert_validation="ignore",
            read_timeout_sec=_DEFAULT_TIMEOUT + 10,
            operation_timeout_sec=_DEFAULT_TIMEOUT,
        )

    def run(self, script: str, timeout: int = _DEFAULT_TIMEOUT) -> dict:
        """
        Executa um script PowerShell e devolve:
        { stdout, stderr, exit_code, ok, warning? }
        """
        if self.machine_type == "workstation":
            return self._run_invoke_command(script, timeout)
        return self._run_pywinrm(script)

    def run_bulk(self, hosts: list[str], script: str, timeout: int = _DEFAULT_BULK_TIMEOUT) -> dict:
        """
        Executa o mesmo script em várias máquinas, em paralelo, numa única
        chamada. Devolve:
        { results: {host: {stdout, stderr, exit_code, ok}}, unreachable: [hosts...] }

        Máquinas inacessíveis (offline, sem WinRM, fora do domínio, etc.) não
        fazem a chamada falhar — aparecem em "unreachable".
        """
        if self.machine_type == "workstation":
            return self._run_bulk_invoke_command(hosts, script, timeout)
        return self._run_bulk_pywinrm(hosts, script, timeout)

    # ── Invoke-Command com vigilante ──────────────────────────────────────────

    def _run_invoke_command(self, script: str, timeout: int) -> dict:
        """
        Envolve o script num wrapper que escreve o resultado num ficheiro temp
        na máquina remota. Se o timeout local disparar, o vigilante verifica
        periodicamente se o ficheiro apareceu e lê o resultado real.
        """
        job_id      = uuid.uuid4().hex[:16]
        result_file = f"C:\\Windows\\Temp\\jarvis_{job_id}.json"

        wrapped = self._wrap_script(script, result_file)
        ps_cmd  = (
            f"Invoke-Command -ComputerName '{self.host}' "
            f"-ScriptBlock {{ {wrapped} }}"
        )

        proc = subprocess.Popen(
            ["powershell.exe", "-NonInteractive", "-NoProfile", "-Command", ps_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            # Completou dentro do timeout — lê o ficheiro de resultado
            return self._read_result_file(result_file) or {
                "stdout":    stdout.strip(),
                "stderr":    stderr.strip(),
                "exit_code": proc.returncode,
                "ok":        proc.returncode == 0,
            }

        except subprocess.TimeoutExpired:
            # Timeout local — a execução remota pode ainda estar em curso.
            # Mata o processo local e activa o vigilante.
            proc.kill()
            proc.communicate()
            return self._watchdog(result_file, timeout)

        except Exception as e:
            proc.kill()
            raise RemoteExecutorError(f"Invoke-Command falhou em {self.host}: {e}") from e

    @staticmethod
    def _wrap_script(script: str, result_file: str) -> str:
        """
        Envolve o script do utilizador num bloco try/catch que captura
        stdout, stderr e exit_code e escreve tudo no ficheiro de resultado.
        """
        # Escapa as aspas do caminho do ficheiro
        rf = result_file.replace("'", "''")
        return f"""
$__stdout = @()
$__stderr = @()
$__code   = 0
try {{
    & {{
        {script}
    }} 2>&1 | ForEach-Object {{
        if ($_ -is [System.Management.Automation.ErrorRecord]) {{ $__stderr += $_.ToString() }}
        else {{ $__stdout += $_ }}
    }}
}} catch {{
    $__stderr += $_.ToString()
    $__code   = 1
}}
$__stdoutText = if ($__stdout.Count -gt 0) {{ ($__stdout | Out-String -Width 4096).Trim() }} else {{ "" }}
@{{
    ok        = ($__code -eq 0)
    exit_code = $__code
    stdout    = $__stdoutText
    stderr    = ($__stderr -join \"`n\").Trim()
}} | ConvertTo-Json -Compress | Out-File -FilePath '{rf}' -Encoding utf8 -Force
""".strip()

    def _read_result_file(self, result_file: str) -> dict | None:
        """
        Lê o ficheiro de resultado da máquina remota via uma ligação WinRM separada.
        Apaga o ficheiro depois de ler. Devolve None se o ficheiro não existir.
        """
        rf  = result_file.replace("'", "''")
        ps  = (
            f"if (Test-Path '{rf}') {{"
            f"  (Get-Content '{rf}' -Raw -Encoding utf8).Trim();"
            f"  Remove-Item '{rf}' -Force -ErrorAction SilentlyContinue"
            f"}} else {{ Write-Output 'JARVIS_NOT_DONE' }}"
        )
        try:
            r = subprocess.run(
                ["powershell.exe", "-NonInteractive", "-NoProfile", "-Command",
                 f"Invoke-Command -ComputerName '{self.host}' -ScriptBlock {{ {ps} }}"],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
            out = r.stdout.strip()
            if not out or out == "JARVIS_NOT_DONE":
                return None
            return json.loads(out)
        except Exception:
            return None

    def _watchdog(self, result_file: str, original_timeout: int) -> dict:
        """
        Vigilante: verifica periodicamente se o ficheiro de resultado apareceu
        na máquina remota. Espera no máximo _WATCHDOG_MAX segundos.
        Reporta o resultado real quando disponível — nunca assume sucesso.
        """
        deadline = time.time() + _WATCHDOG_MAX
        attempt  = 0

        while time.time() < deadline:
            time.sleep(_WATCHDOG_POLL)
            attempt += 1
            result = self._read_result_file(result_file)
            if result is not None:
                # Resultado real encontrado — adiciona nota de contexto
                result["watchdog"] = (
                    f"Timeout local ({original_timeout}s) expirou durante execução. "
                    f"Resultado confirmado pelo vigilante após {attempt * _WATCHDOG_POLL}s adicionais."
                )
                return result

        # Nunca apareceu o ficheiro — verdadeiro timeout ou falha de ligação
        return {
            "ok":        False,
            "exit_code": -2,
            "stdout":    "",
            "stderr":    (
                f"Vigilante: sem resultado após {_WATCHDOG_MAX}s. "
                f"A execução pode ainda estar em curso ou falhou silenciosamente em {self.host}. "
                f"Verifica manualmente o estado da máquina."
            ),
        }

    # ── Bulk: Invoke-Command multi-host (workstations) ────────────────────────

    def _run_bulk_invoke_command(self, hosts: list[str], script: str, timeout: int) -> dict:
        """
        Corre o script em várias máquinas numa única chamada
        Invoke-Command -ComputerName @(...) — o PowerShell paraleliza
        nativamente. Máquinas inacessíveis não abortam as restantes,
        ficam de fora de $__out e são reportadas em "unreachable".
        """
        resolved_all  = _resolve_hosts_parallel(hosts)
        resolved_map  = {resolved_all[h].lower(): h for h in hosts}
        resolved_list = [resolved_all[h] for h in hosts]

        host_array = ",".join(f"'{h}'" for h in resolved_list)
        wrapped    = self._wrap_bulk_script(script)
        ps_cmd = (
            f"$__opt = New-PSSessionOption -OpenTimeout 15000 -OperationTimeout 30000; "
            f"$__out = Invoke-Command -ComputerName @({host_array}) -SessionOption $__opt "
            f"-ErrorAction SilentlyContinue -ScriptBlock {{ {wrapped} }} | "
            f"Select-Object PSComputerName,ok,exit_code,stdout,stderr; "
            f"ConvertTo-Json -InputObject @($__out) -Compress -Depth 5"
        )

        try:
            r = subprocess.run(
                ["powershell.exe", "-NonInteractive", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {
                "results": {},
                "unreachable": list(hosts),
                "warning": (
                    f"Timeout ({timeout}s) atingido antes de obter resposta de qualquer "
                    f"máquina. A execução pode ainda estar em curso nalgumas delas."
                ),
            }

        results = {}
        out = (r.stdout or "").strip()
        if out:
            try:
                data = json.loads(out)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    pc       = str(item.get("PSComputerName", "")).lower()
                    original = resolved_map.get(pc, item.get("PSComputerName", ""))
                    results[original] = {
                        "stdout":    item.get("stdout", ""),
                        "stderr":    item.get("stderr", ""),
                        "exit_code": item.get("exit_code", -1),
                        "ok":        bool(item.get("ok", False)),
                    }
            except Exception:
                pass

        unreachable = [h for h in hosts if h not in results]
        return {"results": results, "unreachable": unreachable}

    @staticmethod
    def _wrap_bulk_script(script: str) -> str:
        """
        Envolve o script do utilizador num try/catch que devolve um objecto
        compacto (ok, exit_code, stdout, stderr). Usado pelo Invoke-Command
        multi-host, onde o PowerShell adiciona automaticamente PSComputerName
        a cada resultado.
        """
        return f"""
$__stdout = @()
$__stderr = @()
$__code   = 0
try {{
    & {{
        {script}
    }} 2>&1 | ForEach-Object {{
        if ($_ -is [System.Management.Automation.ErrorRecord]) {{ $__stderr += $_.ToString() }}
        else {{ $__stdout += $_ }}
    }}
}} catch {{
    $__stderr += $_.ToString()
    $__code   = 1
}}
$__stdoutText = if ($__stdout.Count -gt 0) {{ ($__stdout | Out-String -Width 4096).Trim() }} else {{ "" }}
[PSCustomObject]@{{
    ok        = ($__code -eq 0)
    exit_code = $__code
    stdout    = $__stdoutText
    stderr    = ($__stderr -join \"`n\").Trim()
}}
""".strip()

    # ── pywinrm (servidores NTLM) ─────────────────────────────────────────────

    def _run_pywinrm(self, script: str) -> dict:
        try:
            result = self._session.run_ps(script)
            return {
                "stdout":    result.std_out.decode("utf-8", errors="replace").strip(),
                "stderr":    result.std_err.decode("utf-8", errors="replace").strip(),
                "exit_code": result.status_code,
                "ok":        result.status_code == 0,
            }
        except Exception as e:
            err_str = str(e)
            if "10054" in err_str or "ConnectionReset" in err_str or "forcibly closed" in err_str.lower():
                raise RemoteExecutorError(
                    f"Output demasiado grande para WinRM em {self.host}. "
                    f"Limite o output do script (use Select-Object, -First N, Out-File, etc.)"
                ) from e
            raise RemoteExecutorError(f"WinRM falhou em {self.host}: {e}") from e

    # ── Bulk: pywinrm multi-host (servidores) ─────────────────────────────────

    def _run_bulk_pywinrm(self, hosts: list[str], script: str, timeout: int) -> dict:
        """
        Corre o script em várias máquinas via pywinrm/NTLM, uma sessão por
        máquina, em paralelo. Máquinas que falhem a ligação ou não respondam
        dentro do timeout global ficam em "unreachable".
        """
        resolved = _resolve_hosts_parallel(hosts)

        def _one(host: str):
            try:
                session = self._build_ntlm_session(resolved.get(host, host))
                result  = session.run_ps(script)
                return host, {
                    "stdout":    result.std_out.decode("utf-8", errors="replace").strip(),
                    "stderr":    result.std_err.decode("utf-8", errors="replace").strip(),
                    "exit_code": result.status_code,
                    "ok":        result.status_code == 0,
                }
            except Exception:
                return host, None

        results     = {}
        unreachable = []
        with ThreadPoolExecutor(max_workers=min(len(hosts), _BULK_MAX_WORKERS)) as ex:
            future_to_host = {ex.submit(_one, h): h for h in hosts}
            try:
                for fut in as_completed(future_to_host, timeout=timeout):
                    host, result = fut.result()
                    if result is not None:
                        results[host] = result
                    else:
                        unreachable.append(host)
            except FuturesTimeoutError:
                for fut, host in future_to_host.items():
                    if not fut.done():
                        unreachable.append(host)

        return {"results": results, "unreachable": unreachable}

    # ── Ping ──────────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """Verifica se a ligação WinRM está funcional."""
        try:
            r = self.run("echo ok", timeout=15)
            return r["ok"]
        except Exception as e:
            raise RemoteExecutorError(f"Ping falhou em {self.host}: {e}") from e
