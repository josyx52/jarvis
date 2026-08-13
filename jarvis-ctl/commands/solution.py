import os
import sys

import psycopg2
import requests as _requests
from rich.console import Console
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from context import get_config_value, set_config_value

console = Console()

_TIMEOUT = 5


def _conn():
    return psycopg2.connect(
        host     = os.getenv("JARVIS_DB_HOST", "localhost"),
        database = os.getenv("JARVIS_DB_NAME", "jarvis"),
        user     = os.getenv("JARVIS_DB_USER", "postgres"),
        password = os.getenv("JARVIS_DB_PASS", "Vermelho555@"),
    )


def _dot(ok: bool) -> str:
    return "[green]●[/green]" if ok else "[red]●[/red]"


def _test_endpoint(url: str) -> tuple[bool, str]:
    try:
        r = _requests.post(
            url,
            json={"type": "jarvis_health_check", "version": "1.0"},
            timeout=_TIMEOUT,
        )
        return True, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)


# ── STATUS ────────────────────────────────────────────────────────────────────

def run_status():
    enabled = get_config_value("solution_driver.enabled", "false").lower() == "true"

    console.print()
    console.print("  [bold]Solution Driver[/bold]")
    console.print(f"  [dim]{'─' * 52}[/dim]", highlight=False)

    dot = "[green]●[/green]" if enabled else "[red]●[/red]"
    state = "ACTIVO" if enabled else "INACTIVO"
    console.print(f"  {dot}  Estado          [cyan]{state}[/cyan]")
    console.print()

    try:
        conn = _conn()
        conn.autocommit = True
        cur  = conn.cursor()
        cur.execute(
            "SELECT name, url, min_severity, host_filter, active FROM notification_webhooks ORDER BY created_at DESC"
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        console.print(f"  [red]Erro ao ler webhooks: {e}[/red]\n")
        return

    if not rows:
        console.print("  [yellow]Nenhum webhook configurado.[/yellow]")
        console.print("  [dim]Use: jarvis-ctl solution webhook add <url>[/dim]\n")
        return

    tbl = Table(show_header=True, box=None, padding=(0, 2), show_edge=False)
    tbl.add_column("Nome",        style="cyan",  no_wrap=True)
    tbl.add_column("URL",         style="white", no_wrap=True)
    tbl.add_column("Min severity",style="dim",   no_wrap=True)
    tbl.add_column("Estado",      no_wrap=True)
    tbl.add_column("Endpoint",    style="dim")

    for name, url, sev, host_filter, active in rows:
        ok, msg = _test_endpoint(url)
        tbl.add_row(
            name or "-",
            url,
            sev or "high",
            ("[green]activo[/green]" if active else "[red]inactivo[/red]"),
            ("[green]" + msg + "[/green]") if ok else ("[red]" + msg[:60] + "[/red]"),
        )

    console.print(tbl)
    console.print()


# ── ENABLE / DISABLE ──────────────────────────────────────────────────────────

def run_enable():
    set_config_value("solution_driver.enabled", "true")

    try:
        conn = _conn()
        conn.autocommit = True
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM notification_webhooks WHERE active = TRUE")
        count = cur.fetchone()[0]
        conn.close()
    except Exception:
        count = 0

    console.print()
    console.print("  [green]● Solution Driver ACTIVADO.[/green]")
    if count == 0:
        console.print("  [yellow]Atenção: nenhum webhook activo configurado.[/yellow]")
        console.print("  [dim]Use: jarvis-ctl solution webhook add <url>[/dim]")
    else:
        console.print(f"  [dim]{count} webhook(s) activo(s) configurado(s).[/dim]")
    console.print()


def run_disable():
    set_config_value("solution_driver.enabled", "false")
    console.print()
    console.print("  [yellow]● Solution Driver DESACTIVADO.[/yellow]")
    console.print("  [dim]Nenhuma notificação será enviada.[/dim]\n")


# ── WEBHOOK LIST ──────────────────────────────────────────────────────────────

def run_webhook_list():
    try:
        conn = _conn()
        conn.autocommit = True
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, name, url, min_severity, host_filter, active, created_at "
            "FROM notification_webhooks ORDER BY created_at DESC"
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        console.print(f"\n  [red]Erro: {e}[/red]\n")
        return

    console.print()
    if not rows:
        console.print("  [yellow]Nenhum webhook configurado.[/yellow]")
        console.print("  [dim]Use: jarvis-ctl solution webhook add <url>[/dim]\n")
        return

    tbl = Table(show_header=True, box=None, padding=(0, 2), show_edge=False)
    tbl.add_column("ID",    style="dim",   no_wrap=True)
    tbl.add_column("Nome",  style="cyan",  no_wrap=True)
    tbl.add_column("URL",   style="white")
    tbl.add_column("Sev",   style="dim",   no_wrap=True)
    tbl.add_column("Estado",no_wrap=True)

    for wid, name, url, sev, hf, active, created_at in rows:
        tbl.add_row(
            str(wid),
            name or "-",
            url,
            sev or "high",
            "[green]activo[/green]" if active else "[red]inactivo[/red]",
        )

    console.print(tbl)
    console.print()


# ── WEBHOOK ADD ───────────────────────────────────────────────────────────────

def run_webhook_add(url: str, name: str, min_severity: str, host_filter: str):
    console.print(f"\n  A testar endpoint [cyan]{url}[/cyan]...")
    ok, msg = _test_endpoint(url)

    if ok:
        console.print(f"  [green]● Endpoint respondeu: {msg}[/green]")
    else:
        console.print(f"  [yellow]⚠ Endpoint não respondeu: {msg}[/yellow]")
        console.print("  [dim]O webhook será registado mas o Solution Driver pausará se não responder.[/dim]")

    try:
        conn = _conn()
        conn.autocommit = True
        cur  = conn.cursor()
        host_list = [h.strip() for h in host_filter.split(",") if h.strip()] or None
        cur.execute(
            """
            INSERT INTO notification_webhooks (name, url, min_severity, host_filter, active)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (name or url, url, min_severity, host_list),
        )
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        console.print(f"\n  [red]Erro ao registar: {e}[/red]\n")
        return

    if row:
        console.print(f"  [green]● Webhook registado (id={row[0]}).[/green]")
        console.print(f"  [dim]Severidade mínima: {min_severity}[/dim]")
    else:
        console.print("  [yellow]URL já existe — nada alterado.[/yellow]")
    console.print()


# ── WEBHOOK ENABLE / DISABLE / REMOVE ────────────────────────────────────────

def run_webhook_toggle(url: str, active: bool):
    try:
        conn = _conn()
        conn.autocommit = True
        cur  = conn.cursor()
        cur.execute(
            "UPDATE notification_webhooks SET active=%s WHERE url=%s RETURNING id",
            (active, url),
        )
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        console.print(f"\n  [red]Erro: {e}[/red]\n")
        return

    console.print()
    if row:
        state = "[green]activado[/green]" if active else "[yellow]desactivado[/yellow]"
        console.print(f"  ● Webhook {state}: [cyan]{url}[/cyan]")
    else:
        console.print(f"  [red]Webhook não encontrado: {url}[/red]")
    console.print()


def run_webhook_remove(url: str):
    try:
        conn = _conn()
        conn.autocommit = True
        cur  = conn.cursor()
        cur.execute(
            "DELETE FROM notification_webhooks WHERE url=%s RETURNING id", (url,)
        )
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        console.print(f"\n  [red]Erro: {e}[/red]\n")
        return

    console.print()
    if row:
        console.print(f"  [green]● Webhook removido: [cyan]{url}[/cyan][/green]")
    else:
        console.print(f"  [red]Webhook não encontrado: {url}[/red]")
    console.print()


# ── WEBHOOK TEST ──────────────────────────────────────────────────────────────

def run_webhook_test(url: str):
    console.print(f"\n  A testar [cyan]{url}[/cyan]...")
    ok, msg = _test_endpoint(url)
    if ok:
        console.print(f"  [green]● OK — {msg}[/green]")
    else:
        console.print(f"  [red]● Falhou — {msg}[/red]")
    console.print()
