import time


class HostStateStore:

    def __init__(self):
        self.hosts: dict[str, dict] = {}

    # --------------------------------
    # BUILD HOST KEY
    # --------------------------------

    def _host_key(self, frame: dict) -> str | None:

        host = frame.get("host", {}) or {}

        hostname = host.get("hostname")
        boot_id = host.get("boot_id", "default")

        if not hostname:
            return None

        return f"{hostname}::{boot_id}"

    # --------------------------------
    # GET OR CREATE STATE
    # --------------------------------

    def _new_state(self, host_key: str, host: dict) -> dict:

        now = time.time()

        return {
            "host_key": host_key,
            "host": host,
            # core
            "metrics":    {},
            "processes":  [],
            "services":   [],
            "network":    [],
            "disk":       [],
            "logs":       [],
            "connections": [],
            "otel_spans": [],
            "raw_events": [],
            # specialized
            "db_telemetry":    [],   # lista de payloads db (pode haver varias DBs)
            "kafka_telemetry": [],
            "web_telemetry":   [],
            "mq_telemetry":    [],
            # network probe
            "execution_units": [],  # resultado do CorrelationEngine do agente
            # meta
            "server_profile":  None,
            "last_frame_type": None,
            "last_seen":  now,
            "created_at": now,
        }

    # --------------------------------
    # UPDATE STATE
    # --------------------------------

    def update(self, frame: dict):

        host_key = self._host_key(frame)

        if not host_key:
            return None

        host = frame.get("host", {}) or {}

        if host_key not in self.hosts:
            self.hosts[host_key] = self._new_state(host_key, host)

        state = self.hosts[host_key]

        state["host"] = host
        state["last_seen"] = time.time()

        frame_type = frame.get("frame_type")
        telemetry = frame.get("telemetry", {}) or {}

        if frame_type == "metrics":
            state["metrics"] = telemetry.get("metrics", {}) or {}

        elif frame_type == "inventory":
            # Só sobrescreve se o agente enviou dados — evita apagar o último estado válido
            # quando o colector falha temporariamente e devolve lista vazia
            for field in ("processes", "services", "network", "disk"):
                fresh = telemetry.get(field) or []
                if fresh:
                    state[field] = fresh

        elif frame_type == "logs":
            state["logs"] = telemetry.get("logs", []) or []

        elif frame_type == "connections":
            state["connections"] = telemetry.get("connections", []) or []

        elif frame_type == "events":
            # o center não depende mais destes eventos como fonte principal
            state["raw_events"] = telemetry.get("events", []) or []

        elif frame_type == "otel_trace":
            new_spans = telemetry.get("spans", []) or []
            state["otel_spans"] = new_spans

        elif frame_type == "db_telemetry":
            # Acumula por db_host (pode haver varias DBs no mesmo servidor)
            db_host = telemetry.get("host", "unknown")
            existing = state["db_telemetry"]
            state["db_telemetry"] = [
                e for e in existing if e.get("host") != db_host
            ] + [telemetry]

        elif frame_type == "kafka_telemetry":
            state["kafka_telemetry"] = [telemetry]

        elif frame_type == "web_telemetry":
            state["web_telemetry"] = [telemetry]

        elif frame_type == "mq_telemetry":
            state["mq_telemetry"] = [telemetry]

        elif frame_type == "execution_unit":
            # Execution units do CorrelationEngine do agente
            # Acumula (não substitui) — podem chegar várias por ciclo
            new_units = telemetry.get("units", []) or []
            state.setdefault("execution_units", [])
            state["execution_units"] = (state["execution_units"] + new_units)[-200:]

        elif frame_type == "heartbeat":
            # Aproveita o heartbeat para capturar o perfil do servidor
            server_profile = host.get("server_profile") or frame.get("server_profile")
            if server_profile:
                state["server_profile"] = server_profile

        state["last_frame_type"] = frame_type

        return state

    # --------------------------------
    # OPTIONAL CLEANUP
    # --------------------------------

    def cleanup_stale_hosts(self, max_idle_seconds: int = 3600):

        now = time.time()
        stale_keys = []

        for host_key, state in self.hosts.items():

            last_seen = state.get("last_seen", 0)

            if now - last_seen > max_idle_seconds:
                stale_keys.append(host_key)

        for host_key in stale_keys:
            del self.hosts[host_key]

        return len(stale_keys)