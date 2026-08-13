"""
SecurityEventsCollector — lê o Windows Security Event Log e extrai eventos
de utilizadores para alimentar o UEBA no center.

Eventos recolhidos:
  4624  → logon (interactive, network, remote)
  4634  → logoff
  4647  → logoff iniciado pelo utilizador
  4672  → privilégios especiais atribuídos a nova sessão
  4688  → novo processo criado (com username + cmdline)
  4697  → serviço instalado em runtime
  4698  → tarefa agendada criada
  4702  → tarefa agendada modificada

Requer que o agente corra como Administrador (acesso ao canal Security).
"""

import logging
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

_SECURITY_CHANNEL = "Security"

# Privilégios de alto risco — usados para classificar severidade
_HIGH_RISK_PRIVILEGES = frozenset({
    "SeDebugPrivilege",
    "SeLoadDriverPrivilege",
    "SeTcbPrivilege",
    "SeBackupPrivilege",
    "SeRestorePrivilege",
    "SeTakeOwnershipPrivilege",
    "SeImpersonatePrivilege",
    "SeCreateTokenPrivilege",
})

# Processos suspeitos conhecidos
_SUSPICIOUS_PROCESSES = frozenset({
    "mimikatz.exe", "meterpreter.exe", "cobalt", "beacon.exe",
    "psexec.exe", "procdump.exe", "wce.exe", "pwdump.exe",
    "fgdump.exe", "gsecdump.exe", "lsadump.exe",
})

# Logon types relevantes (ignorar batch/service que são muito ruidosos)
_RELEVANT_LOGON_TYPES = {
    2:  "Interactive",
    3:  "Network",
    10: "RemoteInteractive",
    11: "CachedInteractive",
}


