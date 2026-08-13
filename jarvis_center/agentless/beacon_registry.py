"""
BeaconRegistry — registo de "mini-agentes" temporários em máquinas remotas.

Modelo OUTBOUND POLLING (igual ao Agent permanente, não um listener inbound):
  1. WinRM bootstrap lança um processo PowerShell na máquina remota
  2. Esse processo faz polling de saída: GET /agentless/beacon/{id}/next
  3. O Center responde com o próximo script pendente (ou "wait")
  4. O beacon executa e devolve o resultado: POST /agentless/beacon/{id}/result
  5. Nunca há conexão de entrada na máquina remota — zero problemas de firewall,
     exactamente como o agent_daemon.py faz com /agent/commands/pending.

O registry mantém, por beacon_id:
  - identidade (host, machine_type, api_key)
  - fila de jobs pendentes (normalmente 0 ou 1 de cada vez)
  - resultados prontos para recolha pelo broker
  - timestamps para TTL e detecção de beacons mortos
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque


class Beacon:
    __slots__ = ("beacon_id", "host", "api_key", "machine_type",
                 "started_at", "last_poll", "ttl", "exec_count",
                 "_job_queue", "_results", "_lock")

    def __init__(self, beacon_id: str, host: str, api_key: str,
                 machine_type: str = "server", ttl: int = 600):
        self.beacon_id = beacon_id
        self.host = host.upper()
        self.api_key = api_key
        self.machine_type = machine_type
        self.started_at = time.time()
        self.last_poll = time.time()
        self.ttl = ttl
        self.exec_count = 0
        self._job_queue: deque = deque()
        self._results: dict[str, dict] = {}
        self._lock = threading.Lock()

    @property
    def is_alive(self) -> bool:
        # Considerado vivo se fez poll recentemente (2x o intervalo esperado)
        return (time.time() - self.last_poll) < max(self.ttl, 30)

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at

    def touch(self):
        self.last_poll = time.time()

    def push_job(self, job_id: str, script: str):
        with self._lock:
            self._job_queue.append({"job_id": job_id, "script": script})

    def pop_job(self) -> dict | None:
        with self._lock:
            if self._job_queue:
                return self._job_queue.popleft()
            return None

    def has_pending_job(self) -> bool:
        with self._lock:
            return len(self._job_queue) > 0

    def submit_result(self, job_id: str, result: dict):
        with self._lock:
            self._results[job_id] = result
            self.exec_count += 1

    def pop_result(self, job_id: str) -> dict | None:
        with self._lock:
            return self._results.pop(job_id, None)

    def to_dict(self) -> dict:
        return {
            "beacon_id": self.beacon_id,
            "host": self.host,
            "machine_type": self.machine_type,
            "started_at": self.started_at,
            "last_poll": self.last_poll,
            "ttl": self.ttl,
            "exec_count": self.exec_count,
            "uptime_s": round(self.uptime, 1),
            "alive": self.is_alive,
            "pending_jobs": len(self._job_queue),
        }


class BeaconRegistry:
    """Thread-safe registry de beacons (mini-agentes outbound-polling)."""

    def __init__(self, cleanup_interval: int = 60):
        self._beacons: dict[str, Beacon] = {}       # beacon_id -> Beacon
        self._by_host: dict[str, str] = {}           # host -> beacon_id (mais recente vivo)
        self._lock = threading.Lock()
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()

    def register(self, host: str, api_key: str, machine_type: str = "server",
                 ttl: int = 600) -> Beacon:
        beacon_id = uuid.uuid4().hex[:16]
        beacon = Beacon(beacon_id, host, api_key, machine_type, ttl)
        with self._lock:
            self._beacons[beacon_id] = beacon
            self._by_host[host.upper()] = beacon_id
        return beacon

    def get_by_id(self, beacon_id: str) -> Beacon | None:
        with self._lock:
            return self._beacons.get(beacon_id)

    def get_by_host(self, host: str) -> Beacon | None:
        self._maybe_cleanup()
        with self._lock:
            beacon_id = self._by_host.get(host.upper())
            if beacon_id is None:
                return None
            beacon = self._beacons.get(beacon_id)
            if beacon is None or not beacon.is_alive:
                self._by_host.pop(host.upper(), None)
                if beacon_id in self._beacons:
                    del self._beacons[beacon_id]
                return None
            return beacon

    def remove(self, beacon_id: str) -> None:
        with self._lock:
            beacon = self._beacons.pop(beacon_id, None)
            if beacon and self._by_host.get(beacon.host) == beacon_id:
                self._by_host.pop(beacon.host, None)

    def active_count(self) -> int:
        self._maybe_cleanup()
        with self._lock:
            return sum(1 for b in self._beacons.values() if b.is_alive)

    def status(self) -> dict:
        self._maybe_cleanup()
        with self._lock:
            beacons = [b.to_dict() for b in self._beacons.values()]
            return {
                "active_beacons": sum(1 for b in beacons if b["alive"]),
                "total_execs": sum(b["exec_count"] for b in beacons),
                "beacons": beacons,
            }

    def _maybe_cleanup(self):
        now = time.time()
        if (now - self._last_cleanup) < self._cleanup_interval:
            return
        with self._lock:
            expired = [bid for bid, b in self._beacons.items() if not b.is_alive]
            for bid in expired:
                beacon = self._beacons.pop(bid, None)
                if beacon and self._by_host.get(beacon.host) == bid:
                    self._by_host.pop(beacon.host, None)
            self._last_cleanup = now
