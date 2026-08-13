from typing import Any


class EventEngine:

    def build_events(self, snapshots: list[dict]) -> list[dict]:

        if not snapshots:
            return []

        current = snapshots[0]
        previous = snapshots[1] if len(snapshots) > 1 else None

        events = []

        events.extend(self._disk_events(current))
        events.extend(self._service_events(current, previous))
        events.extend(self._process_events(current, previous))
        events.extend(self._network_interface_events(current, previous))
        events.extend(self._network_connection_events(current, previous))

        return events

    # --------------------------------
    # DISK EVENTS
    # --------------------------------

    def _disk_events(self, current):

        events = []

        for disk in current.get("disk", []) or []:

            device = disk.get("device_id")
            size = self._to_int(disk.get("size"))
            free = self._to_int(disk.get("free_space"))

            if size <= 0:
                continue

            free_pct = (free / size) * 100

            if free_pct < 15:

                events.append({
                    "event_type": "disk_low_space",
                    "severity": "high" if free_pct < 10 else "medium",
                    "entity_type": "disk",
                    "entity_name": device,
                    "summary": f"Disco {device} com pouco espaço livre: {free_pct:.1f}%",
                    "payload": disk
                })

        return events

    # --------------------------------
    # SERVICE EVENTS
    # --------------------------------

    def _service_events(self, current, previous):

        if not previous:
            return []

        prev_services = {
            s.get("name"): (s.get("status") or "").upper()
            for s in (previous.get("services", []) or [])
        }

        events = []

        for svc in current.get("services", []) or []:

            name = svc.get("name")
            current_status = (svc.get("status") or "").upper()
            prev_status = prev_services.get(name, "")

            if prev_status == "RUNNING" and current_status == "STOPPED":

                events.append({
                    "event_type": "service_stopped",
                    "severity": "high",
                    "entity_type": "service",
                    "entity_name": name,
                    "summary": f"Serviço {name} parou entre snapshots",
                    "payload": svc
                })

        return events

    # --------------------------------
    # PROCESS EVENTS
    # --------------------------------

    def _process_events(self, current, previous):

        if not previous:
            return []

        prev_by_pid = {
            str(p.get("pid")): p
            for p in (previous.get("processes", []) or [])
        }

        events = []

        for proc in current.get("processes", []) or []:

            pid = str(proc.get("pid"))
            prev_proc = prev_by_pid.get(pid)

            if not prev_proc:
                continue

            curr_rs = self._to_int(proc.get("resident_size"))
            prev_rs = self._to_int(prev_proc.get("resident_size"))

            if prev_rs <= 0:
                continue

            growth = curr_rs - prev_rs
            growth_pct = (growth / prev_rs) * 100

            if growth > 150_000_000 and growth_pct > 30:

                events.append({
                    "event_type": "process_memory_growth",
                    "severity": "medium",
                    "entity_type": "process",
                    "entity_name": proc.get("name"),
                    "summary": f"Processo {proc.get('name')} aumentou memória rapidamente",
                    "payload": proc
                })

        return events

    # --------------------------------
    # NETWORK INTERFACE EVENTS
    # --------------------------------

    def _network_interface_events(self, current, previous):

        if not previous:
            return []

        prev_if = {
            n.get("interface"): n
            for n in (previous.get("network", []) or [])
        }

        events = []

        for iface in current.get("network", []) or []:

            name = iface.get("interface")

            prev = prev_if.get(name)

            if not prev:
                continue

            prev_status = (prev.get("status") or "").upper()
            curr_status = (iface.get("status") or "").upper()

            if prev_status == "UP" and curr_status == "DOWN":

                events.append({
                    "event_type": "network_interface_down",
                    "severity": "critical",
                    "entity_type": "network_interface",
                    "entity_name": name,
                    "summary": f"Interface de rede {name} caiu",
                    "payload": iface
                })

        return events

    # --------------------------------
    # NETWORK CONNECTION EVENTS
    # --------------------------------

    def _network_connection_events(self, current, previous):

        if not previous:
            return []

        curr_conn = len(current.get("connections", []) or [])
        prev_conn = len(previous.get("connections", []) or [])

        events = []

        if prev_conn > 0:

            growth = curr_conn - prev_conn
            pct = (growth / prev_conn) * 100

            if pct > 200 and curr_conn > 100:

                events.append({
                    "event_type": "network_connection_spike",
                    "severity": "high",
                    "entity_type": "network",
                    "entity_name": "connections",
                    "summary": f"Pico de conexões detectado: {curr_conn}",
                    "payload": {
                        "previous": prev_conn,
                        "current": curr_conn
                    }
                })

        return events

    # --------------------------------
    # UTILS
    # --------------------------------

    def _to_int(self, value: Any):

        try:
            return int(value)
        except Exception:
            return 0