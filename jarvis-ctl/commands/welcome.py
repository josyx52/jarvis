import os
import sys

from rich.console import Console
from rich.text import Text
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from context import get_config_value, db_connected, db_url_str
from commands.llm_client import ollama_running, foundry_running
from ui.banner import print_banner
from ui import theme

console = Console()

_COMMANDS = [
    ("status",              "",                 "Live dashboard — agentes, problems, eventos"),
    ("chat",                "",                 "Chat interactivo com o LLM"),
    ("center status",       "",                 "Estado do serviço Center (API, Redis, DB)"),
    ("center start",        "",                 "Iniciar o serviço Jarvis Center"),
    ("center stop",         "",                 "Parar o serviço Jarvis Center"),
    ("center restart",      "",                 "Reiniciar o serviço Jarvis Center"),
    ("center logs",         "[-n 100]",         "Ver logs recentes do Center"),
    ("models",              "",                 "Ver modelos disponíveis (Foundry + Ollama)"),
    ("models use",          "<id>",             "Activar um modelo"),
    ("agents list",         "",                 "Listar todos os hosts monitorados"),
    ("agents show",         "<host>",           "Detalhe de um host (CPU, mem, eventos)"),
    ("problems list",       "[--status open]",  "Listar problems"),
    ("problems show",       "<id>",             "Detalhe de um Problem"),
    ("problems close",      "<id>",             "Fechar manualmente um Problem"),
    ("config show",         "[categoria]",      "Ver toda a configuração"),
    ("config set",          "<key> <value>",    "Alterar um valor de configuração"),
    ("config thresholds",   "",                 "Editor interactivo de thresholds"),
    ("config export",       "",                 "Exportar config como JSON"),
    ("solution status",     "",                 "Estado do Solution Driver e webhooks"),
    ("solution enable",     "",                 "Activar o Solution Driver"),
    ("solution disable",    "",                 "Desactivar o Solution Driver"),
    ("solution webhook list",   "",             "Listar webhooks de notificação"),
    ("solution webhook add",    "<url>",        "Registar webhook (testa imediatamente)"),
    ("solution webhook test",   "<url>",        "Testar um endpoint de notificação"),
    ("solution webhook enable", "<url>",        "Activar webhook"),
    ("solution webhook disable","<url>",        "Desactivar webhook"),
    ("solution webhook remove", "<url>",        "Remover webhook"),
]


def run_welcome():
    connected   = db_connected()
    provider    = get_config_value("llm.provider",   "foundry")
    model       = get_config_value("llm.model",      "claude-sonnet-4-6")
    ollama_url  = get_config_value("llm.ollama_url", "http://localhost:11434")

    print_banner(console, connected=connected, db_url=db_url_str())

    # ── Status bar ────────────────────────────────────────────────────────────
    db_s  = "[green]● connected[/green]" if connected else "[red]● disconnected[/red]"
    llm_s = f"[cyan]{provider}[/cyan] / [cyan]{model}[/cyan]"

    if provider == "ollama":
        ok = ollama_running(ollama_url)
    else:
        ok = foundry_running()
    llm_dot = "[green]●[/green]" if ok else "[red]●[/red]"
    llm_s += f"  {llm_dot}"

    console.print(f"  DB  {db_s}    LLM  {llm_s}\n", highlight=False)

    # ── Commands table ────────────────────────────────────────────────────────
    tbl = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
    tbl.add_column(style="cyan",         no_wrap=True, width=26)
    tbl.add_column(style="dim cyan",     no_wrap=True, width=20)
    tbl.add_column(style="dim",                        width=46)

    prev_group = None
    for cmd, args, desc in _COMMANDS:
        group = cmd.split()[0]
        if group != prev_group and prev_group is not None:
            tbl.add_row("", "", "")
        prev_group = group
        tbl.add_row(f"jarvis-ctl {cmd}", args, desc)

    console.print(tbl)
    console.print()
    console.print("  [dim]jarvis-ctl <comando> --help   para opções detalhadas[/dim]")
    console.print()
