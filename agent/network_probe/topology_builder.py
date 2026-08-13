"""
TopologyBuilder — constrói o mapa de dependências entre serviços.

A partir de eventos de conexão TCP, produz um grafo:
  ProcessoA → ServiçoB (ip:porto ou nome de protocolo)

Resolve portos conhecidos para nomes de serviço legíveis.
Edges expiram após TTL segundos sem actividade.
"""

import threading
import time


# Portos → nomes de serviço
_KNOWN_PORTS: dict[int, str] = {
    5432:  "postgresql",
    3306:  "mysql",
    1433:  "mssql",
    1521:  "oracle",
    6379:  "redis",
    6380:  "redis-tls",
    27017: "mongodb",
    27018: "mongodb",
    9092:  "kafka",
    9093:  "kafka-tls",
    2181:  "zookeeper",
    5672:  "rabbitmq",
    15672: "rabbitmq-mgmt",
    61616: "activemq",
    4369:  "rabbitmq-epmd",
    9200:  "elasticsearch",
    9300:  "elasticsearch-cluster",
    2379:  "etcd",
    2380:  "etcd-peer",
    8500:  "consul",
    8600:  "consul-dns",
    6443:  "kubernetes-api",
    10250: "kubelet",
    80:    "http",
    8080:  "http-alt",
    8000:  "http-alt",
    443:   "https",
    8443:  "https-alt",
    22:    "ssh",
    3389:  "rdp",
    53:    "dns",
    25:    "smtp",
    587:   "smtp-tls",
    143:   "imap",
    993:   "imap-tls",
    5601:  "kibana",
    3000:  "grafana",
    9090:  "prometheus",
    4318:  "otlp-http",
    4317:  "otlp-grpc",
}

# IPs de loopback
_LOOPBACK = {"127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}


class TopologyBuilder:

    def __init__(self, ttl_seconds: int = 300):
        self._ttl    = ttl_seconds
        self._edges: dict = {}   # (source, dest) → edge_dict
        self._lock   = threading.Lock()

    # --------------------------------
    # UPDATE
    # --------------------------------

    def update(self, conn_events: list):
        now = time.time()
        for ev in conn_events:
            if ev.get("type") != "new_connection":
                continue

            source    = ev.get("process", "unknown")
            remote_ip = ev.get("remote_ip", "")
            remote_port = int(ev.get("remote_port", 0))
            dest      = self._resolve(remote_ip, remote_port)
            key       = (source, dest)

            with self._lock:
                if key not in self._edges:
                    self._edges[key] = {
                        "source":           source,
                        "destination":      dest,
                        "remote_ip":        remote_ip,
                        "remote_port":      remote_port,
                        "protocol":         _KNOWN_PORTS.get(remote_port, "tcp"),
                        "connection_count": 0,
                        "first_seen":       now,
                        "last_seen":        now,
                    }
                self._edges[key]["connection_count"] += 1
                self._edges[key]["last_seen"]         = now

    # --------------------------------
    # RESOLVER DESTINO
    # --------------------------------

    def _resolve(self, ip: str, port: int) -> str:
        service = _KNOWN_PORTS.get(port)

        if ip in _LOOPBACK:
            return service if service else f"localhost:{port}"

        if service:
            return f"{ip}/{service}"

        return f"{ip}:{port}"

    # --------------------------------
    # SNAPSHOT
    # --------------------------------

    def snapshot(self) -> list:
        now = time.time()
        with self._lock:
            stale = [k for k, v in self._edges.items() if now - v["last_seen"] > self._ttl]
            for k in stale:
                del self._edges[k]
            return list(self._edges.values())

    def get_topology_frame_data(self) -> dict:
        edges = self.snapshot()
        services = set(e["source"] for e in edges)
        return {
            "edges":            edges,
            "service_count":    len(services),
            "dependency_count": len(edges),
            "services":         sorted(services),
        }
