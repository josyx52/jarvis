"""
CorrelationEngine — reconstrói comportamento de execução a partir de sinais parciais.

Correlaciona três fontes de dados:
  1. Kernel (ETW)      — processo create/exit, TCP connect/accept com PID, DNS
  2. Rede (WinDivert)  — payloads TCP decodificados: HTTP, SQL, Redis, Kafka, etc.
  3. User-space (OTel) — spans da instrumentação manual ou automática

Estratégia de correlação:
  Forte  (high)   — trace_id disponível OU conexão TCP → PID via tabela ETW
  Média  (medium) — PID + janela temporal de 10ms
  Fraca  (low)    — apenas timestamp + porto de destino (sem PID conhecido)

Output: "execution units" — conjuntos de eventos que pertencem ao mesmo pedido.
Emitidas quando a janela temporal fecha (TTL de 500ms por omissão).
"""

import threading
import time
import uuid
from collections import defaultdict

import psutil


_WINDOW_NS      = 10_000_000    # 10ms — janela de correlação forte
_WINDOW_ASYNC   = 500_000_000   # 500ms — janela para operações com queueing
_UNIT_TTL       = 0.5           # segundos antes de fechar uma execution unit
_CLEANUP_EVERY  = 1             # ciclos entre limpezas (sweep a cada drain)


class ExecutionUnit:
    """
    Representa um conjunto de eventos que pertencem ao mesmo pedido/operação.
    Fechada quando nenhum evento novo chega dentro do TTL.
    """

    def __init__(self, unit_id: str, pid: int, process_name: str):
        self.unit_id      = unit_id
        self.pid          = pid
        self.process_name = process_name
        self.trace_id     = None
        self.events: list = []
        self.first_ts     = time.time()
        self.last_ts      = self.first_ts
        self.confidence   = "low"

    def add_event(self, event: dict):
        self.events.append(event)
        self.last_ts = time.time()

        # Promover confiança se trace_id disponível
        if event.get("trace_id") and not self.trace_id:
            self.trace_id = event["trace_id"]
            self.confidence = "high"
        elif self.confidence == "low" and event.get("pid"):
            self.confidence = "medium"

    def is_expired(self) -> bool:
        return (time.time() - self.last_ts) > _UNIT_TTL

    def to_dict(self) -> dict:
        duration_ms = (self.last_ts - self.first_ts) * 1000
        return {
            "unit_id":        self.unit_id,
            "pid":            self.pid,
            "process_name":   self.process_name,
            "trace_id":       self.trace_id,
            "confidence":     self.confidence,
            "duration_ms":    round(duration_ms, 3),
            "first_ts":       self.first_ts,
            "last_ts":        self.last_ts,
            "event_count":    len(self.events),
            "events":         self.events,
            "sources":        list({e.get("source", "unknown") for e in self.events}),
            "protocols":      list({
                (e.get("payload") or {}).get("protocol", "")
                for e in self.events
                if (e.get("payload") or {}).get("protocol")
            }),
        }


