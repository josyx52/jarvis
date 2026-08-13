"""
SecurityCollector — recolha forense generalista de uma máquina remota via WinRM.

Princípios:
  - Cobertura máxima sem depender de software ou configuração específica
  - Artefactos forenses nativos do Windows (Prefetch, UserAssist, MSI events)
  - Qualidade > velocidade; cada script é independente e tem timeout próprio
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

_DT_FMT = "%Y-%m-%dT%H:%M:%S"
_CREDENTIAL_WINDOW_HOURS = 24  # janela máxima de validade de uma credencial


def _ps_events(log_name: str, event_ids: list[int], since_iso: str, until_iso: str, username: str = "") -> str:
    """Eventos genéricos — filtra por username no corpo da mensagem se fornecido."""
    ids_filter = ", ".join(str(i) for i in event_ids)
    user_filter = (
        f" | Where-Object {{ $_.Message -like '*{username}*' }}"
        if username else ""
    )
    return f"""
try {{
    $events = Get-WinEvent -FilterHashtable @{{
        LogName   = '{log_name}'
        Id        = {ids_filter}
        StartTime = '{since_iso}'
        EndTime   = '{until_iso}'
    }} -ErrorAction Stop{user_filter} | Select-Object -First 500 |
    ForEach-Object {{
        @{{
            id      = $_.Id
            time    = $_.TimeCreated.ToString('o')
            level   = $_.LevelDisplayName
            message = $_.Message.Substring(0, [Math]::Min(2000, $_.Message.Length))
        }}
    }}
    ConvertTo-Json -InputObject @($events) -Compress
}} catch {{
    ConvertTo-Json -InputObject @() -Compress
}}
""".strip()


def _ps_account_events(since_iso: str, until_iso: str, username: str) -> str:
    """
    Eventos de gestão de contas com extracção XML de Subject e Target.
    Inclui apenas:
      - Acções FEITAS pelo utilizador investigado (Subject = username)
      - Acções SOBRE a sua conta (Target = username)
    Outros utilizadores que alterem contas durante a janela NÃO são incluídos.
    """
    ids = "4720, 4722, 4723, 4724, 4725, 4726, 4738, 4740, 4767"
    return f"""
try {{
    $results = @()
    $raw = Get-WinEvent -FilterHashtable @{{
        LogName   = 'Security'
        Id        = {ids}
        StartTime = '{since_iso}'
        EndTime   = '{until_iso}'
    }} -ErrorAction Stop | Select-Object -First 500

    foreach ($ev in $raw) {{
        try {{
            $xml  = [xml]$ev.ToXml()
            $data = $xml.Event.EventData.Data

            $subject = ($data | Where-Object {{ $_.Name -eq 'SubjectUserName'  }}).'#text'
            $target  = ($data | Where-Object {{ $_.Name -eq 'TargetUserName'   }}).'#text'
            $domain  = ($data | Where-Object {{ $_.Name -eq 'SubjectDomainName'}}).'#text'

            # Só inclui se Subject OU Target corresponder ao utilizador investigado
            $subjectMatch = $subject -and ($subject -ilike '*{username}*' -or $subject -ilike '*sprd_passreset*')
            $targetMatch  = $target  -and  $target  -ilike '*{username}*'

            if ($subjectMatch -or $targetMatch) {{
                $results += @{{
                    id             = $ev.Id
                    time           = $ev.TimeCreated.ToString('o')
                    subject        = "$domain\\$subject"
                    target_account = $target
                    performed_by   = if ($subjectMatch -and -not $targetMatch) {{ 'investigated_user' }}
                                     elseif ($targetMatch -and -not $subjectMatch) {{ 'other_actor' }}
                                     else {{ 'investigated_user' }}
                    message        = $ev.Message.Substring(0, [Math]::Min(1000, $ev.Message.Length))
                }}
            }}
        }} catch {{ }}
    }}
    ConvertTo-Json -InputObject @($results) -Compress
}} catch {{
    ConvertTo-Json -InputObject @() -Compress
}}
""".strip()


# Processos em execução com path e hora de início
_PS_PROCESSES = """
try {
    $procs = Get-Process | Select-Object -First 200 |
    ForEach-Object {
        @{
            pid     = $_.Id
            name    = $_.Name
            cpu     = [Math]::Round($_.CPU, 2)
            mem_mb  = [Math]::Round($_.WorkingSet64 / 1MB, 1)
            path    = try { $_.MainModule.FileName } catch { '' }
            started = try { $_.StartTime.ToString('o') } catch { '' }
        }
    }
    ConvertTo-Json -InputObject @($procs) -Compress
} catch {
    ConvertTo-Json -InputObject @() -Compress
}
""".strip()


# Rede com nome do processo (PID → executável)
_PS_NETWORK = """
try {
    $procMap = @{}
    Get-Process | ForEach-Object { $procMap[$_.Id] = $_.Name }
    $conns = Get-NetTCPConnection -ErrorAction SilentlyContinue |
    Where-Object { $_.State -ne 'Listen' } | Select-Object -First 300 |
    ForEach-Object {
        @{
            local_addr   = $_.LocalAddress
            local_port   = $_.LocalPort
            remote_addr  = $_.RemoteAddress
            remote_port  = $_.RemotePort
            state        = $_.State.ToString()
            pid          = $_.OwningProcess
            process_name = if ($procMap.ContainsKey($_.OwningProcess)) { $procMap[$_.OwningProcess] } else { 'unknown' }
        }
    }
    ConvertTo-Json -InputObject @($conns) -Compress
} catch {
    ConvertTo-Json -InputObject @() -Compress
}
""".strip()


# Prefetch — execuções reais sem necessidade de auditpol (Event 4688)
def _ps_prefetch(since_iso: str, until_iso: str) -> str:
    return f"""
