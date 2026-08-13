import os
import platform
import socket
import uuid


def get_primary_ip() -> str:
    try:
        hostname = socket.gethostname()
        return socket.gethostbyname(hostname)
    except Exception:
        return "127.0.0.1"


def build_host_identity(agent_version: str) -> dict:
    return {
        "hostname": socket.gethostname(),
        "os": platform.system().lower(),
        "os_version": platform.version(),
        "ip": get_primary_ip(),
        "machine": platform.machine(),
        "agent_version": agent_version,
        "boot_id": str(uuid.uuid4()),
        "env": os.environ.get("JARVIS_ENV", "default"),
    }