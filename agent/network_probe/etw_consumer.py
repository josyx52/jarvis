"""
EtwConsumer — consome eventos ETW do kernel Windows.

Providers subscritos:
  Microsoft-Windows-Kernel-Process  → processo create/exit (PID, nome, cmdline)
  Microsoft-Windows-DNS-Client      → DNS queries (PID, hostname resolvido)
  Microsoft-Windows-TCPIP           → TCP connect/accept com PID — chave para
                                      correlacionar pacotes WinDivert a processos

O provider TCPIP é o mais crítico para o CorrelationEngine:
  Event 12 (TcpIpConnectIPv4) → {PID, src_ip, src_port, dst_ip, dst_port}
  Event 28 (TcpIpAcceptIPv4)  → {PID, src_ip, src_port, dst_ip, dst_port}

Faz fallback gracioso se pywintrace não instalado.
  pip install pywintrace
"""

import threading
import time


_PROVIDER_KERNEL_PROCESS = "{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}"
_PROVIDER_DNS_CLIENT      = "{1C95126E-7EEA-49A9-A3FE-A378B03DDB4D}"
_PROVIDER_TCPIP           = "{7DD42A49-5329-4832-8DFD-43D979153A88}"

# Event IDs do provider TCPIP relevantes para correlação PID ↔ conexão
_TCPIP_CONNECT_V4 = 12
_TCPIP_ACCEPT_V4  = 28
_TCPIP_CONNECT_V6 = 26
_TCPIP_ACCEPT_V6  = 42
_TCPIP_DISCONNECT = 14


