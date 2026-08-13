import os
import subprocess
import sys

import requests as _requests
from rich.console import Console
from rich.table import Table
from rich.text import Text

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from ui import theme

console = Console()

_SVC_NAME = "jarvis_center"


def _run(args: list[str]) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=15,
            creationflags=0x08000000,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def _svc_state() -> str:
    rc, out, _ = _run(["sc", "query", _SVC_NAME])
    if rc != 0:
        return "NOT_INSTALLED"
    for line in out.splitlines():
        if "STATE" in line:
            for word in line.split():
                if word in ("RUNNING", "STOPPED", "PAUSED", "START_PENDING", "STOP_PENDING"):
                    return word
    return "UNKNOWN"


def _api_health() -> tuple[bool, str]:
    port = os.getenv("JARVIS_API_PORT", "8080")
    try:
        r = _requests.get(f"http://127.0.0.1:{port}/", timeout=3)
        return True, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)


def _redis_health() -> tuple[bool, str]:
    import socket
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return True, f"{host}:{port}"
    except Exception as e:
        return False, str(e)


def _db_health() -> tuple[bool, str]:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from context import get_db
    try:
        cur = get_db().cursor()
        cur.execute("SELECT COUNT(*) FROM snapshots")
        count = cur.fetchone()[0]
        return True, f"{count} snapshots"
    except Exception as e:
        return False, str(e)


def _dot(ok: bool) -> str:
    return "[green]●[/green]" if ok else "[red]●[/red]"


def run_status():
    state     = _svc_state()
    api_ok,   api_msg   = _api_health()
    redis_ok, redis_msg = _redis_health()
    db_ok,    db_msg    = _db_health()

    running = state == "RUNNING"

    console.print()
    console.print(f"  [bold]Jarvis Center[/bold]  —  serviço [cyan]{_SVC_NAME}[/cyan]")
    console.print(f"  [dim]{'─' * 52}[/dim]", highlight=False)

    svc_dot = "[green]●[/green]" if running else "[red]●[/red]"
    console.print(f"  {svc_dot}  Serviço Windows   [cyan]{state}[/cyan]")
    console.print(f"  {_dot(api_ok)}  API  (port 8080)  [dim]{api_msg}[/dim]")
    console.print(f"  {_dot(redis_ok)}  Redis             [dim]{redis_msg}[/dim]")
    console.print(f"  {_dot(db_ok)}  PostgreSQL        [dim]{db_msg}[/dim]")
    console.print()

    if running and api_ok and redis_ok and db_ok:
        console.print("  [green]ONLINE — todos os componentes operacionais[/green]")
    else:
        console.print("  [yellow]DEGRADADO — verificar componentes em erro[/yellow]")
    console.print()


def run_start():
    state = _svc_state()
    if state == "NOT_INSTALLED":
        console.print(f"\n  [red]Serviço {_SVC_NAME} não está instalado.[/red]")
        console.print("  [dim]Corre install_packages.bat e instala via NSSM primeiro.[/dim]\n")
        return

    if state == "RUNNING":
        console.print(f"\n  [yellow]Serviço {_SVC_NAME} já está a correr.[/yellow]\n")
        return

    console.print(f"\n  A iniciar [cyan]{_SVC_NAME}[/cyan]...")
    rc, out, err = _run(["net", "start", _SVC_NAME])
    if rc == 0:
        console.print(f"  [green]● Serviço iniciado.[/green]\n")
    else:
        console.print(f"  [red]Falha ao iniciar: {err or out}[/red]")
        console.print(f"  [dim]Logs: {_LOG_STDERR}[/dim]\n")


def run_stop():
    state = _svc_state()
    if state == "NOT_INSTALLED":
        console.print(f"\n  [red]Serviço {_SVC_NAME} não está instalado.[/red]\n")
        return

    if state == "STOPPED":
        console.print(f"\n  [yellow]Serviço {_SVC_NAME} já está parado.[/yellow]\n")
        return

    console.print(f"\n  A parar [cyan]{_SVC_NAME}[/cyan]...")
    rc, out, err = _run(["net", "stop", _SVC_NAME])
    if rc == 0:
        console.print(f"  [green]● Serviço parado.[/green]\n")
    else:
        console.print(f"  [red]Falha ao parar: {err or out}[/red]\n")


def run_restart():
    console.print(f"\n  A reiniciar [cyan]{_SVC_NAME}[/cyan]...")
    _run(["net", "stop", _SVC_NAME])
    import time; time.sleep(3)
    rc, out, err = _run(["net", "start", _SVC_NAME])
    if rc == 0:
        console.print(f"  [green]● Serviço reiniciado.[/green]\n")
    else:
        console.print(f"  [red]Falha ao reiniciar: {err or out}[/red]\n")


def run_logs(lines: int = 50):
    import config as _cfg
    log_path = _cfg.center_log_path()
    # Tentar o path configurado e depois o stderr na mesma pasta
    log_dir   = os.path.dirname(log_path)
    candidates = [log_path, os.path.join(log_dir, "stderr.log")]

    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-lines:]
                console.print(f"\n  [dim]{path}[/dim]")
                console.print(f"  [dim]{'─' * 60}[/dim]", highlight=False)
                for line in tail:
                    console.print(f"  {line.rstrip()}", highlight=False)
                console.print()
                return
            except Exception as e:
                console.print(f"  [red]Erro ao ler log: {e}[/red]\n")
                return

    console.print(f"\n  [yellow]Logs não encontrados em {log_dir}[/yellow]")
    console.print("  [dim]O center pode estar a correr em modo standalone (terminal).[/dim]\n")
