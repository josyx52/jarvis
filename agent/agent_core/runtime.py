from copy import deepcopy

import agent_core.config as cfg
from agent_core.config import (
    AGENT_VERSION,
    COLLECTION_INTERVAL_SECONDS,
    ENABLE_LOGS,
    ENABLE_TRACES,
    INVENTORY_INTERVAL_SECONDS,
    LOGS_INTERVAL_SECONDS,
    METRICS_INTERVAL_SECONDS,
    OTEL_FLUSH_INTERVAL_SECONDS,
    AUTODISCOVERY_INTERVAL_SECONDS,
    NETWORK_PROBE_ENABLED,
    NETWORK_PROBE_OTEL_ENDPOINT,
    PROCESS_INJECTOR_ENABLED,
)

from agent_core.telemetry_schema import build_frame, build_otel_frame, build_specialized_frame, build_execution_unit_frame, build_security_frame

from detection.event_engine import EventEngine
from registry.collector_registry import CollectorRegistry
from discovery.discovery_engine import DiscoveryEngine

from storage.queue_store import QueueStore
from storage.sqlite_store import SQLiteStore

from utils.system_identity import build_host_identity


class RuntimeAgent:

    def __init__(self):

        self.store = SQLiteStore()
        self.queue = QueueStore(self.store)

        # Registry inicializa todos os collectors conforme config
        self.registry = CollectorRegistry()

        # Atalhos para collectors core (acesso direto no ciclo)
        collectors = self.registry.core_collectors()
        self._metrics  = self._find(collectors, "MetricsCollector")
        self._osquery  = self._find(collectors, "OsqueryCollector")
        self._connections = self._find(collectors, "ConnectionsCollector")
        self._logs     = self._find(collectors, "LogsCollector")
        self._otel     = self.registry.otel_receiver()

        # Security events collector — alimenta UEBA no center
        try:
            from collectors.security_events_collector import SecurityEventsCollector
            self._security = SecurityEventsCollector()
        except Exception:
            self._security = None

        self.event_engine = EventEngine()

        # DiscoveryEngine — corre em background
        self.discovery = DiscoveryEngine()
        self.discovery.start()

        # Network Probe (Architecture B) — corre em background
        self.probe = None
        if NETWORK_PROBE_ENABLED:
            try:
                from network_probe.probe_manager import ProbeManager
                self.probe = ProbeManager(
                    otel_endpoint=NETWORK_PROBE_OTEL_ENDPOINT,
                    process_injection=PROCESS_INJECTOR_ENABLED,
                )
                self.probe.start()
            except Exception as e:
                print(f"[Runtime] Network probe nao iniciado: {e}")

        self.sequence = 0
        self.tick     = 0

        self.host_identity             = build_host_identity(AGENT_VERSION)
        self.collection_interval_seconds = COLLECTION_INTERVAL_SECONDS

        self.last_metrics   = None
        self.last_inventory = None
        self.last_connections = []
        self.last_logs      = []

        self.snapshot_history: list[dict] = []

    def _find(self, collectors, class_name):
        for c in collectors:
            if type(c).__name__ == class_name:
                return c
        return None

    def _next_sequence(self):
        self.sequence += 1
        return self.sequence

    def _should_collect(self, interval):
        if interval <= 1:
            return True
        return self.tick % interval == 0

    # ---------------------------
    # FRAMES CORE
    # ---------------------------

    def _build_heartbeat_frame(self):
        return build_frame("heartbeat", self.host_identity, self._next_sequence(), {})

    def _build_metrics_frame(self):
        if not self._metrics:
            return None
        metrics = self._metrics.collect()
        return build_frame("metrics", self.host_identity, self._next_sequence(), {"metrics": metrics})

    def _build_inventory_frame(self):
        if not self._osquery:
            return None
        inventory = self._osquery.collect_inventory_snapshot()
        return build_frame("inventory", self.host_identity, self._next_sequence(), {
            "processes": inventory["processes"],
            "services":  inventory["services"],
            "disk":      inventory["disk"],
            "network":   inventory["network"],
        })

    def _build_connections_frame(self):
        if not self._connections:
            return None
        connections = self._connections.collect()
        return build_frame("connections", self.host_identity, self._next_sequence(), {"connections": connections})

    def _build_logs_frame(self):
        if not self._logs:
            return None
        logs = self._logs.collect()
        return build_frame("logs", self.host_identity, self._next_sequence(), {"logs": logs})

    def _build_security_frame(self):
        if not self._security:
            return None
        events = self._security.collect(limit=100)
        if not events:
            return None
        return build_security_frame(self.host_identity, self._next_sequence(), events)

    def _build_events_frame(self, events: list[dict]):
        return build_frame("events", self.host_identity, self._next_sequence(), {"events": events})

    # ---------------------------
    # FRAMES ESPECIALIZADOS
    # ---------------------------

    def _build_specialized_frames(self) -> list[dict]:
        frames = []
        for collector in self.registry.specialized_collectors():
            try:
                results = collector.collect()
                for data in (results or []):
                    frame = build_specialized_frame(
                        frame_type=collector.FRAME_TYPE,
                        host=self.host_identity,
                        sequence=self._next_sequence(),
                        data=data,
                    )
                    frames.append(frame)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"[RUNTIME] Erro no collector {type(collector).__name__}: {e}"
                )
        return frames

    # ---------------------------
    # ESTADO CANONICO
    # ---------------------------

    def _update_latest_state(self, frame: dict):
        if not frame:
            return
        ft = frame.get("frame_type")
        t  = frame.get("telemetry", {})
        if ft == "metrics":
            self.last_metrics = t.get("metrics")
        elif ft == "inventory":
            self.last_inventory = {k: t.get(k, []) for k in ("processes", "services", "disk", "network")}
        elif ft == "connections":
            self.last_connections = t.get("connections", [])
        elif ft == "logs":
            self.last_logs = t.get("logs", [])

    def _build_canonical_snapshot(self):
        if not self.last_inventory:
            return None
        return {
            "host":      deepcopy(self.host_identity),
            "metrics":   deepcopy(self.last_metrics) if self.last_metrics else {},
            "processes": deepcopy(self.last_inventory.get("processes", [])),
            "services":  deepcopy(self.last_inventory.get("services", [])),
            "network":   deepcopy(self.last_inventory.get("network", [])),
            "disk":      deepcopy(self.last_inventory.get("disk", [])),
            "logs":        deepcopy(self.last_logs),
            "connections": deepcopy(self.last_connections),
        }

    def _push_snapshot_history(self, snapshot: dict):
        self.snapshot_history.insert(0, snapshot)
        self.snapshot_history = self.snapshot_history[:3]

    # ---------------------------
    # RUN CYCLE
    # ---------------------------

    def run_cycle(self):

        self.tick += 1
        frames_to_send: list[dict] = []
        telemetry_changed = False

        # Heartbeat
        frames_to_send.append(self._build_heartbeat_frame())

        # Core collectors
        if self._should_collect(METRICS_INTERVAL_SECONDS):
            frame = self._build_metrics_frame()
            if frame:
                self._update_latest_state(frame)
                frames_to_send.append(frame)
                telemetry_changed = True

        if self._should_collect(INVENTORY_INTERVAL_SECONDS):
            frame = self._build_inventory_frame()
            if frame:
                self._update_latest_state(frame)
                frames_to_send.append(frame)
                telemetry_changed = True

        if ENABLE_TRACES and self._should_collect(cfg.TRACES_INTERVAL_SECONDS):
            frame = self._build_connections_frame()
            if frame:
                self._update_latest_state(frame)
                frames_to_send.append(frame)
                telemetry_changed = True

        if ENABLE_LOGS and self._should_collect(cfg.LOGS_INTERVAL_SECONDS):
            frame = self._build_logs_frame()
            if frame:
                self._update_latest_state(frame)
                frames_to_send.append(frame)

        # Security events — a cada 30s (intervalo fixo, independente dos logs)
        if self._should_collect(30):
            frame = self._build_security_frame()
            if frame:
                frames_to_send.append(frame)
                telemetry_changed = True

        # Eventos locais
        if telemetry_changed:
            snapshot = self._build_canonical_snapshot()
            if snapshot:
                self._push_snapshot_history(snapshot)
                if len(self.snapshot_history) >= 2:
                    events = self.event_engine.build_events(self.snapshot_history)
                    if events:
                        frames_to_send.append(self._build_events_frame(events))

        # OTel spans — alimentar o CorrelationEngine antes de enfileirar
        if self._otel and self._should_collect(OTEL_FLUSH_INTERVAL_SECONDS):
            spans = self._otel.drain()
            if spans:
                if self.probe:
                    for span in spans:
                        self.probe.ingest_otel_span(span)
                frames_to_send.append(build_otel_frame(
                    host=self.host_identity,
                    sequence=self._next_sequence(),
                    spans=spans,
                ))

        # Collectors especializados (DB, Kafka, Web, ...)
        if self._should_collect(30):  # a cada 30 ciclos
            frames_to_send.extend(self._build_specialized_frames())

        # Eventos do DiscoveryEngine
        discovery_events = self.discovery.drain()
        if discovery_events:
            frames_to_send.append(self._build_events_frame(discovery_events))

        # Network Probe — execution units correlacionadas + eventos de topologia
        if self.probe:
            probe_events = self.probe.drain()
            if probe_events:
                # Separar execution units dos restantes eventos de topologia/lifecycle
                exec_units = [e for e in probe_events if e.get("event_type") == "execution_unit"]
                other_events = [e for e in probe_events if e.get("event_type") != "execution_unit"]

                if exec_units:
                    # Execution units → frame próprio (resultado do CorrelationEngine)
                    frames_to_send.append(build_execution_unit_frame(
                        host=self.host_identity,
                        sequence=self._next_sequence(),
                        units=[e.get("payload", e) for e in exec_units],
                    ))

                if other_events:
                    frames_to_send.append(self._build_events_frame(other_events))

        for frame in frames_to_send:
            self.queue.enqueue(frame)
