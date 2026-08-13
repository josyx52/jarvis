import os
import sys
from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich.text import Text

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from context import get_db, ensure_config_table, get_config_value, set_config_value
from ui import theme

console = Console()

_CATEGORY_ORDER = ["thresholds", "baseline", "llm", "problems", "retention", "agent"]


def _fetch_all() -> list[dict]:
    ensure_config_table()
    cur = get_db().cursor()
    cur.execute("SELECT key, value, type, category, description, updated_at FROM jarvis_config ORDER BY category, key")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _cast(value: str, typ: str):
    if typ == "int":   return int(value)
    if typ == "float": return float(value)
    if typ == "bool":  return value.lower() in ("true", "1", "yes")
    return value


def run_show(category: str | None = None):
    ensure_config_table()
    rows = _fetch_all()
    if category:
        rows = [r for r in rows if r["category"] == category]

    tbl = Table(
        show_header=True, header_style="dim cyan",
        border_style="dim cyan", padding=(0, 1),
    )
    tbl.add_column("Key",         style="cyan",         no_wrap=True, width=32)
    tbl.add_column("Value",       style="bold white",   no_wrap=True, width=28)
    tbl.add_column("Type",        style="dim",          no_wrap=True, width=8)
    tbl.add_column("Category",    style="dim",          no_wrap=True, width=12)
    tbl.add_column("Description", style="dim",                        width=36)

    current_cat = None
    for r in sorted(rows, key=lambda x: (_CATEGORY_ORDER.index(x["category"]) if x["category"] in _CATEGORY_ORDER else 99, x["key"])):
        if r["category"] != current_cat:
            current_cat = r["category"]
            tbl.add_row(
                f"[dim]── {current_cat} ──[/dim]", "", "", "", "",
                style="dim",
            )
        tbl.add_row(r["key"], r["value"], r["type"], r["category"], r["description"] or "")

    console.print()
    console.print(tbl)
    console.print(f"  [dim]{len(rows)} keys[/dim]\n")


def run_set(key: str, value: str):
    ensure_config_table()
    cur = get_db().cursor()
    cur.execute("SELECT type, category FROM jarvis_config WHERE key = %s", [key])
    row = cur.fetchone()

    if row is None:
        console.print(f"\n  [red]● Key not found: [bold]{key}[/bold][/red]")
        console.print("  [dim]Use [cyan]jarvis-ctl config show[/cyan] to list valid keys.[/dim]\n")
        return

    typ, cat = row
    try:
        _cast(value, typ)
    except (ValueError, TypeError):
        console.print(f"\n  [red]● Invalid value for type [bold]{typ}[/bold]: {value}[/red]\n")
        return

    old = get_config_value(key)
    set_config_value(key, value)
    console.print(f"\n  [green]● [bold]{key}[/bold]  {old} → {value}[/green]\n")


def run_thresholds():
    """Interactive threshold editor."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.styles import Style as PTStyle

    ensure_config_table()
    session = PromptSession(style=PTStyle.from_dict({"prompt": "ansiyellow bold", "": "ansiwhite"}))

    threshold_keys = [
        ("alert.cpu_threshold",    "CPU alert threshold (%)"),
        ("alert.memory_threshold", "Memory alert threshold (%)"),
        ("alert.disk_threshold",   "Disk alert threshold (%)"),
        ("baseline.sigma_warn",    "Baseline z-score for medium anomaly"),
        ("baseline.sigma_high",    "Baseline z-score for high anomaly"),
    ]

    console.print("\n  [dim cyan]▲ Threshold Editor[/dim cyan]  [dim](Enter to keep current, Ctrl+C to exit)[/dim]\n")

    for key, label in threshold_keys:
        current = get_config_value(key, "—")
        try:
            new_val = session.prompt(
                [("class:prompt", f"  {label} [{current}]: ")],
            ).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n  [dim]Cancelled.[/dim]\n")
            return
        if new_val:
            set_config_value(key, new_val)
            console.print(f"  [green]✓ {key} = {new_val}[/green]")
        else:
            console.print(f"  [dim]  {key} unchanged ({current})[/dim]")

    console.print()


def run_export() -> str:
    """Export config as JSON string."""
    import json
    rows = _fetch_all()
    out  = {r["key"]: r["value"] for r in rows}
    return json.dumps(out, indent=2)