class SecurityEventsCollector:
    """Recolhe eventos de segurança do Windows Security Event Log."""

    def __init__(self):
        self._last_record_number: dict[str, int] = {}

    def collect(self, limit: int = 50) -> list[dict]:
        try:
            import win32evtlog
            import win32evtlogutil
            import win32security
        except ImportError:
            _log.debug("[SecurityEventsCollector] pywin32 não disponível")
            return []

        results: list[dict] = []
        flags = (
            win32evtlog.EVENTLOG_BACKWARDS_READ |
            win32evtlog.EVENTLOG_SEQUENTIAL_READ
        )

        try:
            handle = win32evtlog.OpenEventLog(None, _SECURITY_CHANNEL)
        except Exception as e:
            _log.debug("[SecurityEventsCollector] Sem acesso ao canal Security: %s", e)
            return []

        try:
            collected = 0
            last_seen = self._last_record_number.get(_SECURITY_CHANNEL, 0)
            newest    = last_seen

            while collected < limit:
                batch = win32evtlog.ReadEventLog(handle, flags, 0)
                if not batch:
                    break

                for ev in batch:
                    if collected >= limit:
                        break

                    rec_num = ev.RecordNumber
                    if rec_num <= last_seen:
                        break

                    if rec_num > newest:
                        newest = rec_num

                    event_id = ev.EventID & 0xFFFF
                    parsed = self._parse_event(ev, event_id)
                    if parsed:
                        results.append(parsed)
                        collected += 1

            if newest > last_seen:
                self._last_record_number[_SECURITY_CHANNEL] = newest

        except Exception as e:
            _log.debug("[SecurityEventsCollector] Erro: %s", e)
        finally:
            try:
                win32evtlog.CloseEventLog(handle)
            except Exception:
                pass

        return results

    # ─── PARSERS POR EVENT ID ───────────────────────────────────────────────

    def _parse_event(self, ev, event_id: int) -> dict | None:
        inserts = list(ev.StringInserts or [])
        ts      = self._ts(ev.TimeGenerated)

        if event_id == 4624:
            return self._parse_logon(inserts, ts)
        if event_id in (4634, 4647):
            return self._parse_logoff(inserts, ts)
        if event_id == 4672:
            return self._parse_privilege(inserts, ts)
        if event_id == 4688:
            return self._parse_process_create(inserts, ts)
        if event_id == 4697:
            return self._parse_service_install(inserts, ts)
        if event_id in (4698, 4702):
            return self._parse_scheduled_task(inserts, ts, event_id)
        return None

    def _parse_logon(self, ins: list, ts: str) -> dict | None:
        try:
            logon_type = int(ins[8]) if len(ins) > 8 else 0
            if logon_type not in _RELEVANT_LOGON_TYPES:
                return None
            username = ins[5] if len(ins) > 5 else ""
            domain   = ins[6] if len(ins) > 6 else ""
            if not username or username in ("-", "SYSTEM"):
                return None
            return {
                "event_type": "user_logon",
                "event_time": ts,
                "username":   f"{domain}\\{username}" if domain and domain != "-" else username,
                "details": {
                    "logon_type":    _RELEVANT_LOGON_TYPES[logon_type],
                    "logon_id":      ins[7] if len(ins) > 7 else "",
                    "ip_address":    ins[18] if len(ins) > 18 else "",
                    "workstation":   ins[11] if len(ins) > 11 else "",
                },
            }
        except Exception:
            return None

    def _parse_logoff(self, ins: list, ts: str) -> dict | None:
        try:
            username = ins[1] if len(ins) > 1 else ""
            domain   = ins[2] if len(ins) > 2 else ""
            if not username or username in ("-", "SYSTEM"):
                return None
            return {
                "event_type": "user_logoff",
                "event_time": ts,
                "username":   f"{domain}\\{username}" if domain and domain != "-" else username,
                "details": {"logon_id": ins[3] if len(ins) > 3 else ""},
            }
        except Exception:
            return None

    def _parse_privilege(self, ins: list, ts: str) -> dict | None:
        try:
            username   = ins[1] if len(ins) > 1 else ""
            domain     = ins[2] if len(ins) > 2 else ""
            privileges = [p.strip() for p in (ins[4] if len(ins) > 4 else "").split("\n") if p.strip()]
            if not username or username in ("-", "SYSTEM") or not privileges:
                return None
            high_risk = [p for p in privileges if p in _HIGH_RISK_PRIVILEGES]
            return {
                "event_type": "privilege_use",
                "event_time": ts,
                "username":   f"{domain}\\{username}" if domain and domain != "-" else username,
                "severity":   "high" if high_risk else "low",
                "details": {
                    "privileges":      privileges,
                    "high_risk":       high_risk,
                    "logon_id":        ins[3] if len(ins) > 3 else "",
                },
            }
        except Exception:
            return None

    def _parse_process_create(self, ins: list, ts: str) -> dict | None:
        try:
            username  = ins[6]  if len(ins) > 6  else ""
            domain    = ins[7]  if len(ins) > 7  else ""
            proc_name = ins[5]  if len(ins) > 5  else ""
            proc_path = ins[4]  if len(ins) > 4  else ""
            cmdline   = ins[8]  if len(ins) > 8  else ""
            elevated  = ins[18] if len(ins) > 18 else ""

            if not username or username in ("-", "SYSTEM"):
                return None

            name_lower = (proc_name or "").lower()
            suspicious = any(s in name_lower for s in _SUSPICIOUS_PROCESSES)

            return {
                "event_type": "process_start",
                "event_time": ts,
                "username":   f"{domain}\\{username}" if domain and domain != "-" else username,
                "severity":   "high" if suspicious else "low",
                "details": {
                    "name":       proc_name,
                    "path":       proc_path,
                    "cmdline":    cmdline[:500],
                    "elevated":   elevated == "%%1937",
                    "suspicious": suspicious,
                },
            }
        except Exception:
            return None

    def _parse_service_install(self, ins: list, ts: str) -> dict | None:
        try:
            username = ins[1] if len(ins) > 1 else ""
            domain   = ins[2] if len(ins) > 2 else ""
            svc_name = ins[4] if len(ins) > 4 else ""
            img_path = ins[5] if len(ins) > 5 else ""
            return {
                "event_type": "service_install",
                "event_time": ts,
                "username":   f"{domain}\\{username}" if domain and domain != "-" else username,
                "severity":   "high",
                "details": {
                    "service_name": svc_name,
                    "image_path":   img_path,
                    "start_type":   ins[6] if len(ins) > 6 else "",
                    "account":      ins[7] if len(ins) > 7 else "",
                },
            }
        except Exception:
            return None

    def _parse_scheduled_task(self, ins: list, ts: str, event_id: int) -> dict | None:
        try:
            username  = ins[1] if len(ins) > 1 else ""
            domain    = ins[2] if len(ins) > 2 else ""
            task_name = ins[4] if len(ins) > 4 else ""
            content   = ins[5] if len(ins) > 5 else ""
            return {
                "event_type": "scheduled_task",
                "event_time": ts,
                "username":   f"{domain}\\{username}" if domain and domain != "-" else username,
                "severity":   "medium",
                "details": {
                    "task_name":    task_name,
                    "task_content": content[:500],
                    "action":       "created" if event_id == 4698 else "updated",
                },
            }
        except Exception:
            return None

    @staticmethod
    def _ts(win_time) -> str:
        try:
            if hasattr(win_time, "timetuple"):
                return datetime(*win_time.timetuple()[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
        return datetime.now(timezone.utc).isoformat()