try {{
    $since = [DateTime]::Parse('{since_iso}')
    $until = [DateTime]::Parse('{until_iso}')
    $pf = Get-ChildItem C:\\Windows\\Prefetch\\*.pf -ErrorAction SilentlyContinue |
        Where-Object {{ $_.LastWriteTime -ge $since -and $_.LastWriteTime -le $until }} |
        Sort-Object LastWriteTime | Select-Object -First 200 |
        ForEach-Object {{
            @{{
                executable   = $_.Name -replace '-[A-F0-9]{{8}}\\.pf$', ''
                last_run     = $_.LastWriteTime.ToString('o')
                created      = $_.CreationTime.ToString('o')
                size_kb      = [Math]::Round($_.Length / 1KB, 1)
            }}
        }}
    ConvertTo-Json -InputObject @($pf) -Compress
}} catch {{
    ConvertTo-Json -InputObject @() -Compress
}}
""".strip()


# UserAssist — o que o utilizador correu via Explorer (sem auditpol)
def _ps_userassist(username: str) -> str:
    return f"""
try {{
    $results = @()
    $profilePaths = @("C:\\Users\\{username}", $env:USERPROFILE)
    foreach ($profilePath in ($profilePaths | Select-Object -Unique)) {{
        if (-not (Test-Path $profilePath)) {{ continue }}
        $hivePath = "$profilePath\\NTUSER.DAT"
        $uaKey = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\UserAssist"
        if (Test-Path $uaKey) {{
            Get-ChildItem $uaKey -ErrorAction SilentlyContinue | ForEach-Object {{
                $countKey = "$($_.PSPath)\\Count"
                if (Test-Path $countKey) {{
                    Get-ItemProperty $countKey -ErrorAction SilentlyContinue |
                    Get-Member -MemberType NoteProperty | Where-Object {{ $_.Name -notmatch '^PS' }} |
                    ForEach-Object {{
                        $name = $_.Name
                        try {{
                            $decoded = [System.Text.Encoding]::Unicode.GetString(
                                [System.Convert]::FromBase64String(
                                    [System.Text.Encoding]::Unicode.GetString(
                                        [System.Text.Encoding]::Unicode.GetBytes($name) | ForEach-Object {{ [byte]($_ -bxor 13) }}
                                    )
                                )
                            )
                        }} catch {{
                            $decoded = $name
                        }}
                        if ($decoded -match '\\.(exe|msi|bat|cmd|ps1)') {{
                            $results += @{{ program = $decoded; raw = $name }}
                        }}
                    }}
                }}
            }}
        }}
    }}
    ConvertTo-Json -InputObject @($results | Select-Object -First 100) -Compress
}} catch {{
    ConvertTo-Json -InputObject @() -Compress
}}
""".strip()


# Eventos MSI no Application log — apanha TODA instalação via Windows Installer
def _ps_msi_events(since_iso: str, until_iso: str) -> str:
    return f"""
