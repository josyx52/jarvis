import logging

_log = logging.getLogger(__name__)

_CHANNELS = ["System", "Application"]

# win32evtlog EventType constants
_LEVEL_NAMES = {
    1:  "error",
    2:  "warning",
    4:  "information",
    8:  "audit_success",
    16: "audit_failure",
}
# recolher apenas erros e avisos — informação é demasiado ruidosa para monitorização
_RELEVANT_LEVELS = frozenset({1, 2})


class LogsCollector:
    """Lê eventos recentes (erros e avisos) do Windows Event Log."""

    def collect(self, limit: int = 20) -> list[dict]:
        try:
            import win32evtlog
            import win32evtlogutil
        except ImportError:
            _log.debug("[LogsCollector] pywin32 não disponível")
            return []

        results: list[dict] = []
        per_channel = max(1, limit // len(_CHANNELS))
        flags = (
            win32evtlog.EVENTLOG_BACKWARDS_READ |
            win32evtlog.EVENTLOG_SEQUENTIAL_READ
        )

        for channel in _CHANNELS:
            try:
                handle = win32evtlog.OpenEventLog(None, channel)
            except Exception as e:
                _log.debug("[LogsCollector] Não foi possível abrir canal %s: %s", channel, e)
                continue

            try:
                collected = 0
                while collected < per_channel:
                    batch = win32evtlog.ReadEventLog(handle, flags, 0)
                    if not batch:
                        break
                    for ev in batch:
                        if collected >= per_channel:
                            break
                        if ev.EventType not in _RELEVANT_LEVELS:
                            continue
                        try:
                            msg = win32evtlogutil.SafeFormatMessage(ev, channel)
                        except Exception:
                            msg = " | ".join(ev.StringInserts or [])
                        results.append({
                            "channel":   channel,
                            "source":    ev.SourceName,
                            "event_id":  ev.EventID & 0xFFFF,
                            "level":     _LEVEL_NAMES.get(ev.EventType, "other"),
                            "message":   (msg or "").strip()[:500],
                            "timestamp": ev.TimeGenerated.isoformat(),
                        })
                        collected += 1
            except Exception as e:
                _log.debug("[LogsCollector] Erro a ler eventos de %s: %s", channel, e)
            finally:
                try:
                    win32evtlog.CloseEventLog(handle)
                except Exception:
                    pass

        return results
