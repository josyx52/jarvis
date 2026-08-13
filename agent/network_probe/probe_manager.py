"""
ProbeManager — orquestrador da Architecture B (kernel/network level observability).

Coordena cinco componentes:
  ConnectionTracker  — polling psutil para topologia TCP (sempre activo)
  EtwConsumer        — eventos kernel ETW: processo, DNS, TCP+PID (se pywintrace)
  PacketCapture      — captura WinDivert SNIFF / raw socket (se pydivert)
  ProtocolDecoder    — decodifica HTTP, PostgreSQL, Redis, MySQL, MongoDB, Kafka
  CorrelationEngine  — correlaciona as três fontes → execution units
  SpanEmitter        — emite para o OTel receiver local (porta 4318)

Segue o padrão DiscoveryEngine: start() + drain() para integração no RuntimeAgent.
"""

import threading
import time

from network_probe.connection_tracker    import ConnectionTracker
from network_probe.etw_consumer          import EtwConsumer
from network_probe.packet_capture        import PacketCapture
from network_probe.protocol_decoder      import ProtocolDecoder
from network_probe.correlation_engine    import CorrelationEngine
from network_probe.topology_builder      import TopologyBuilder
from network_probe.span_emitter          import SpanEmitter
from auto_instrument.process_injector    import ProcessInjector


_LOOP_INTERVAL          = 0.1   # segundos entre ciclos (100ms para capturar conexões curtas)
_TOPOLOGY_EMIT_INTERVAL = 30    # segundos entre frames de topologia


