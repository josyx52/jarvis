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
    if diff < 60:   return f"{diff}s"
    if diff < 3600: return f"{diff//60}m"
    return f"{diff//3600}h{(diff%3600)//60}m"


def run_list(status_filter: str = "open"):
    cur = get_db().cursor()

    if status_filter == "all":
        cur.execute("""
            SELECT problem_id, host, title, severity, status, alert_count,
                   opened_at, last_seen, duration_s
            FROM problems
            ORDER BY opened_at DESC LIMIT 50
        """)
    else:
        cur.execute("""
            SELECT problem_id, host, title, severity, status, alert_count,
                   opened_at, last_seen, duration_s
            FROM problems WHERE status = %s
            ORDER BY opened_at DESC LIMIT 50
        """, [status_filter])

    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    tbl = Table(
        show_header=True, header_style="dim cyan",
        border_style="dim cyan", padding=(0, 1),
    )
    tbl.add_column("ID",       style="dim",           no_wrap=True, width=28)
    tbl.add_column("Host",     style="bright_white",  no_wrap=True, width=22)
    tbl.add_column("Severity", no_wrap=True,           width=10)
    tbl.add_column("Status",   no_wrap=True,           width=10)
    tbl.add_column("Alerts",   style="dim",            no_wrap=True, width=7)
    tbl.add_column("Title",    style="white",           width=30)
    tbl.add_column("Age",      style="dim",            no_wrap=True, width=8)

    for r in rows:
        sev    = r.get("severity", "unknown")
        status = r.get("status", "unknown")
        host   = (r.get("host") or "").split("::")[0]
        sev_c  = theme.SEV_COLORS.get(sev, "white")
        st_c   = theme.STATUS_COLORS.get(status, "white")
        tbl.add_row(
            r.get("problem_id", ""),
            host,
            f"[{sev_c}]{sev}[/{sev_c}]",
            f"[{st_c}]{status}[/{st_c}]",
            str(r.get("alert_count", 0)),
            (r.get("title") or "")[:30],
            _age(r.get("opened_at")),
        )

    console.print()
    console.print(tbl)
    console.print(f"  [dim]{len(rows)} problem{'s' if len(rows) != 1 else ''}[/dim]\n")


def run_show(problem_id: str):
    cur = get_db().cursor()
    cur.execute("SELECT * FROM problems WHERE problem_id = %s", [problem_id])
    row = cur.fetchone()
    if not row:
        console.print(f"\n  [red]Problem not found: {problem_id}[/red]\n")
        return

    cols = [d[0] for d in cur.description]
    p    = dict(zip(cols, row))
    sev  = p.get("severity", "unknown")
    host = (p.get("host") or "").split("::")[0]

    console.print()
    title_t = Text()
    title_t.append(f"▲ {p.get('title', 'Problem')}", style=theme.DELTA)
    console.print(Panel(title_t, border_style="dim cyan"))

    def row_line(label, value):
        console.print(f"  [dim]{label:20}[/dim] {value}", highlight=False)

    row_line("ID",       p.get("problem_id", ""))
    row_line("Host",     host)
    row_line("Severity", f"[{theme.SEV_COLORS.get(sev,'white')}]{sev}[/{theme.SEV_COLORS.get(sev,'white')}]")
    row_line("Status",   p.get("status", ""))
    row_line("Alerts",   str(p.get("alert_count", 0)))
    row_line("Events",   str(p.get("events_count", 0)))

    opened = p.get("opened_at")
    if opened:
        row_line("Opened",   str(opened)[:19])
    if p.get("resolved_at"):
        row_line("Resolved", str(p["resolved_at"])[:19])
        row_line("Duration", f"{p.get('duration_s', 0):.0f}s")

    titles = p.get("alert_titles") or []
    if titles:
        console.print(f"\n  [dim cyan]Alert titles:[/dim cyan]")
        for t in titles[:10]:
            console.print(f"  [dim]•[/dim] {t}")
    console.print()


def run_close(problem_id: str):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT status FROM problems WHERE problem_id = %s", [problem_id])
    row = cur.fetchone()
    if not row:
        console.print(f"\n  [red]Problem not found: {problem_id}[/red]\n")
        return
    if row[0] == "resolved":
        console.print(f"\n  [dim]Problem already resolved.[/dim]\n")
        return
    cur.execute("""
        UPDATE problems
        SET status = 'resolved', resolved_at = NOW(),
            duration_s = EXTRACT(EPOCH FROM (NOW() - opened_at))
        WHERE problem_id = %s
    """, [problem_id])
    conn.commit()
    console.print(f"\n  [green]● Problem closed: {problem_id}[/green]\n")
