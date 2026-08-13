from rich.console import Console
from rich.text import Text

JARVIS_VERSION = "2.1.0"

_ART = [
    "     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗                    ",
    "     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝                    ",
    "     ██║███████║██████╔╝██║   ██║██║███████╗              ▲      ",
    "██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║                    ",
    "╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║                    ",
    " ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝                    ",
]

# inner width derived from first art line
_W = max(len(l) for l in _ART)


def _row(console: Console, content: str, markup: bool = False):
    """Prints a banner row: ║ content (padded to _W) ║"""
    if markup:
        # content already has Rich markup — measure plain length separately
        plain_len = Text.from_markup(content).cell_len
        pad = " " * max(0, _W - plain_len)
        console.print(f"[dim cyan]║[/dim cyan]{content}{pad}[dim cyan]║[/dim cyan]",
                      highlight=False)
    else:
        pad = " " * max(0, _W - len(content))
        console.print(f"[dim cyan]║[/dim cyan]{content}{pad}[dim cyan]║[/dim cyan]",
                      highlight=False)


def _art_row(console: Console, line: str):
    """Prints an art line, colouring ▲ yellow if present."""
    if "▲" in line:
        idx = line.index("▲")
        before = line[:idx]
        after  = line[idx + 1:]
        content = (
            f"[bold white]{before}[/bold white]"
            f"[bold yellow]▲[/bold yellow]"
            f"[bold white]{after}[/bold white]"
        )
    else:
        content = f"[bold white]{line}[/bold white]"
    _row(console, content, markup=True)


def print_banner(console: Console, connected: bool = True, db_url: str = ""):
    border_top = "╔" + "═" * _W + "╗"
    border_bot = "╚" + "═" * _W + "╝"
    empty      = "║" + " " * _W + "║"

    console.print(f"[dim cyan]{border_top}[/dim cyan]", highlight=False)
    console.print(f"[dim cyan]{empty}[/dim cyan]",      highlight=False)

    for line in _ART:
        _art_row(console, line)

    console.print(f"[dim cyan]{empty}[/dim cyan]", highlight=False)

    subtitle = f"     Infrastructure Intelligence Platform  [dim cyan]v{JARVIS_VERSION}[/dim cyan]"
    _row(console, subtitle, markup=True)

    dot = "[green]●[/green]" if connected else "[red]●[/red]"
    url = db_url or "postgres://jarvis@localhost/jarvis"
    conn_line = f"     Connected: [cyan]{url}[/cyan]  {dot}"
    _row(console, conn_line, markup=True)

    console.print(f"[dim cyan]{border_bot}[/dim cyan]", highlight=False)
    console.print()