class ProbeManager:

    def __init__(self, otel_endpoint: str = "http://localhost:4318",
                 process_injection: bool = True):

        # Componentes de captura
        self._conn_tracker = ConnectionTracker(interval=2.0)
        self._etw          = EtwConsumer()
        self._capture      = PacketCapture()
        self._decoder      = ProtocolDecoder()

        # Componentes de correlação e topologia
        self._correlation  = CorrelationEngine(etw_consumer=self._etw)
        self._topology     = TopologyBuilder(ttl_seconds=300)

        # Emissão
        self._emitter      = SpanEmitter(endpoint=otel_endpoint)

        # Dynatrace Layer 2 — injecção por processo
        if process_injection:
            self._injector = ProcessInjector()
            self._etw.set_process_start_callback(self._injector.on_process_start)
        else:
            self._injector = None

        # Fila de eventos para o RuntimeAgent
        self._pending_events: list = []
        self._lock                 = threading.Lock()
        self._running              = False
        self._last_topology_emit   = 0.0

    # ─── LIFECYCLE ───

    def start(self):
        self._running = True

        self._conn_tracker.start()
        self._etw.start()
        self._capture.start()

        threading.Thread(
            target=self._loop,
            daemon=True,
            name="probe-manager"
        ).start()

        mode     = getattr(self._capture, "mode", "rawsocket")
        etw      = "activo" if self._etw._available else "desactivado (pip install pywintrace)"
        injector = "activo" if self._injector else "desactivado"
        print(f"[ProbeManager] Architecture B iniciada | capture={mode} | ETW={etw} | injector={injector}")

        # Injectar processos que já estavam a correr antes do agente arrancar
        if self._injector:
            threading.Thread(
                target=self._injector.scan_existing_processes,
                daemon=True,
                name="injector-scan-existing",
            ).start()

    def stop(self):
        self._running = False
        self._conn_tracker.stop()
        self._etw.stop()
        self._capture.stop()

    # ─── DRAIN — chamado pelo RuntimeAgent a cada ciclo ───

    def drain(self) -> list:
        """Devolve eventos de topologia e lifecycle para o RuntimeAgent enfileirar."""
        with self._lock:
            events              = self._pending_events[:]
            self._pending_events = []
        return events

    def get_topology(self) -> dict:
        return self._topology.get_topology_frame_data()

    # ─── LOOP PRINCIPAL ───

    def _loop(self):
        while self._running:
            try:
                self._process_connections()
                self._process_etw()
                self._process_packets()
                self._flush_execution_units()
                self._emit_topology()
            except Exception as e:
                print(f"[ProbeManager] erro no loop: {e}")
            time.sleep(_LOOP_INTERVAL)

    # ─── PROCESSAMENTO ───

    def _process_connections(self):
        """ConnectionTracker → Topologia + CorrelationEngine."""
        conn_events = self._conn_tracker.drain()
        if not conn_events:
            return

        self._topology.update(conn_events)
        self._emitter.emit_connection_spans(conn_events)

        # Alimentar o CorrelationEngine com cada nova conexão TCP
        for ev in conn_events:
            pid = ev.get("pid", 0)
            if not pid:
                continue
            local = ev.get("local_addr", "").rsplit(":", 1)
            src_ip   = local[0] if len(local) > 0 else ""
            src_port = int(local[1]) if len(local) > 1 and local[1].isdigit() else 0
            self._correlation.ingest_kernel_event({
                "type":         "tcp_connection",
                "pid":          pid,
                "process_name": ev.get("process", ""),
                "src_ip":       src_ip,
                "src_port":     src_port,
                "dst_ip":       ev.get("remote_ip", ""),
                "dst_port":     ev.get("remote_port", 0),
                "timestamp":    ev.get("timestamp", time.time()),
            })

    def _process_etw(self):
        """ETW → CorrelationEngine + eventos de lifecycle para o runtime."""
        etw_events = self._etw.drain()
        if not etw_events:
            return

        for ev in etw_events:
            # Alimentar o correlation engine
            self._correlation.ingest_kernel_event(ev)

            # Eventos de processo → enfileirar para o RuntimeAgent
            if ev.get("type") == "process_lifecycle":
                self._queue_event({
                    "event_type":  "process_lifecycle",
                    "severity":    "info",
                    "entity_type": "process",
                    "entity_name": ev.get("process_name", ""),
                    "summary": (
                        f"Processo {ev.get('action', '')}: "
                        f"{ev.get('process_name', '')} (pid={ev.get('pid', 0)})"
                    ),
                    "payload": ev,
                })

            # DNS queries → enfileirar
            elif ev.get("type") == "dns_query" and ev.get("query"):
                self._queue_event({
                    "event_type":  "dns_query",
                    "severity":    "info",
                    "entity_type": "network",
                    "entity_name": ev.get("query", ""),
                    "summary":     f"DNS: {ev.get('query', '')} por pid={ev.get('pid', 0)}",
                    "payload":     ev,
                })

    def _process_packets(self):
        """WinDivert/raw → ProtocolDecoder → CorrelationEngine → SpanEmitter."""
        raw_packets = self._capture.drain()
        if not raw_packets:
            return

        decoded_batch = []
        for pkt in raw_packets:
            result = self._decoder.decode(pkt)
            if result:
                decoded_batch.append(result)
                self._correlation.ingest_decoded_packet(result)

        if decoded_batch:
            self._emitter.emit_decoded_spans(decoded_batch)

    def _flush_execution_units(self):
        """CorrelationEngine → execution units fechadas → SpanEmitter + RuntimeAgent."""
        units = self._correlation.drain()
        if not units:
            return

        for unit in units:
            # Emitir como span OTel enriquecido
            self._emitter.emit_execution_unit(unit)

            # Enfileirar para o RuntimeAgent se tiver interesse (alta confiança)
            if unit.get("confidence") in ("high", "medium") and unit.get("event_count", 0) >= 1:
                protocols = ", ".join(unit.get("protocols", [])) or "tcp"
                self._queue_event({
                    "event_type":  "execution_unit",
                    "severity":    "info",
                    "entity_type": "process",
                    "entity_name": unit.get("process_name", "unknown"),
                    "summary": (
                        f"{unit.get('process_name', 'unknown')} — "
                        f"{unit.get('event_count', 0)} operações [{protocols}] "
                        f"em {unit.get('duration_ms', 0):.1f}ms "
                        f"[{unit.get('confidence', 'low')}]"
                    ),
                    "payload": {
                        "unit_id":      unit.get("unit_id"),
                        "pid":          unit.get("pid"),
                        "process_name": unit.get("process_name", ""),
                        "trace_id":     unit.get("trace_id"),
                        "duration_ms":  unit.get("duration_ms"),
                        "confidence":   unit.get("confidence"),
                        "protocols":    unit.get("protocols"),
                        "sources":      unit.get("sources"),
                        "event_count":  unit.get("event_count"),
                    },
                })

    def _emit_topology(self):
        """Emite frame de topologia periodicamente."""
        now = time.time()
        if now - self._last_topology_emit < _TOPOLOGY_EMIT_INTERVAL:
            return
        self._last_topology_emit = now

        data = self._topology.get_topology_frame_data()
        if data["dependency_count"] == 0:
            return

        self._queue_event({
            "event_type":  "network_topology",
            "severity":    "info",
            "entity_type": "host",
            "entity_name": "network",
            "summary": (
                f"Topologia: {data['service_count']} serviços, "
                f"{data['dependency_count']} dependências"
            ),
            "payload": data,
        })

    # ─── INGESTÃO DE SPANS OTEL ───

    def ingest_otel_span(self, span: dict):
        """
        Chamado pelo OtelReceiver quando chega um span de user-space.
        Alimenta o CorrelationEngine para enriquecer execution units existentes.
        """
        self._correlation.ingest_otel_span(span)

    # ─── HELPERS ───

    def _queue_event(self, event: dict):
        with self._lock:
            self._pending_events.append(event)
