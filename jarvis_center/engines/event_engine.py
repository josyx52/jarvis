from typing import Any


class EventEngine:

    def build_events(self, snapshots: list[dict]) -> list[dict]:

        if not snapshots:
            return []

        current = snapshots[0]
        previous = snapshots[1] if len(snapshots) > 1 else None

        events = []

        events.extend(self._metric_threshold_events(current))
        events.extend(self._disk_events(current))
        events.extend(self._service_events(current, previous))
        events.extend(self._process_events(current, previous))
        events.extend(self._network_interface_events(current, previous))
        events.extend(self._network_connection_events(current, previous))
        events.extend(self._network_throughput_events(current, previous))

        return self._deduplicate(events)

    # --------------------------------
    # METRIC THRESHOLD EVENTS
    # --------------------------------

    def _metric_threshold_events(self, current: dict) -> list[dict]:

        metrics = current.get("metrics", {}) or {}
        events = []

        cpu = self._to_float(metrics.get("cpu_percent"))
        memory = self._to_float(metrics.get("memory_percent"))
        disk = self._to_float(metrics.get("disk_percent"))

        if cpu >= 90:
            events.append({
                "event_type": "cpu_high",
                "severity": "high",
                "entity_type": "host",
                "entity_name": "cpu",
                "summary": f"CPU muito alta: {cpu:.1f}%",
                "payload": {"cpu_percent": cpu}
            })
        elif cpu >= 75:
            events.append({
                "event_type": "cpu_elevated",
                "severity": "medium",
                "entity_type": "host",
                "entity_name": "cpu",
                "summary": f"CPU elevada: {cpu:.1f}%",
                "payload": {"cpu_percent": cpu}
            })

        if memory >= 90:
            events.append({
                "event_type": "memory_high",
                "severity": "high",
                "entity_type": "host",
                "entity_name": "memory",
                "summary": f"Memória muito alta: {memory:.1f}%",
                "payload": {"memory_percent": memory}
            })
        elif memory >= 80:
            events.append({
                "event_type": "memory_elevated",
                "severity": "medium",
                "entity_type": "host",
                "entity_name": "memory",
                "summary": f"Memória elevada: {memory:.1f}%",
                "payload": {"memory_percent": memory}
            })

        if disk >= 95:
            events.append({
                "event_type": "disk_usage_critical",
                "severity": "critical",
                "entity_type": "host",
                "entity_name": "disk",
                "summary": f"Uso de disco crítico: {disk:.1f}%",
                "payload": {"disk_percent": disk}
            })
        elif disk >= 85:
            events.append({
                "event_type": "disk_usage_high",
                "severity": "high",
                "entity_type": "host",
                "entity_name": "disk",
                "summary": f"Uso de disco alto: {disk:.1f}%",
                "payload": {"disk_percent": disk}
            })

        return events

    # --------------------------------
    # DISK EVENTS
    # --------------------------------

    def _disk_events(self, current):

        events = []

        for disk in current.get("disk", []) or []:

            device = disk.get("device_id") or disk.get("name") or "unknown_disk"
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
    # NETWORK THROUGHPUT EVENTS
    # --------------------------------

    def _network_throughput_events(self, current, previous):

        if not previous:
            return []

        curr = current.get("metrics", {}) or {}
        prev = previous.get("metrics", {}) or {}

        curr_total = self._to_int(curr.get("net_bytes_sent")) + self._to_int(curr.get("net_bytes_recv"))
        prev_total = self._to_int(prev.get("net_bytes_sent")) + self._to_int(prev.get("net_bytes_recv"))

        if prev_total <= 0 or curr_total <= prev_total:
            return []

        delta = curr_total - prev_total
        events = []

        if delta >= 1_000_000_000:
            events.append({
                "event_type": "network_throughput_spike",
                "severity": "high",
                "entity_type": "network",
                "entity_name": "throughput",
                "summary": f"Pico de tráfego de rede detectado: +{delta} bytes",
                "payload": {
                    "previous_total_bytes": prev_total,
                    "current_total_bytes": curr_total,
                    "delta_bytes": delta
                }
            })
        elif delta >= 250_000_000:
            events.append({
                "event_type": "network_throughput_elevated",
                "severity": "medium",
                "entity_type": "network",
                "entity_name": "throughput",
                "summary": f"Tráfego de rede elevado: +{delta} bytes",
                "payload": {
                    "previous_total_bytes": prev_total,
                    "current_total_bytes": curr_total,
                    "delta_bytes": delta
                }
            })

        return events

    # --------------------------------
    # UTILS
    # --------------------------------

    def _deduplicate(self, events: list[dict]) -> list[dict]:
        seen = set()
        result = []

        for event in events:
            key = (
                event.get("event_type"),
                event.get("entity_type"),
                event.get("entity_name"),
                event.get("summary"),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(event)

        return result

    def _to_int(self, value: Any):
        try:
            return int(float(value))
        except Exception:
            return 0

    def _to_float(self, value: Any):
        try:
            return float(value)
        except Exception:
            return 0.0