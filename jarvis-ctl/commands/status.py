import os
import sys
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.columns import Columns

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from context import get_db, db_connected
from ui import theme

console = Console()


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d  %H:%M:%S")


def _fetch_open_problems() -> list[dict]:
    try:
        cur = get_db().cursor()
        cur.execute("""
            SELECT problem_id, host, title, severity, alert_count, opened_at
            FROM problems WHERE status = 'open'
            ORDER BY
                CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                              WHEN 'medium' THEN 2 ELSE 3 END,
                opened_at DESC
            LIMIT 8
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _fetch_recent_events(limit=10) -> list[dict]:
    try:
        cur = get_db().cursor()
        cur.execute("""
            SELECT host, event_type, severity, event_time
            FROM events
            WHERE event_time >= NOW() - INTERVAL '10 minutes'
            ORDER BY event_time DESC LIMIT %s
        """, [limit])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _fetch_agent_counts() -> tuple[int, int]:
    try:
        cur = get_db().cursor()
        cur.execute("SELECT COUNT(DISTINCT host) FROM snapshots")
        total = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(DISTINCT host) FROM snapshots WHERE snapshot_time >= EXTRACT(EPOCH FROM NOW() - INTERVAL '5 minutes')")
        active = cur.fetchone()[0] or 0
        return active, total
    except Exception:
        return 0, 0


def _fetch_system_health() -> dict:
    try:
        cur = get_db().cursor()
        cur.execute("SELECT COUNT(*) FROM events WHERE event_time >= NOW() - INTERVAL '1 minute'")
        events_pm = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM alerts WHERE alert_time >= NOW() - INTERVAL '1 minute'")
        alerts_pm = cur.fetchone()[0] or 0
        return {"events_pm": events_pm, "alerts_pm": alerts_pm}
    except Exception:
        return {"events_pm": 0, "alerts_pm": 0}


def _sev_style(sev: str) -> str:
    return theme.SEV_COLORS.get(sev, "white")


def _age(ts) -> str:
    if ts is None:
        return "—"
    now = datetime.now(tz=timezone.utc)
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    diff = int((now - ts).total_seconds())
    if diff < 60:
        return f"{diff}s"
    if diff < 3600:
        return f"{diff//60}m"
    return f"{diff//3600}h{(diff%3600)//60}m"


def _build_header(active: int, total: int, problems: list) -> Panel:
    t = Text()
    t.append("▲ JARVIS", style=theme.DELTA)
    t.append("  │  ", style="dim")
    t.append(f"Agents {active}/{total}", style="green" if active == total else "yellow")
    t.append("  │  ", style="dim")
    open_cnt = len(problems)
    sev_style = "red" if any(p["severity"] == "critical" for p in problems) else ("yellow" if open_cnt else "green")
    t.append(f"Problems {open_cnt}", style=sev_style)
    t.append("  │  ", style="dim")
    t.append(_now_str(), style="dim")
    return Panel(t, border_style="dim cyan", padding=(0, 1))


def _build_problems_table(problems: list) -> Table:
    tbl = Table(
        show_header=True, header_style="dim cyan",
        border_style="dim", box=None, padding=(0, 1),
    )
    tbl.add_column("ID",       style="dim",          no_wrap=True, width=26)
    tbl.add_column("Host",     style="bright_white",  no_wrap=True, width=22)
    tbl.add_column("Severity", no_wrap=True,          width=10)
    tbl.add_column("Title",    style="white",          width=32)
    tbl.add_column("Age",      style="dim",           no_wrap=True, width=8)

    if not problems:
        tbl.add_row("[green]—[/green]", "No open problems", "", "", "")
        return tbl

    for p in problems:
        sev  = p.get("severity", "unknown")
        host = (p.get("host") or "").split("::")[0]
        tbl.add_row(
            p.get("problem_id", ""),
            host,
            f"[{_sev_style(sev)}]{sev}[/{_sev_style(sev)}]",
            (p.get("title") or "")[:32],
            _age(p.get("opened_at")),
        )
    return tbl


def _build_events_table(events: list) -> Table:
    tbl = Table(
        show_header=True, header_style="dim cyan",
        border_style="dim", box=None, padding=(0, 1),
    )
    tbl.add_column("Time",       style="dim",          no_wrap=True, width=8)
    tbl.add_column("Host",       style="bright_white", no_wrap=True, width=22)
    tbl.add_column("Event",      style="cyan",         no_wrap=True, width=30)
    tbl.add_column("Severity",   no_wrap=True,          width=10)

    if not events:
        tbl.add_row("—", "No events in last 10 minutes", "", "")
        return tbl

    for e in events:
        sev  = e.get("severity", "unknown")
        host = (e.get("host") or "").split("::")[0]
        ts   = e.get("event_time")
        ts_s = ts.strftime("%H:%M:%S") if ts and hasattr(ts, "strftime") else str(ts or "")
        tbl.add_row(
            ts_s,
            host,
            (e.get("event_type") or "")[:30],
            f"[{_sev_style(sev)}]{sev}[/{_sev_style(sev)}]",
        )
    return tbl


def _build_dashboard() -> str:
    if not db_connected():
        return Panel("[red]● Database unreachable[/red]", border_style="red")

    active, total = _fetch_agent_counts()
    problems      = _fetch_open_problems()
    events        = _fetch_recent_events()
    health        = _fetch_system_health()

    from rich.console import Console as C
    from io import StringIO
    buf = StringIO()
    c2  = C(file=buf, width=120, highlight=False)

    c2.print(_build_header(active, total, problems))
    c2.print()
    c2.print(Panel(
        _build_problems_table(problems),
        title="[dim cyan]▲ Active Problems[/dim cyan]",
        border_style="dim cyan",
        padding=(0, 1),
    ))
    c2.print(Panel(
        _build_events_table(events),
        title="[dim cyan]Recent Events  (last 10m)[/dim cyan]",
        border_style="dim",
        padding=(0, 1),
    ))

    footer = Text()
    footer.append(f"  events/min: {health['events_pm']}   alerts/min: {health['alerts_pm']}",
                  style="dim")
    footer.append("   │  ", style="dim")
    footer.append("[r] refresh  [q] quit", style="dim cyan")
    c2.print(footer)

    return buf.getvalue()


def run_status(refresh: int = 5):
    """Live dashboard. Refreshes every `refresh` seconds."""
    from rich.live import Live

    with Live(console=console, refresh_per_second=1, screen=False) as live:
        try:
            while True:
                live.update(_build_dashboard())
                time.sleep(refresh)
        except KeyboardInterrupt:
            pass
