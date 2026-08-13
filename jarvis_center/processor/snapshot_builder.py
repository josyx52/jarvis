from copy import deepcopy
import time


class SnapshotBuilder:

    def build(self, state: dict):

        if not state:
            return None

        metrics = deepcopy(state.get("metrics") or {})
        processes = deepcopy(state.get("processes") or [])
        services = deepcopy(state.get("services") or [])
        network = deepcopy(state.get("network") or [])
        disk = deepcopy(state.get("disk") or [])
        logs = deepcopy(state.get("logs") or [])
        connections = deepcopy(state.get("connections") or [])
        raw_events = deepcopy(state.get("raw_events") or [])

        snapshot = {
            "timestamp": metrics.get("timestamp") or time.time(),
            "host": deepcopy(state["host"]),
            "metrics": metrics,
            "processes": processes,
            "services": services,
            "network": network,
            "disk": disk,
            "logs": logs,
            "connections": connections,
            "raw_events": raw_events,
            "events": [],
        }

        snapshot["cpu"] = self._build_cpu_view(processes)
        snapshot["memory"] = self._build_memory_view(processes)

        return snapshot

    def _build_cpu_view(self, processes: list[dict]) -> list[dict]:

        def _norm_cpu(proc: dict) -> float:
            """Windows percent_processor_time pode ser raw 100ns ticks — normaliza para 0-100."""
            try:
                v = float(proc.get("cpu_percent") or proc.get("percent_processor_time") or 0)
                return min(v, 100.0)  # cap a 100% — valores > 100 são artefactos WMI
            except Exception:
                return 0.0

        ordered = sorted(processes, key=_norm_cpu, reverse=True)

        result = []

        for proc in ordered[:20]:
            result.append({
                "pid": proc.get("pid"),
                "name": proc.get("name"),
                "cpu_percent": min(float(proc.get("cpu_percent") or 0), 100.0),
                "percent_processor_time": min(float(proc.get("percent_processor_time") or 0), 100.0),
                "user_time": proc.get("user_time"),
                "system_time": proc.get("system_time"),
            })

        return result

    def _build_memory_view(self, processes: list[dict]) -> list[dict]:

        def mem_value(proc: dict) -> int:
            try:
                return int(proc.get("resident_size") or 0)
            except Exception:
                return 0

        ordered = sorted(processes, key=mem_value, reverse=True)

        result = []

        for proc in ordered[:20]:
            result.append({
                "pid": proc.get("pid"),
                "name": proc.get("name"),
                "resident_size": proc.get("resident_size"),
                "total_size": proc.get("total_size"),
            })

        return result