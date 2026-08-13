"""
ServerScanner — examina o servidor e devolve um payload de discovery.

Identifica:
  - Portas abertas e servicos conhecidos
  - Processos relevantes a correr
  - Servicos Windows instalados
  - Bases de dados (PostgreSQL, SQL Server, Oracle, MySQL)
  - Brokers de mensagens (Kafka, RabbitMQ, IBM MQ, ActiveMQ)
  - Web servers (Nginx, IIS, Apache)
  - Frameworks de API (Python/Flask/FastAPI, Java, .NET)

Resultado: dict com tudo o que foi encontrado — passado ao SetupEngine.
"""

import os
import socket


# ---------------------------------------------------
# PORTAS CONHECIDAS
# ---------------------------------------------------

KNOWN_PORTS = {
    5432:  {"type": "db",    "name": "PostgreSQL"},
    1433:  {"type": "db",    "name": "SQL Server"},
    1521:  {"type": "db",    "name": "Oracle"},
    3306:  {"type": "db",    "name": "MySQL"},
    27017: {"type": "db",    "name": "MongoDB"},
    6379:  {"type": "cache", "name": "Redis"},
    9092:  {"type": "mq",    "name": "Kafka"},
    5672:  {"type": "mq",    "name": "RabbitMQ"},
    1414:  {"type": "mq",    "name": "IBM MQ"},
    61616: {"type": "mq",    "name": "ActiveMQ"},
    80:    {"type": "web",   "name": "HTTP"},
    443:   {"type": "web",   "name": "HTTPS"},
    8080:  {"type": "web",   "name": "HTTP Alt"},
    8443:  {"type": "web",   "name": "HTTPS Alt"},
    22:    {"type": "sys",   "name": "SSH"},
    3389:  {"type": "sys",   "name": "RDP"},
    636:   {"type": "auth",  "name": "LDAPS"},
    389:   {"type": "auth",  "name": "LDAP"},
}

# Processos que indicam o tipo de servidor
PROCESS_SIGNATURES = {
    "postgres":         {"type": "db",    "name": "PostgreSQL"},
    "sqlservr":         {"type": "db",    "name": "SQL Server"},
    "oracle":           {"type": "db",    "name": "Oracle"},
    "mysqld":           {"type": "db",    "name": "MySQL"},
    "mongod":           {"type": "db",    "name": "MongoDB"},
    "kafka":            {"type": "mq",    "name": "Kafka"},
    "rabbitmq":         {"type": "mq",    "name": "RabbitMQ"},
    "amqpbroker":       {"type": "mq",    "name": "RabbitMQ"},
    "runbroker":        {"type": "mq",    "name": "ActiveMQ"},
    "amqsput":          {"type": "mq",    "name": "IBM MQ"},
    "nginx":            {"type": "web",   "name": "Nginx"},
    "httpd":            {"type": "web",   "name": "Apache"},
    "w3wp":             {"type": "web",   "name": "IIS"},
    "python":           {"type": "api",   "name": "Python"},
    "uvicorn":          {"type": "api",   "name": "FastAPI/Uvicorn"},
    "gunicorn":         {"type": "api",   "name": "Gunicorn"},
    "java":             {"type": "api",   "name": "Java"},
    "node":             {"type": "api",   "name": "Node.js"},
    "dotnet":           {"type": "api",   "name": ".NET"},
}