class EtwConsumer:

    def __init__(self):
        self._events: list  = []
        self._lock          = threading.Lock()
        self._running       = False
        self._available     = self._check_available()

        # Tabela PID ↔ conexão — mantida pelo EtwConsumer, consumida pelo CorrelationEngine
        self._connection_table: dict = {}   # (src_ip, src_port, dst_ip, dst_port) → pid
        self._conn_expiry: dict      = {}   # (src_ip, src_port, dst_ip, dst_port) → expiry_ts
        self._pid_names: dict        = {}   # pid → process_name
        self._conn_lock = threading.Lock()

        # Callback do ProcessInjector — chamado em cada Process_Start
        self._process_start_cb = None

    def set_process_start_callback(self, cb):
        """Regista callback(pid, name, cmdline) chamado em cada novo processo."""
        self._process_start_cb = cb

    # ─── DISPONIBILIDADE ───

    def _check_available(self) -> bool:
        try:
            import etw  # pywintrace
            return True
        except ImportError:
            return False

    # ─── LIFECYCLE ───

    def start(self):
        if not self._available:
            print("[ETW] pywintrace não instalado — ETW desactivado (pip install pywintrace)")
            return
        self._running = True
        threading.Thread(
            target=self._loop,
            daemon=True,
            name="etw-consumer"
        ).start()
        print("[ETW] consumer iniciado — Kernel-Process + DNS + TCPIP")

    def stop(self):
        self._running = False

    # ─── DRAIN ───

    def drain(self) -> list:
        """Devolve eventos acumulados para o ProbeManager processar."""
        with self._lock:
            events       = self._events[:]
            self._events = []
        return events

    # ─── TABELA DE CORRELAÇÃO ───

    def get_pid_for_connection(self, src_ip: str, src_port: int,
                                dst_ip: str, dst_port: int) -> int:
        """
        Devolve o PID que estabeleceu esta conexão TCP.
        Consultado pelo CorrelationEngine para cada pacote capturado.
        """
        key     = (src_ip, src_port, dst_ip, dst_port)
        rev_key = (dst_ip, dst_port, src_ip, src_port)
        now = time.time()
        with self._conn_lock:
            # Limpar entradas expiradas
            expired = [k for k, exp in self._conn_expiry.items() if now > exp]
            for k in expired:
                self._connection_table.pop(k, None)
                del self._conn_expiry[k]
            return self._connection_table.get(key) or self._connection_table.get(rev_key, 0)

    def get_process_name(self, pid: int) -> str:
        with self._conn_lock:
            return self._pid_names.get(pid, "")

    def get_connection_table_snapshot(self) -> dict:
        with self._conn_lock:
            return dict(self._connection_table)

    # ─── LOOP ETW ───

    def _loop(self):
        try:
            from etw import ETW, ProviderInfo, GUID

            providers = [
                ProviderInfo("Microsoft-Windows-Kernel-Process", GUID(_PROVIDER_KERNEL_PROCESS)),
                ProviderInfo("Microsoft-Windows-DNS-Client",     GUID(_PROVIDER_DNS_CLIENT)),
                ProviderInfo("Microsoft-Windows-TCPIP",          GUID(_PROVIDER_TCPIP)),
            ]

            def on_event(event_list):
                for raw in event_list:
                    try:
                        self._handle_event(raw)
                    except Exception:
                        pass

            with ETW(providers=providers, event_callback=on_event) as session:
                while self._running:
                    time.sleep(0.5)

        except Exception as e:
            print(f"[ETW] sessão terminou: {e}")

    # ─── PROCESSAMENTO DE EVENTOS ───

    def _handle_event(self, raw: dict):
        provider = raw.get("provider_name", "")
        event_id = raw.get("event_id", 0)
        ts       = time.time()

        # ── PROCESSO CREATE / EXIT ──
        if "Kernel-Process" in provider:
            self._handle_process_event(raw, event_id, ts)
            return

        # ── DNS QUERY ──
        if "DNS" in provider:
            self._handle_dns_event(raw, ts)
            return

        # ── TCP CONNECT / ACCEPT / DISCONNECT ──
        if "TCPIP" in provider:
            self._handle_tcpip_event(raw, event_id, ts)
            return

    def _handle_process_event(self, raw: dict, event_id: int, ts: float):
        pid  = raw.get("ProcessId") or raw.get("ProcessID", 0)
        name = raw.get("ProcessName") or raw.get("ImageFileName", "")

        if pid and name:
            with self._conn_lock:
                if event_id in (1, 3):   # ProcessStart, ProcessDCStart
                    self._pid_names[pid] = name
                elif event_id in (2, 4): # ProcessStop, ProcessDCStop
                    self._pid_names.pop(pid, None)

        # Notificar ProcessInjector em cada novo processo
        if event_id in (1, 3) and pid and self._process_start_cb:
            try:
                self._process_start_cb(pid, name, raw.get("CommandLine", ""))
            except Exception:
                pass

        action = "start" if event_id in (1, 3) else "exit"
        ev = {
            "type":         "process_lifecycle",
            "action":       action,
            "pid":          pid,
            "process_name": name,
            "cmdline":      raw.get("CommandLine", ""),
            "timestamp":    ts,
            "source":       "kernel",
            "provider":     "Microsoft-Windows-Kernel-Process",
        }
        with self._lock:
            self._events.append(ev)

    def _handle_dns_event(self, raw: dict, ts: float):
        query  = raw.get("QueryName", "") or raw.get("QueryOptions", "")
        result = raw.get("QueryResults", "")
        pid    = raw.get("ProcessId", 0)

        ev = {
            "type":         "dns_query",
            "pid":          pid,
            "process_name": self._pid_names.get(pid, ""),
            "query":        query,
            "result":       result,
            "timestamp":    ts,
            "source":       "kernel",
            "provider":     "Microsoft-Windows-DNS-Client",
        }
        with self._lock:
            self._events.append(ev)

    def _handle_tcpip_event(self, raw: dict, event_id: int, ts: float):
        pid      = raw.get("PID") or raw.get("ProcessId", 0)
        src_ip   = raw.get("saddr") or raw.get("SourceAddress", "")
        dst_ip   = raw.get("daddr") or raw.get("DestinationAddress", "")
        src_port = int(raw.get("sport") or raw.get("SourcePort", 0))
        dst_port = int(raw.get("dport") or raw.get("DestinationPort", 0))

        if not (src_ip and dst_ip):
            return

        conn_key = (src_ip, src_port, dst_ip, dst_port)

        # Actualizar tabela de correlação PID ↔ conexão
        if event_id in (_TCPIP_CONNECT_V4, _TCPIP_ACCEPT_V4,
                        _TCPIP_CONNECT_V6, _TCPIP_ACCEPT_V6):
            with self._conn_lock:
                if pid:
                    self._connection_table[conn_key] = pid

        elif event_id == _TCPIP_DISCONNECT:
            with self._conn_lock:
                # Manter entrada por 5s após disconnect para correlacionar pacotes tardios
                self._conn_expiry[conn_key] = time.time() + 5.0

        action = {
            _TCPIP_CONNECT_V4: "connect",
            _TCPIP_ACCEPT_V4:  "accept",
            _TCPIP_CONNECT_V6: "connect",
            _TCPIP_ACCEPT_V6:  "accept",
            _TCPIP_DISCONNECT: "disconnect",
        }.get(event_id, f"tcpip_{event_id}")

        ev = {
            "type":         "tcp_connection",
            "action":       action,
            "pid":          pid,
            "process_name": self._pid_names.get(pid, ""),
            "src_ip":       src_ip,
            "src_port":     src_port,
            "dst_ip":       dst_ip,
            "dst_port":     dst_port,
            "timestamp":    ts,
            "source":       "kernel",
            "provider":     "Microsoft-Windows-TCPIP",
        }
        with self._lock:
            self._events.append(ev)
