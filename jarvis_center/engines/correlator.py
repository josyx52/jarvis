class Correlator:

    def correlate(self, events: list[dict], topology: dict | None = None) -> list[dict]:
        correlations = []

        topology = topology or {}

        has_disk = any(e["event_type"] == "disk_low_space" for e in events)
        proc_growth = [e for e in events if e["event_type"] == "process_memory_growth"]
        svc_stop = [e for e in events if e["event_type"] == "service_stopped"]
        net_spike = [e for e in events if e["event_type"] == "network_connection_spike"]
        iface_down = [e for e in events if e["event_type"] == "network_interface_down"]

        # eventos vindos do TraceEngine
        slow_spans = [e for e in events if e["event_type"] == "trace_slow_span"]
        slow_queries = [e for e in events if e["event_type"] == "trace_slow_query"]
        high_errors = [e for e in events if e["event_type"] == "trace_high_error_rate"]

        # --------------------------------
        # TRACE: QUERY LENTA + DISCO
        # --------------------------------

        if slow_queries and has_disk:
            top_query = slow_queries[0]
            payload = top_query.get("payload", {})
            correlations.append({
                "correlation_type": "slow_query_causing_disk_pressure",
                "severity": "high",
                "summary": (
                    f"Query lenta detectada no servico '{top_query['entity_name']}' "
                    f"({payload.get('duration_ms', '?')}ms em '{payload.get('db_system', '?')}') "
                    f"em simultâneo com pressão de disco. Possível crescimento de logs ou temp files."
                ),
                "evidence": {
                    "events": events,
                    "topology": {
                        "service": top_query["entity_name"],
                        "db_system": payload.get("db_system"),
                        "duration_ms": payload.get("duration_ms"),
                    }
                }
            })

        # --------------------------------
        # TRACE: ERRO ELEVADO + SERVICO PARADO
        # --------------------------------

        if high_errors and svc_stop:
            top_err = high_errors[0]
            stopped = svc_stop[0]
            correlations.append({
                "correlation_type": "trace_errors_after_service_stop",
                "severity": "high",
                "summary": (
                    f"Servico '{stopped['entity_name']}' parou e o servico de aplicacao "
                    f"'{top_err['entity_name']}' passou a reportar taxa de erro elevada. "
                    f"Provável dependência entre eles."
                ),
                "evidence": {
                    "events": events,
                    "topology": {
                        "app_service": top_err["entity_name"],
                        "stopped_service": stopped["entity_name"],
                        "error_rate": top_err.get("payload", {}).get("error_rate"),
                    }
                }
            })

        # --------------------------------
        # TRACE: SPAN LENTO + CRESCIMENTO DE PROCESSO
        # --------------------------------

        if slow_spans and proc_growth:
            top_span = slow_spans[0]
            top_proc = proc_growth[0]
            correlations.append({
                "correlation_type": "slow_span_with_process_memory_growth",
                "severity": "high",
                "summary": (
                    f"Operacao lenta '{top_span.get('payload', {}).get('operation', '?')}' "
                    f"no servico '{top_span['entity_name']}' ocorre em simultâneo com "
                    f"crescimento de memória do processo '{top_proc['entity_name']}'. "
                    f"Possível memory leak por request."
                ),
                "evidence": {
                    "events": events,
                    "topology": {
                        "app_service": top_span["entity_name"],
                        "process": top_proc["entity_name"],
                        "avg_duration_ms": top_span.get("payload", {}).get("avg_duration_ms"),
                    }
                }
            })

        # --------------------------------
        # DISK + PROCESS GROWTH
        # --------------------------------

        if has_disk and proc_growth:
            top_proc = proc_growth[0]
            proc_name = top_proc["entity_name"]

            has_network = self._process_name_has_network_activity(topology, proc_name)

            summary = (
                f"Possível pressão de disco associada ao processo "
                f"{proc_name} com crescimento de memória."
            )

            if has_network:
                summary += " O processo também possui atividade de rede no snapshot atual."

            correlations.append({
                "correlation_type": "disk_pressure_with_process_growth",
                "severity": "high",
                "summary": summary,
                "evidence": {
                    "events": events,
                    "topology": {
                        "process_name": proc_name,
                        "has_network_activity": has_network
                    }
                }
            })

        # --------------------------------
        # SERVICE STOP + PROCESS GROWTH
        # --------------------------------

        if svc_stop and proc_growth:
            top_proc = proc_growth[0]
            stopped = svc_stop[0]

            proc_name = top_proc["entity_name"]
            service_name = stopped["entity_name"]

            relation_type = self._infer_service_process_relation(
                topology=topology,
                service_name=service_name,
                process_name=proc_name
            )

            if relation_type == "direct":
                summary = (
                    f"Possível impacto direto do processo {proc_name} "
                    f"na parada do serviço {service_name}, pois a topologia "
                    f"indica vínculo entre eles."
                )

                severity = "high"

            elif relation_type == "indirect":
                summary = (
                    f"Possível impacto indireto do processo {proc_name} "
                    f"na parada do serviço {service_name}. Há evidências "
                    f"operacionais e atividade topológica relevante, mas sem vínculo direto confirmado."
                )

                severity = "high"

            else:
                summary = (
                    f"Possível impacto do processo {proc_name} "
                    f"na parada do serviço {service_name}. A hipótese foi "
                    f"mantida com base nos eventos, mas a topologia ainda não confirmou vínculo direto."
                )

                severity = "medium"

            correlations.append({
                "correlation_type": "service_stop_after_process_growth",
                "severity": severity,
                "summary": summary,
                "evidence": {
                    "events": events,
                    "topology": {
                        "process_name": proc_name,
                        "service_name": service_name,
                        "relation_type": relation_type
                    }
                }
            })

        # --------------------------------
        # NETWORK SPIKE + PROCESS GROWTH
        # --------------------------------

        if net_spike and proc_growth:
            top_proc = proc_growth[0]
            proc_name = top_proc["entity_name"]

            has_network = self._process_name_has_network_activity(topology, proc_name)

            summary = (
                f"Pico de conexões detectado enquanto o processo "
                f"{proc_name} apresenta crescimento de memória."
            )

            if has_network:
                summary += " A topologia confirma atividade de rede associada ao processo."

            correlations.append({
                "correlation_type": "network_spike_during_process_growth",
                "severity": "high" if has_network else "medium",
                "summary": summary,
                "evidence": {
                    "events": events,
                    "topology": {
                        "process_name": proc_name,
                        "has_network_activity": has_network
                    }
                }
            })

        # --------------------------------
        # RESOURCE PRESSURE CHAIN
        # --------------------------------

        if has_disk and proc_growth and net_spike:
            top_proc = proc_growth[0]
            proc_name = top_proc["entity_name"]

            correlations.append({
                "correlation_type": "resource_pressure_chain",
                "severity": "high",
                "summary": (
                    f"Cadeia de pressão de recursos detectada envolvendo o processo "
                    f"{proc_name}, com crescimento de memória, pressão de disco e pico de conexões."
                ),
                "evidence": {
                    "events": events,
                    "topology": {
                        "process_name": proc_name
                    }
                }
            })

        # --------------------------------
        # SERVICE STOP + INTERFACE DOWN
        # --------------------------------

        if svc_stop and iface_down:
            stopped = svc_stop[0]
            iface = iface_down[0]

            correlations.append({
                "correlation_type": "service_stop_after_interface_failure",
                "severity": "high",
                "summary": (
                    f"Serviço {stopped['entity_name']} parou em contexto de falha "
                    f"da interface de rede {iface['entity_name']}."
                ),
                "evidence": {
                    "events": events,
                    "topology": {
                        "service_name": stopped["entity_name"],
                        "interface_name": iface["entity_name"]
                    }
                }
            })

        return correlations

    def _process_name_has_network_activity(self, topology: dict, process_name: str) -> bool:

        process_name = (process_name or "").lower()

        process_to_remotes = topology.get("process_to_remotes", {})
        processes = topology.get("processes", {})

        for pid, proc in processes.items():
            if (proc.get("name") or "").lower() != process_name:
                continue

            remotes = process_to_remotes.get(pid, [])

            for r in remotes:
                remote = r.get("remote")
                if remote not in ("()", "unknown_remote"):
                    return True

        return False

    def _infer_service_process_relation(
        self,
        topology: dict,
        service_name: str,
        process_name: str
    ) -> str:
        """
        Retorna:
        - direct   -> serviço está ligado ao PID do processo com growth
        - indirect -> processo com growth tem atividade de rede/topológica relevante
        - none     -> sem relação observável
        """

        service_name = service_name or ""
        process_name = (process_name or "").lower()

        service_to_pid = topology.get("service_to_pid", {})
        processes = topology.get("processes", {})
        process_to_remotes = topology.get("process_to_remotes", {})

        service_pid = service_to_pid.get(service_name, 0)

        if service_pid > 0:
            proc = processes.get(service_pid)

            if proc and (proc.get("name") or "").lower() == process_name:
                return "direct"

        for pid, proc in processes.items():
            if (proc.get("name") or "").lower() != process_name:
                continue

            remotes = process_to_remotes.get(pid, [])
            if any(r.get("remote") not in ("()", "unknown_remote") for r in remotes):
                return "indirect"

        return "none"