class ServerScanner:

    def scan(self) -> dict:
        """Executa o scan completo e devolve o payload de discovery."""

        hostname    = self._get_hostname()
        os_info     = self._get_os_info()
        open_ports  = self._scan_ports()
        processes   = self._scan_processes()
        services    = self._scan_services()
        disk_info   = self._get_disk_info()
        memory_info = self._get_memory_info()

        detected = self._classify(open_ports, processes, services)

        return {
            "hostname":  hostname,
            "os":        os_info,
            "open_ports": open_ports,
            "processes":  processes,
            "services":   services,
            "disk":       disk_info,
            "memory":     memory_info,
            "detected":   detected,
        }

    # ----------------------------------------
    # HOSTNAME / OS
    # ----------------------------------------

    def _get_hostname(self) -> str:
        try:
            return socket.gethostname()
        except Exception:
            return "unknown"

    def _get_os_info(self) -> dict:
        try:
            import platform
            return {
                "system":  platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
            }
        except Exception:
            return {}

    # ----------------------------------------
    # PORTAS
    # ----------------------------------------

    def _scan_ports(self) -> list[dict]:
        found = []
        for port, info in KNOWN_PORTS.items():
            if self._port_open(port):
                found.append({"port": port, **info})
        return found

    def _port_open(self, port: int, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                return True
        except Exception:
            return False

    # ----------------------------------------
    # PROCESSOS
    # ----------------------------------------

    def _scan_processes(self) -> list[dict]:
        found = []
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "name", "cmdline", "status"]):
                try:
                    name = (proc.info["name"] or "").lower()
                    for sig, info in PROCESS_SIGNATURES.items():
                        if sig in name:
                            cmdline = " ".join(proc.info.get("cmdline") or [])[:200]
                            found.append({
                                "pid":     proc.info["pid"],
                                "name":    proc.info["name"],
                                "cmdline": cmdline,
                                **info
                            })
                            break
                except Exception:
                    pass
        except Exception:
            pass
        return found

    # ----------------------------------------
    # SERVICOS WINDOWS
    # ----------------------------------------

    def _scan_services(self) -> list[dict]:
        found = []
        try:
            import psutil
            for svc in psutil.win_service_iter():
                try:
                    name = svc.name().lower()
                    display = svc.display_name().lower()
                    combined = name + " " + display
                    for sig, info in PROCESS_SIGNATURES.items():
                        if sig in combined:
                            found.append({
                                "name":    svc.name(),
                                "display": svc.display_name(),
                                "status":  svc.status(),
                                **info
                            })
                            break
                except Exception:
                    pass
        except Exception:
            pass
        return found

    # ----------------------------------------
    # DISCO / MEMORIA
    # ----------------------------------------

    def _get_disk_info(self) -> list[dict]:
        disks = []
        try:
            import psutil
            for part in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append({
                        "device":      part.device,
                        "mountpoint":  part.mountpoint,
                        "fstype":      part.fstype,
                        "total_gb":    round(usage.total / 1e9, 1),
                        "free_gb":     round(usage.free / 1e9, 1),
                        "percent":     usage.percent,
                    })
                except Exception:
                    pass
        except Exception:
            pass
        return disks

    def _get_memory_info(self) -> dict:
        try:
            import psutil
            mem = psutil.virtual_memory()
            return {
                "total_gb": round(mem.total / 1e9, 1),
                "available_gb": round(mem.available / 1e9, 1),
                "percent": mem.percent,
            }
        except Exception:
            return {}

    # ----------------------------------------
    # CLASSIFICACAO FINAL
    # ----------------------------------------

    def _classify(self, ports: list, processes: list, services: list) -> dict:
        """Agrega tudo numa visao clara do que foi detectado."""
        all_signals = ports + processes + services

        dbs  = [s for s in all_signals if s.get("type") == "db"]
        mqs  = [s for s in all_signals if s.get("type") == "mq"]
        webs = [s for s in all_signals if s.get("type") == "web"]
        apis = [s for s in all_signals if s.get("type") == "api"]

        # Deduplica por nome
        def unique_names(items):
            seen = set()
            result = []
            for item in items:
                n = item.get("name")
                if n not in seen:
                    seen.add(n)
                    result.append(item)
            return result

        return {
            "databases":       unique_names(dbs),
            "message_queues":  unique_names(mqs),
            "web_servers":     unique_names(webs),
            "api_runtimes":    unique_names(apis),
            "has_db":          len(dbs) > 0,
            "has_mq":          len(mqs) > 0,
            "has_web":         len(webs) > 0,
            "has_api":         len(apis) > 0,
        }
