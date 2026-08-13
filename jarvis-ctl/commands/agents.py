import os
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.panel import Panel

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from context import get_db
from ui import theme

console = Console()


def _age(ts) -> str:
    if ts is None:
        return "—"
    now = datetime.now(tz=timezone.utc)
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    diff = int((now - ts).total_seconds())
    if diff < 60:   return f"[green]{diff}s[/green]"
    if diff < 300:  return f"[green]{diff//60}m {diff%60}s[/green]"
    if diff < 3600: return f"[yellow]{diff//60}m[/yellow]"
    return f"[red]{diff//3600}h{(diff%3600)//60}m[/red]"


def _online(ts) -> str:
    if ts is None:
        return "[red]offline[/red]"
    now  = datetime.now(tz=timezone.utc)
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    diff = int((now - ts).total_seconds())
    return "[green]online[/green]" if diff < 300 else "[red]offline[/red]"


def run_list():
    cur = get_db().cursor()
    cur.execute("""
        SELECT
            s.host,
            TO_TIMESTAMP(MAX(s.snapshot_time))                      AS last_seen,
            AVG(s.cpu_percent)    FILTER (WHERE s.snapshot_time >= EXTRACT(EPOCH FROM NOW() - INTERVAL '10 minutes')) AS cpu_avg,
            AVG(s.memory_percent) FILTER (WHERE s.snapshot_time >= EXTRACT(EPOCH FROM NOW() - INTERVAL '10 minutes')) AS mem_avg,
            COUNT(DISTINCT p.problem_id) FILTER (WHERE p.status = 'open') AS open_problems
        FROM snapshots s
        LEFT JOIN problems p ON p.host = s.host
        GROUP BY s.host
        ORDER BY last_seen DESC
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    tbl = Table(
        show_header=True, header_style="dim cyan",
        border_style="dim cyan", padding=(0, 1),
    )
    tbl.add_column("Host",          style="bright_white", no_wrap=True, width=30)
    tbl.add_column("Status",        no_wrap=True,          width=10)
    tbl.add_column("Last seen",     no_wrap=True,          width=12)
    tbl.add_column("CPU avg",       no_wrap=True,          width=10)
    tbl.add_column("Mem avg",       no_wrap=True,          width=10)
    tbl.add_column("Problems",      no_wrap=True,          width=10)

    for r in rows:
        host     = (r["host"] or "").split("::")[0]
        cpu_avg  = r["cpu_avg"]
        mem_avg  = r["mem_avg"]
        probs    = r["open_problems"] or 0
        cpu_s    = f"{float(cpu_avg):.1f}%" if cpu_avg is not None else "—"
        mem_s    = f"{float(mem_avg):.1f}%" if mem_avg is not None else "—"
        prob_s   = f"[red]{probs}[/red]" if probs else "[dim]0[/dim]"
        tbl.add_row(
            host,
            _online(r["last_seen"]),
            _age(r["last_seen"]),
            cpu_s,
            mem_s,
            prob_s,
        )

    console.print()
    console.print(tbl)
    console.print(f"  [dim]{len(rows)} hosts[/dim]\n")


def run_show(host: str):
    cur = get_db().cursor()

    # latest snapshot metrics
    cur.execute("""
        SELECT TO_TIMESTAMP(snapshot_time) AS snapshot_time, cpu_percent, memory_percent, disk_percent, process_count, service_count
        FROM snapshots WHERE host ILIKE %s ORDER BY snapshot_time DESC LIMIT 1
    """, [f"%{host}%"])
    row = cur.fetchone()
    if not row:
        console.print(f"\n  [red]Host not found: {host}[/red]\n")
        return

    snap_time, cpu, mem, disk, procs, svcs = row

    # open problems
    cur.execute("SELECT problem_id, title, severity FROM problems WHERE host ILIKE %s AND status='open'", [f"%{host}%"])
    problems = cur.fetchall()

    # recent events
    cur.execute("""
        SELECT event_type, severity, summary, event_time FROM events
        WHERE host ILIKE %s AND event_time >= NOW() - INTERVAL '1 hour'
        ORDER BY event_time DESC LIMIT 8
    """, [f"%{host}%"])
    events = cur.fetchall()

    console.print()
    # ── Header ────────────────────────────────────────────────────────────
    t = Text()
    t.append(f"▲ {host.split('::')[0]}", style=theme.DELTA)
    if snap_time:
        t.append(f"  last seen {snap_time.strftime('%H:%M:%S')}", style="dim")
    console.print(Panel(t, border_style="dim cyan", padding=(0, 1)))

    # ── Metrics ───────────────────────────────────────────────────────────
    def bar(pct: float, width=20) -> str:
        filled = int((pct / 100) * width)
        color  = "red" if pct > 85 else ("yellow" if pct > 70 else "green")
        return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * (width - filled)}[/dim] {pct:.1f}%"

    console.print(f"  CPU     {bar(float(cpu or 0))}", highlight=False)
    console.print(f"  Memory  {bar(float(mem or 0))}", highlight=False)
    console.print(f"  Disk    {bar(float(disk or 0))}", highlight=False)
    console.print(f"  [dim]Processes: {procs}   Services: {svcs}[/dim]\n")

    # ── Open problems ─────────────────────────────────────────────────────
    if problems:
        console.print("  [bold red]▲ Open Problems[/bold red]")
        for pid, title, sev in problems:
            console.print(f"  [{theme.SEV_COLORS.get(sev,'white')}]{sev:8}[/{theme.SEV_COLORS.get(sev,'white')}]  {title}", highlight=False)
        console.print()

    # ── Recent events ─────────────────────────────────────────────────────
    console.print("  [dim cyan]Recent Events (last 1h)[/dim cyan]")
    if events:
        for etype, sev, summary, ets in events:
            ts_s = ets.strftime("%H:%M:%S") if ets and hasattr(ets, "strftime") else str(ets or "")
            console.print(f"  [dim]{ts_s}[/dim]  [{theme.SEV_COLORS.get(sev,'white')}]{sev:8}[/{theme.SEV_COLORS.get(sev,'white')}]  {(summary or '')[:60]}", highlight=False)
    else:
        console.print("  [dim]—  no events in last hour[/dim]")
    console.print()