class CorrelationEngine:

    def __init__(self, etw_consumer=None):
        self._etw             = etw_consumer   # referência ao EtwConsumer (para tabela PID)
        self._units: dict     = {}             # unit_id → ExecutionUnit
        self._pid_units: dict = defaultdict(list)  # pid → [unit_id] (unidades abertas)
        self._completed: list = []             # unidades fechadas prontas a emitir
        self._lock            = threading.Lock()
        self._cleanup_tick    = 0

        # Cache PID → nome do processo (fallback quando ETW não disponível)
        self._pid_cache: dict = {}
        self._pid_cache_ts    = 0.0

    # ─── INGESTÃO DE EVENTOS ───

    def ingest_kernel_event(self, event: dict):
        """Evento ETW: processo create/exit, TCP connect, DNS query."""
        ev_type = event.get("type", "")

        if ev_type == "process_lifecycle":
            action = event.get("action", "")
            pid    = event.get("pid", 0)
            name   = event.get("process_name", "")

            if action == "start" and pid:
                with self._lock:
                    self._pid_cache[pid] = name
            elif action == "exit" and pid:
                with self._lock:
                    self._pid_cache.pop(pid, None)
                    # Fechar unidades abertas deste processo
                    for uid in list(self._pid_units.get(pid, [])):
                        unit = self._units.get(uid)
                        if unit:
                            self._close_unit(uid)

        elif ev_type in ("tcp_connection", "dns_query"):
            pid = event.get("pid", 0)
            if pid:
                unit = self._get_or_create_unit(pid, event.get("timestamp", time.time()))
                unit.add_event(self._normalize_event(event, "kernel"))

    def ingest_decoded_packet(self, packet: dict):
        """
        Pacote TCP decodificado pelo ProtocolDecoder.
        Resolve PID via tabela ETW. Fallback: psutil.
        """
        src_ip   = packet.get("src_ip", "")
        src_port = packet.get("src_port", 0)
        dst_ip   = packet.get("dst_ip", "")
        dst_port = packet.get("dst_port", 0)
        ts       = packet.get("timestamp", time.time())

        pid, confidence = self._resolve_pid(src_ip, src_port, dst_ip, dst_port)

        unit = self._get_or_create_unit(pid, ts)

        event = self._normalize_event({
            "type":    "network_request",
            "pid":     pid,
            "src_ip":  src_ip, "src_port": src_port,
            "dst_ip":  dst_ip, "dst_port": dst_port,
            "timestamp": ts,
            "payload": {k: v for k, v in packet.items()
                        if k not in ("src_ip", "src_port", "dst_ip", "dst_port",
                                     "payload", "timestamp")},
        }, "network")

        event["correlation_confidence"] = confidence
        unit.add_event(event)
        if confidence == "high":
            unit.confidence = "high"

    def ingest_otel_span(self, span: dict):
        """
        Span OTel vindo do receiver local (porta 4318).
        Correlaciona por trace_id se disponível, fallback por PID + timestamp.
        """
        trace_id = span.get("trace_id") or span.get("traceId", "")
        pid      = span.get("pid", 0) or self._pid_from_service(span.get("service_name", ""))
        ts       = self._span_ts(span)

        # Procurar unidade existente com este trace_id
        unit = None
        if trace_id:
            with self._lock:
                for u in self._units.values():
                    if u.trace_id == trace_id:
                        unit = u
                        break

        if not unit:
            unit = self._get_or_create_unit(pid, ts)

        event = self._normalize_event({
            "type":      "span",
            "pid":       pid,
            "trace_id":  trace_id,
            "span_id":   span.get("span_id") or span.get("spanId", ""),
            "name":      span.get("name") or span.get("operation_name", ""),
            "timestamp": ts,
            "duration_ns": span.get("duration_ns", 0),
        }, "userspace")

        unit.add_event(event)

    # ─── DRAIN ───

    def drain(self) -> list:
        """Devolve execution units fechadas, prontas a emitir."""
        self._cleanup_tick += 1
        if self._cleanup_tick % _CLEANUP_EVERY == 0:
            self._sweep_expired()

        with self._lock:
            completed      = self._completed[:]
            self._completed = []
        return completed

    # ─── GESTÃO DE EXECUTION UNITS ───

    def _get_or_create_unit(self, pid: int, ts: float) -> ExecutionUnit:
        """
        Devolve a unidade activa para este PID, ou cria uma nova.
        Usa a última unidade aberta dentro da janela temporal.
        """
        with self._lock:
            # Procurar unidade aberta recente para este PID
            for uid in reversed(self._pid_units.get(pid, [])):
                unit = self._units.get(uid)
                if unit and not unit.is_expired():
                    if (ts - unit.last_ts) < _UNIT_TTL:
                        return unit

            # Criar nova unidade
            uid          = str(uuid.uuid4())
            proc_name    = self._resolve_process_name(pid)
            unit         = ExecutionUnit(uid, pid, proc_name)
            self._units[uid] = unit
            self._pid_units[pid].append(uid)
            return unit

    def _close_unit(self, uid: str):
        """Fechar unidade e mover para completed. Deve ser chamado com lock."""
        unit = self._units.pop(uid, None)
        if unit and unit.events:
            self._completed.append(unit.to_dict())
        # Remover de pid_units
        pid = unit.pid if unit else 0
        if pid in self._pid_units:
            try:
                self._pid_units[pid].remove(uid)
            except ValueError:
                pass

    def _sweep_expired(self):
        """Fechar todas as unidades expiradas."""
        with self._lock:
            expired = [uid for uid, u in self._units.items() if u.is_expired()]
            for uid in expired:
                self._close_unit(uid)

    # ─── RESOLUÇÃO DE PID ───

    def _resolve_pid(self, src_ip: str, src_port: int,
                     dst_ip: str, dst_port: int) -> tuple[int, str]:
        """
        Tenta resolver o PID desta conexão.
        1. Via tabela ETW (forte — event-driven, em tempo real)
        2. Via psutil (médio — polling, pode ter latência)
        3. Sem PID (fraco — apenas topologia)
        """
        # 1. ETW connection table
        if self._etw:
            pid = self._etw.get_pid_for_connection(src_ip, src_port, dst_ip, dst_port)
            if pid:
                return pid, "high"

        # 2. psutil net_connections
        pid = self._pid_from_psutil(src_ip, src_port, dst_ip, dst_port)
        if pid:
            return pid, "medium"

        return 0, "low"

    def _pid_from_psutil(self, src_ip: str, src_port: int,
                         dst_ip: str, dst_port: int) -> int:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if not conn.pid:
                    continue
                if conn.laddr and conn.raddr:
                    if (conn.laddr.port == src_port and conn.raddr.ip == dst_ip
                            and conn.raddr.port == dst_port):
                        return conn.pid
                    if (conn.laddr.port == dst_port and conn.raddr.ip == src_ip
                            and conn.raddr.port == src_port):
                        return conn.pid
        except Exception:
            pass
        return 0

    def _resolve_process_name(self, pid: int) -> str:
        if pid == 0:
            return "unknown"

        # Cache ETW
        if self._etw:
            name = self._etw.get_process_name(pid)
            if name:
                return name

        # Cache local
        name = self._pid_cache.get(pid)
        if name:
            return name

        # psutil
        try:
            name = psutil.Process(pid).name()
            self._pid_cache[pid] = name
            return name
        except Exception:
            return f"pid:{pid}"

    def _pid_from_service(self, service_name: str) -> int:
        """Tenta encontrar PID pelo nome do serviço (para spans OTel sem PID)."""
        if not service_name:
            return 0
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                if service_name.lower() in (proc.info["name"] or "").lower():
                    return proc.info["pid"]
        except Exception:
            pass
        return 0

    # ─── NORMALIZAÇÃO ───

    def _normalize_event(self, raw: dict, source: str) -> dict:
        return {
            "event_type":   raw.get("type", "unknown"),
            "source":       source,
            "timestamp_ns": int(raw.get("timestamp", time.time()) * 1_000_000_000),
            "pid":          raw.get("pid", 0),
            "trace_id":     raw.get("trace_id"),
            "span_id":      raw.get("span_id"),
            "operation_name": (
                raw.get("name")
                or raw.get("query")
                or raw.get("payload", {}).get("span_name")
                or raw.get("type", "")
            ),
            "network": {
                "src_ip":   raw.get("src_ip", ""),
                "src_port": raw.get("src_port", 0),
                "dst_ip":   raw.get("dst_ip", ""),
                "dst_port": raw.get("dst_port", 0),
            } if raw.get("src_ip") or raw.get("dst_ip") else None,
            "payload":    raw.get("payload"),
            "error":      raw.get("error"),
            "correlation_confidence": raw.get("correlation_confidence", "low"),
        }

    def _span_ts(self, span: dict) -> float:
        ns = span.get("start_time_unix_nano") or span.get("startTimeUnixNano", 0)
        if ns:
            return int(ns) / 1_000_000_000
        return time.time()
