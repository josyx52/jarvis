"""
ConnectionTracker — mapeia conexões TCP activas para processos em tempo real.

Usa psutil.net_connections() com polling a cada N segundos.
Detecta novas conexões estabelecidas e resolve o PID para nome do processo.
Funciona em Windows e Linux sem dependências adicionais.
"""

import threading
import time

import psutil


class ConnectionTracker:

    def __init__(self, interval: float = 2.0):
        self._interval  = interval
        self._known: dict = {}        # key -> timestamp de primeira visita
        self._events: list = []
        self._lock    = threading.Lock()
        self._running = False
        self._thread  = None

    # --------------------------------
    # LIFECYCLE
    # --------------------------------

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            daemon=True,
            name="conn-tracker"
        )
        self._thread.start()

    def stop(self):
        self._running = False

    # --------------------------------
    # DRAIN
    # --------------------------------

    def drain(self) -> list:
        with self._lock:
            events        = self._events[:]
            self._events  = []
        return events

    # --------------------------------
    # LOOP
    # --------------------------------

    def _loop(self):
        while self._running:
            try:
                self._poll()
            except Exception:
                pass
            time.sleep(self._interval)

    def _poll(self):
        try:
            conns = psutil.net_connections(kind="inet")
        except Exception:
            return

        # Cache de nomes de processo para este ciclo
        pid_names: dict[int, str] = {}

        current: set = set()

        for conn in conns:
            # Só conexões estabelecidas com destino remoto
            if conn.status != psutil.CONN_ESTABLISHED:
                continue
            if not conn.raddr:
                continue

            pid = conn.pid or 0
            laddr = f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else ""
            raddr = f"{conn.raddr.ip}:{conn.raddr.port}"
            key   = (laddr, raddr, pid)
            current.add(key)

            if key not in self._known:
                # Resolver nome do processo
                if pid and pid not in pid_names:
                    try:
                        pid_names[pid] = psutil.Process(pid).name()
                    except Exception:
                        pid_names[pid] = f"pid:{pid}"

                proc_name = pid_names.get(pid, f"pid:{pid}")

                event = {
                    "type":        "new_connection",
                    "pid":         pid,
                    "process":     proc_name,
                    "local_addr":  laddr,
                    "remote_addr": raddr,
                    "remote_ip":   conn.raddr.ip,
                    "remote_port": conn.raddr.port,
                    "timestamp":   time.time(),
                }

                with self._lock:
                    self._events.append(event)

                self._known[key] = time.time()

        # Remover conexões que já não existem
        stale = [k for k in list(self._known) if k not in current]
        for k in stale:
            del self._known[k]
