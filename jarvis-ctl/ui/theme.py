from rich.style import Style

BORDER   = Style(color="cyan", dim=True)
ART      = Style(color="white", bold=True)
DELTA    = Style(color="yellow", bold=True)
SUBTITLE = Style(color="bright_white")
VERSION  = Style(color="cyan", dim=True)
DIM      = Style(dim=True)
URL_ST   = Style(color="cyan")
SUCCESS  = Style(color="green", bold=True)
WARNING  = Style(color="yellow", bold=True)
ERROR    = Style(color="red", bold=True)
INFO     = Style(color="cyan")
PROMPT   = Style(color="yellow", bold=True)
USER_MSG = Style(color="bright_white")
AI_MSG   = Style(color="white")
TOOL_BOX = Style(color="bright_black")

SEV_COLORS = {
    "critical": "bold red",
    "high":     "red",
    "medium":   "yellow",
    "low":      "dim white",
    "unknown":  "dim",
}

STATUS_COLORS = {
    "open":     "red",
    "resolved": "green",
    "running":  "green",
    "stopped":  "red",
    "online":   "green",
    "offline":  "red",
}