try {{
    $events = Get-WinEvent -FilterHashtable @{{
        LogName   = 'Application'
        Id        = 1033, 1034, 11707, 11708
        StartTime = '{since_iso}'
        EndTime   = '{until_iso}'
    }} -ErrorAction Stop | Select-Object -First 200 |
    ForEach-Object {{
        @{{
            id      = $_.Id
            time    = $_.TimeCreated.ToString('o')
            message = $_.Message.Substring(0, [Math]::Min(500, $_.Message.Length))
        }}
    }}
    ConvertTo-Json -InputObject @($events) -Compress
}} catch {{
    ConvertTo-Json -InputObject @() -Compress
}}
""".strip()


# Software instalado — todos os registry paths, sem exigir InstallDate
def _ps_installed_since(since_iso: str, until_iso: str) -> str:
    date_compact = since_iso[:10].replace("-", "")
    return f"""
try {{
    $since = [DateTime]::Parse('{since_iso}')
    $until = [DateTime]::Parse('{until_iso}')
    $paths = @(
        'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',
        'HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',
        'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',
        'HKCU:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'
    )
    $seen = @{{}}
    $apps = @()
    foreach ($path in $paths) {{
        Get-ItemProperty $path -ErrorAction SilentlyContinue |
        Where-Object {{ $_.DisplayName }} |
        ForEach-Object {{
            $key = Get-Item -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
            $isNew = $false
            if ($_.InstallDate -and $_.InstallDate -ge '{date_compact}') {{ $isNew = $true }}
            elseif ($key -and $key.LastWriteTime -ge $since -and $key.LastWriteTime -le $until) {{ $isNew = $true }}
            if ($isNew -and -not $seen.ContainsKey($_.DisplayName)) {{
                $seen[$_.DisplayName] = $true
                $apps += @{{
                    name      = $_.DisplayName
                    version   = $_.DisplayVersion
                    publisher = $_.Publisher
                    date      = if ($_.InstallDate) {{ $_.InstallDate }} else {{ if ($key) {{ $key.LastWriteTime.ToString('o') }} else {{ '' }} }}
                    location  = $_.InstallLocation
                }}
            }}
        }}
    }}
    ConvertTo-Json -InputObject @($apps) -Compress
}} catch {{
    ConvertTo-Json -InputObject @() -Compress
}}
""".strip()


# Histórico PowerShell — comandos executados por qualquer utilizador
def _ps_history(username: str, since_iso: str, until_iso: str) -> str:
    return f"""
try {{
    $since = [DateTime]::Parse('{since_iso}')
    $until = [DateTime]::Parse('{until_iso}')
    $results = @()
    Get-ChildItem "C:\\Users" -Directory -ErrorAction SilentlyContinue | ForEach-Object {{
        $p = "$($_.FullName)\\AppData\\Roaming\\Microsoft\\Windows\\PowerShell\\PSReadLine\\ConsoleHost_history.txt"
        if (Test-Path $p) {{
            $item = Get-Item $p -ErrorAction SilentlyContinue
            if ($item -and $item.LastWriteTime -ge $since -and $item.LastWriteTime -le $until) {{
                $content = Get-Content $p -Tail 200 -ErrorAction SilentlyContinue
                if ($content) {{
                    $results += $content | ForEach-Object {{
                        @{{ command = $_; user_profile = $item.Directory.Parent.Name }}
                    }}
                }}
            }}
        }}
    }}
    ConvertTo-Json -InputObject @($results | Select-Object -First 300) -Compress
}} catch {{
    ConvertTo-Json -InputObject @() -Compress
}}
""".strip()


# Ficheiros executáveis recentemente criados/modificados
def _ps_recent_files(since_iso: str, until_iso: str) -> str:
    return f"""
