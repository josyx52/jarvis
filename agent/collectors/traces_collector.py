import time
import psutil


class ConnectionsCollector:
    """Colecta snapshot das conexões TCP/IP activas no host."""

    def collect(self, limit: int = 200) -> list[dict]:
        results: list[dict] = []

        # cache de nomes de processos para evitar chamadas repetidas ao SO
        pid_names: dict[int, str] = {}

        try:
            conns = psutil.net_connections(kind="inet")
        except Exception:
            return []

        for conn in conns[:limit]:
            if not conn.raddr:
                continue

            pid = conn.pid or 0
            if pid and pid not in pid_names:
                try:
                    pid_names[pid] = psutil.Process(pid).name()
                except Exception:
                    pid_names[pid] = ""

            results.append({
                "pid":          pid,
                "process":      pid_names.get(pid, ""),
                "local_addr":   f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else "",
                "remote_addr":  f"{conn.raddr.ip}:{conn.raddr.port}",
                "remote_ip":    conn.raddr.ip,
                "remote_port":  conn.raddr.port,
                "status":       conn.status,
                "family":       "ipv6" if "::" in (conn.laddr.ip if conn.laddr else "") else "ipv4",
                "timestamp":    time.time(),
            })

        return results