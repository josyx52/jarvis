import logging
import time

from processor.snapshot_builder import SnapshotBuilder

log = logging.getLogger("jarvis.central_runtime")
from state.host_state_store import HostStateStore

from engines.event_engine import EventEngine
from engines.correlator import Correlator
from engines.predictor import Predictor
from engines.alert_manager import AlertManager
from engines.trace_engine import TraceEngine
from engines.db_engine import DbEngine
from engines.kafka_engine import KafkaEngine
from engines.web_engine import WebEngine
from engines.baseline_engine import BaselineEngine
from engines.problem_engine import ProblemEngine
from engines.instrumentation_engine import InstrumentationEngine
from engines.adaptive_threshold_engine import AdaptiveThresholdEngine
from engines.solution_engine import SolutionEngine

from storage.memory_store import MemoryStore
from security.user_activity_store import UserActivityStore
from security.ueba_engine import UEBAEngine
from security.user_profiler import UserProfiler


class CentralRuntime:

    def __init__(self):

        self.state_store = HostStateStore()
        self.snapshot_builder = SnapshotBuilder()

        self.event_engine = EventEngine()
        self.correlator = Correlator()
        self.predictor = Predictor()
        self.alert_manager = AlertManager()
        self.trace_engine = TraceEngine()
        self.db_engine = DbEngine()
        self.kafka_engine = KafkaEngine()
        self.web_engine = WebEngine()
        self.baseline_engine = BaselineEngine()
        self.problem_engine          = ProblemEngine()
        self.instrumentation_engine  = InstrumentationEngine()
        self.adaptive_threshold_engine = AdaptiveThresholdEngine()
        self.baseline_engine.set_threshold_engine(self.adaptive_threshold_engine)
        self.solution_engine = SolutionEngine()

        self.memory = MemoryStore()

        # UEBA — camada de segurança comportamental
        self.user_activity_store = UserActivityStore()
        self.ueba_engine         = UEBAEngine()
        self.user_profiler       = UserProfiler()

        self.snapshots_by_host = {}

        # Controlo de frequência de snapshots por host
        # {host_key → (last_ts, last_cpu, last_mem, last_disk)}
        self._last_snapshot_meta: dict[str, tuple] = {}

        # TTL para evicção de hosts inativos (1 hora sem frames)
        self._host_last_seen: dict[str, float] = {}
        self._HOST_EVICTION_TTL = 3600
        self._last_eviction_run = 0
        self._EVICTION_INTERVAL = 300

    # --------------------------------
    # EVICÇÃO DE HOSTS INATIVOS
    # --------------------------------

    def _evict_stale_hosts(self):
        now = time.time()
        if (now - self._last_eviction_run) < self._EVICTION_INTERVAL:
            return
        self._last_eviction_run = now
        stale = [
            hk for hk, last_seen in self._host_last_seen.items()
            if (now - last_seen) > self._HOST_EVICTION_TTL
        ]
        for hk in stale:
            self.snapshots_by_host.pop(hk, None)
            self._last_snapshot_meta.pop(hk, None)
            self._host_last_seen.pop(hk, None)
        if stale:
            log.info("eviction: %d host(s) inativos removidos da memória", len(stale))

    # --------------------------------
    # HOST KEY
    # --------------------------------

    def _host_key(self, state):

        host = state.get("host", {}) or {}

        hostname = host.get("hostname", "unknown")
        boot_id = host.get("boot_id", "default")

        return f"{hostname}::{boot_id}"

    # --------------------------------
    # INVENTORY CHECK
    # --------------------------------

    def _has_inventory_base(self, state: dict) -> bool:
        # Aceita métricas como base suficiente — sem depender de osquery/inventory
        return bool(
            state.get("metrics")
            or state.get("processes")
            or state.get("services")
            or state.get("network")
            or state.get("disk")
        )

    # --------------------------------
    # NORMALIZE EVENTS
    # --------------------------------

    def _normalize_raw_events(self, raw_events):

        normalized = []

        for event in raw_events or []:
            if not isinstance(event, dict):
                continue

            normalized.append({
                "event_type": event.get("event_type") or "agent_event",
                "severity": event.get("severity", "medium"),
                "entity_type": event.get("entity_type", "host"),
                "entity_name": event.get("entity_name", "agent"),
                "summary": event.get("summary") or "Evento recebido do agente",
                "payload": event
            })

        return normalized

    # --------------------------------
    # SNAPSHOT THROTTLE
    # --------------------------------

    _SNAPSHOT_MIN_INTERVAL = 60   # segundos mínimos entre snapshots do mesmo host
    _SNAPSHOT_DELTA_PCT    = 5.0  # % de variação mínima para forçar snapshot

    def _should_save_snapshot(self, host_key: str, snapshot: dict) -> bool:
        """Retorna True só se passou tempo suficiente OU houve delta significativo."""
        import time as _time
        metrics = snapshot.get("metrics") or {}
        cpu  = float(metrics.get("cpu_percent")    or 0)
        mem  = float(metrics.get("memory_percent") or 0)
        disk = float(metrics.get("disk_percent")   or 0)
        now  = _time.time()

        meta = self._last_snapshot_meta.get(host_key)
        if meta is None:
            self._last_snapshot_meta[host_key] = (now, cpu, mem, disk)
            return True

        last_ts, last_cpu, last_mem, last_disk = meta
        elapsed = now - last_ts

        delta = max(abs(cpu - last_cpu), abs(mem - last_mem), abs(disk - last_disk))

        if elapsed >= self._SNAPSHOT_MIN_INTERVAL or delta >= self._SNAPSHOT_DELTA_PCT:
            self._last_snapshot_meta[host_key] = (now, cpu, mem, disk)
            return True

        return False

    # --------------------------------
    # BASELINE HELPERS
    # --------------------------------

    def _extract_trace_stats(self, spans: list) -> list[dict]:
        """Agrega spans por endpoint para alimentar o BaselineEngine."""
        if not spans:
            return []
        by_ep: dict[str, list] = {}
        for s in spans:
            key = f"{s.get('service','?')}::{s.get('name','?')}"
            by_ep.setdefault(key, []).append(s)
        stats = []
        for key, ep_spans in by_ep.items():
            svc, op = key.split("::", 1)
            durations = sorted(s.get("duration_ms", 0) for s in ep_spans)
            n      = len(durations)
            p95_ms = durations[int(n * 0.95)] if n >= 2 else (durations[0] if n else 0)
            errors = sum(1 for s in ep_spans if s.get("status") == "error")
            stats.append({
                "service":    svc,
                "operation":  op,
                "p95_ms":     p95_ms,
                "error_rate": errors / n if n else 0,
            })
        return stats

    def _extract_db_stats(self, db_telemetry: list) -> list[dict]:
        """Extrai métricas de DB para alimentar o BaselineEngine."""
        stats = []
        for data in db_telemetry:
            db_name = data.get("database") or data.get("db") or "unknown"
            queries = data.get("slow_queries") or data.get("queries") or []
            avg_ms  = 0.0
            if queries:
                durations = [q.get("avg_ms") or q.get("duration_ms") or 0 for q in queries]
                avg_ms    = sum(durations) / len(durations)
            conn_pct = data.get("connection_pool_pct") or data.get("connections_pct") or 0
            stats.append({"db": db_name, "avg_query_ms": avg_ms, "connections_pct": conn_pct})
        return stats

    # --------------------------------
    # SECURITY / UEBA PROCESSING
    # --------------------------------

    def _process_security_frame(self, frame: dict):
        """
        Processa um frame security_events vindo do SecurityEventsCollector.
        1. Persiste eventos em user_activity_events
        2. Para eventos de alto risco → análise UEBA imediata
        3. Para utilizadores activos → análise periódica e profiling
        """
        try:
            host   = (frame.get("host") or {}).get("hostname", "unknown")
            events = (frame.get("telemetry") or {}).get("events") or []

            if not events:
                return

            # 1. Persistir todos os eventos
            self.user_activity_store.bulk_insert(host, events)

            # 2. Verificar eventos de alto risco → análise imediata
            high_risk = [e for e in events if e.get("severity") in ("high", "critical")]
            analyzed_users: set[str] = set()

            for ev in high_risk:
                username = ev.get("username", "")
                if not username or username in analyzed_users:
                    continue
                analyzed_users.add(username)
                try:
                    self.ueba_engine.analyze_high_risk(
                        host, username, ev, self.user_activity_store
                    )
                except Exception as e:
                    log.exception("UEBA análise imediata falhou para %s@%s", username, host)

            # 3. Análise periódica + profiling para todos os utilizadores activos
            active_users = {e.get("username", "") for e in events if e.get("username")}
            for username in active_users:
                if not username:
                    continue
                try:
                    self.ueba_engine.analyze_if_due(
                        host, username, self.user_activity_store
                    )
                    self.user_profiler.profile_if_due(
                        host, username, self.user_activity_store
                    )
                except Exception as e:
                    log.exception("UEBA análise periódica falhou para %s@%s", username, host)

        except Exception as e:
            log.exception("security frame processing error")

    # --------------------------------
    # MAIN PROCESSING
    # --------------------------------

    def process_frame(self, frame):

        self._evict_stale_hosts()

        state = self.state_store.update(frame)

        if not state:
            return None

        frame_type = state.get("last_frame_type")
        host_key_early = self._host_key(state)
        self._host_last_seen[host_key_early] = time.time()

        # OTel traces guardadas independentemente do inventory base
        # (o processor pode nao ter inventory para este host ainda)
        if frame_type == "otel_trace":
            otel_spans = state.get("otel_spans") or []
            if otel_spans:
                try:
                    host_key_for_traces = self._host_key(state)
                    self.memory.save_traces(host_key_for_traces, otel_spans)
                    state["otel_spans"] = []
                except Exception as e:
                    log.exception("trace save error")

        if frame_type == "execution_unit":
            units = state.get("execution_units") or []
            if units:
                try:
                    host_key_for_units = self._host_key(state)
                    self.memory.save_execution_units(host_key_for_units, units)
                    state["execution_units"] = []
                except Exception as e:
                    log.exception("execution unit save error")

        if frame_type == "security_events":
            self._process_security_frame(frame)
            return {"status": "security_events_processed"}

        if not self._has_inventory_base(state):
            return {"status": "waiting_inventory"}

        snapshot = self.snapshot_builder.build(state)

        if not snapshot:
            return None

        host_key = self._host_key(state)

        history = self.snapshots_by_host.setdefault(host_key, [])
        history.insert(0, snapshot)
        history[:] = history[:5]

        # --------------------------------
        # OTEL TRACES (auto-instrumentacao)
        # --------------------------------

        otel_spans = state.get("otel_spans") or []
        trace_events = self.trace_engine.analyze(otel_spans) if otel_spans else []

        if otel_spans:
            try:
                host_key_for_traces = self._host_key(state)
                self.memory.save_traces(host_key_for_traces, otel_spans)
            except Exception as e:
                log.exception("trace save error (otel)")

        # --------------------------------
        # SPECIALIZED TELEMETRY ENGINES
        # --------------------------------

        db_events    = []
        kafka_events = []
        web_events   = []

        for db_data in state.get("db_telemetry") or []:
            db_events.extend(self.db_engine.analyze(db_data))

        for kafka_data in state.get("kafka_telemetry") or []:
            kafka_events.extend(self.kafka_engine.analyze(kafka_data))

        for web_data in state.get("web_telemetry") or []:
            web_events.extend(self.web_engine.analyze(web_data, host=host_key))

        # --------------------------------
        # BASELINE — alimentar + detectar anomalias (sem thresholds fixos)
        # --------------------------------

        trace_stats = self._extract_trace_stats(otel_spans)
        db_stats    = self._extract_db_stats(state.get("db_telemetry") or [])

        self.baseline_engine.ingest(host_key, snapshot, trace_stats, db_stats)
        baseline_events = self.baseline_engine.detect(host_key, snapshot, trace_stats, db_stats)

        # --------------------------------
        # EVENTS
        # --------------------------------

        events = []
        events.extend(self._normalize_raw_events(state.get("raw_events") or []))
        events.extend(self.event_engine.build_events(history))
        events.extend(trace_events)
        events.extend(db_events)
        events.extend(kafka_events)
        events.extend(web_events)
        events.extend(baseline_events)

        # --------------------------------
        # CORRELATION
        # --------------------------------

        topology = {}
        correlations = self.correlator.correlate(events, topology)

        # --------------------------------
        # PREDICTIONS
        # --------------------------------

        predictions = []
        if len(history) >= 3:
            predictions = self.predictor.predict(history)

        # --------------------------------
        # ALERTS
        # --------------------------------

        alerts = self.alert_manager.generate_alerts(
            events,
            correlations,
            predictions,
            host=host_key
        )

        # --------------------------------
        # PROBLEM GROUPING + LIFECYCLE
        # --------------------------------

        problems = self.problem_engine.process(
            host_key     = host_key,
            alerts       = alerts,
            events       = events,
            correlations = correlations,
        )

        # --------------------------------
        # ADAPTIVE THRESHOLD FEEDBACK
        # Regista alertas de baseline e se resultaram num Problem activo.
        # Usado pelo AdaptiveThresholdEngine para ajustar sigma por série.
        # --------------------------------

        if alerts:
            problem_titles = set()
            for p in problems:
                if p.get("status") == "open":
                    problem_titles.update(p.get("alert_titles") or [])

            for alert in alerts:
                series_key = (alert.get("payload") or {}).get("series_key")
                if series_key:
                    became_problem = alert.get("title", "") in problem_titles
                    self.adaptive_threshold_engine.record_alert(series_key, became_problem)

        # --------------------------------
        # NOTA: a investigação (InvestigationEngine) já não corre automaticamente
        # aqui — só é disparada sob pedido explícito do utilizador, via
        # POST /investigations/trigger (ver api/ingestion_api.py), a partir de
        # um alerta já persistido. Ver engines/investigation_engine.py.

        # --------------------------------
        # SOLUTION DRIVER
        # Avalia problems significativos e dispara notificação + conversa.
        # Só actua após 5 min de problema aberto — não bloqueia o frame.
        # --------------------------------

        if problems:
            try:
                self.solution_engine.maybe_trigger(
                    host_key = host_key,
                    problems = problems,
                    snapshot = snapshot,
                )
            except Exception as e:
                log.exception("solution engine error")

        # --------------------------------
        # SAVE
        # --------------------------------

        snapshot["events"] = events

        try:
            if self._should_save_snapshot(host_key, snapshot):
                self.memory.save_snapshot(host_key, snapshot)

            if events:
                self.memory.save_events(host_key, events)

            if correlations:
                self.memory.save_correlations(host_key, correlations)

            if predictions:
                self.memory.save_predictions(host_key, predictions)

            if alerts:
                self.memory.save_alerts(host_key, alerts)

            if problems:
                self.memory.save_problems(host_key, problems)

            # AI-assisted instrumentation gap analysis
            # Runs only every _COOLDOWN_S (10 min) per host — cheap when no gaps
            if alerts or problems:
                try:
                    instr_recs = self.instrumentation_engine.analyze_gaps(
                        host_key = host_key,
                        snapshot = snapshot,
                        problems = problems,
                        alerts   = alerts,
                        traces   = otel_spans,
                    )
                    if instr_recs:
                        self.memory.save_instrumentation_recommendations(host_key, instr_recs)
                        log.info("instrumentation: %d recomendação(ões) para %s", len(instr_recs), host_key)
                except Exception as e:
                    log.exception("instrumentation engine error")

            self.memory.flush()

        except Exception as e:
            log.exception("memory store error")

        return {
            "analysis_triggered": True,
            "events":   len(events),
            "alerts":   len(alerts),
            "problems": len(problems),
        }