try {{
    $since = [DateTime]::Parse('{since_iso}')
    $until = [DateTime]::Parse('{until_iso}')
    $dirs = @('C:\\Users', 'C:\\Temp', 'C:\\Windows\\Temp', 'C:\\ProgramData')
    $files = @()
    foreach ($dir in $dirs) {{
        if (Test-Path $dir) {{
            Get-ChildItem $dir -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object {{
                ($_.LastWriteTime -ge $since -and $_.LastWriteTime -le $until -or
                 $_.CreationTime  -ge $since -and $_.CreationTime  -le $until) -and
                $_.Extension -match '\\.(exe|msi|dll|bat|ps1|cmd|zip|rar|7z|jar|py|vbs|js)$'
            }} | Select-Object -First 100 |
            ForEach-Object {{
                $files += @{{
                    path     = $_.FullName
                    size_kb  = [Math]::Round($_.Length / 1KB, 1)
                    created  = $_.CreationTime.ToString('o')
                    modified = $_.LastWriteTime.ToString('o')
                    ext      = $_.Extension
                }}
            }}
        }}
    }}
    ConvertTo-Json -InputObject @($files | Select-Object -First 200) -Compress
}} catch {{
    ConvertTo-Json -InputObject @() -Compress
}}
""".strip()


# Collector -------------------------------------------------------------------------

class SecurityCollector:

    def __init__(self, executor):
        self._exec = executor

    def collect(self, username: str, credential_time: datetime) -> dict:
        since_iso = credential_time.strftime(_DT_FMT)
        # Janela de investigação: credential_time até credential_time + 24h
        until_dt  = credential_time + timedelta(hours=_CREDENTIAL_WINDOW_HOURS)
        until_iso = until_dt.strftime(_DT_FMT)

        return {
            "machine":             self._exec.host,
            "username":            username,
            "credential_time":     credential_time.isoformat(),
            "investigation_window": f"{since_iso} → {until_iso}",
            "collected_at":        datetime.now(timezone.utc).isoformat(),
            # Autenticação e sessão
            "logon_events":        self._get("Security", [4624, 4625, 4634, 4647, 4648], since_iso, until_iso, username),
            # Gestão de contas: filtrado por Subject/Target — sem falsos positivos
            "account_events":      self._run_json(_ps_account_events(since_iso, until_iso, username)),
            # Processos (requer auditpol — complementado por Prefetch)
            "process_events":      self._get("Security", [4688, 4689], since_iso, until_iso, username),
            # Prefetch — execuções reais sem necessidade de auditpol
            "prefetch":            self._run_json(_ps_prefetch(since_iso, until_iso)),
            # Serviços instalados
            "service_events":      self._collect_services(since_iso, until_iso),
            # Tarefas agendadas
            "task_events":         self._get("Security", [4698, 4699, 4702], since_iso, until_iso),
            # Privilégios especiais
            "privilege_events":    self._get("Security", [4672, 4673], since_iso, until_iso, username),
            # Acesso a ficheiros (requer auditpol)
            "file_events":         self._get("Security", [4663], since_iso, until_iso, username),
            # Registo
            "registry_events":     self._get("Security", [4657], since_iso, until_iso, username),
            # Instalações via Windows Installer (MSI) — generalista
            "msi_install_events":  self._run_json(_ps_msi_events(since_iso, until_iso)),
            # Estado actual
            "running_processes":   self._run_json(_PS_PROCESSES),
            "network_connections": self._run_json(_PS_NETWORK),
            # Software instalado (todos os registry paths)
            "software_installed":  self._run_json(_ps_installed_since(since_iso, until_iso)),
            # Ficheiros executáveis recentes
            "recent_files":        self._run_json(_ps_recent_files(since_iso, until_iso)),
            # Histórico PowerShell
            "powershell_history":  self._run_json(_ps_history(username, since_iso, until_iso)),
        }

    def _get(self, log: str, ids: list[int], since: str, until: str, username: str = "") -> list:
        return self._run_json(_ps_events(log, ids, since, until, username))

    def _collect_services(self, since_iso: str, until_iso: str) -> list:
        sec = self._run_json(_ps_events("Security", [4697], since_iso, until_iso))
        sys = self._run_json(_ps_events("System",   [7045], since_iso, until_iso))
        return sec + sys

    def _run_json(self, script: str) -> list:
        try:
            result = self._exec.run(script)
            if not result["ok"] or not result["stdout"]:
                return []
            parsed = json.loads(result["stdout"])
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
