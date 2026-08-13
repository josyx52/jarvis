import os
import sys

from rich.console import Console
from rich.table import Table
from rich.text import Text

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from context import get_config_value, set_config_value, ensure_config_table
from commands.llm_client import (
    get_foundry_models, foundry_running,
    get_ollama_models,  ollama_running,
)
from ui import theme

console = Console()

_TOOL_CAPABLE_PREFIXES = {
    "llama3.2", "llama3.3", "llama3.1",
    "qwen2.5", "qwen2", "qwen3",
    "mistral", "mistral-nemo", "mistral-small", "mixtral",
    "command-r",
    "granite3",
    "firefunction",
    "hermes",
    "nous-hermes",
}


def _tool_support(model_name: str) -> str:
    base = model_name.split(":")[0].lower()
    for prefix in _TOOL_CAPABLE_PREFIXES:
        if base == prefix or base.startswith(prefix):
            return "[green]✓[/green]"
    return "[dim]?[/dim]"


def run_models():
    ensure_config_table()

    active_provider = get_config_value("llm.provider",   "foundry")
    active_model    = get_config_value("llm.model",      "claude-sonnet-4-6")
    ollama_url      = get_config_value("llm.ollama_url", "http://localhost:11434")
    foundry_key     = os.getenv("FOUNDRY_API_KEY", "")
    foundry_url     = os.getenv("FOUNDRY_ENDPOINT", "")

    console.print()

    # ── Foundry ───────────────────────────────────────────────────────────────
    with console.status("  [dim]a consultar Foundry...[/dim]", spinner="dots"):
        foundry_models = get_foundry_models(api_key=foundry_key, base_url=foundry_url or None)
        foundry_ok     = bool(foundry_models)

    dot_f = "[green]● online[/green]" if foundry_ok else "[red]● offline / sem chave[/red]"
    console.print(f"  [dim cyan]── Foundry  (Azure AI / Anthropic)  {dot_f} ──[/dim cyan]",
                  highlight=False)

    if foundry_models:
        tbl_f = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
        tbl_f.add_column(no_wrap=True, width=4)
        tbl_f.add_column(style="cyan", no_wrap=True, width=36)
        tbl_f.add_column(style="dim",  no_wrap=True, width=28)

        for m in foundry_models:
            active = m == active_model and active_provider == "foundry"
            mark   = "[yellow]▲[/yellow]" if active else " "
            tbl_f.add_row(mark, m, f"jarvis-ctl models use foundry {m}")
        console.print(tbl_f)
    else:
        console.print("  [dim]  Sem modelos — verifique FOUNDRY_API_KEY e FOUNDRY_ENDPOINT[/dim]")

    console.print()

    # ── Ollama ────────────────────────────────────────────────────────────────
    with console.status("  [dim]a consultar Ollama...[/dim]", spinner="dots"):
        ollama_ok     = ollama_running(ollama_url)
        ollama_models = get_ollama_models(ollama_url) if ollama_ok else []

    dot_o = "[green]● online[/green]" if ollama_ok else "[red]● offline[/red]"
    console.print(f"  [dim cyan]── Ollama  ({ollama_url})  {dot_o} ──[/dim cyan]",
                  highlight=False)

    if ollama_ok and ollama_models:
        tbl_o = Table(show_header=True, header_style="dim", box=None,
                      padding=(0, 2), show_edge=False)
        tbl_o.add_column("",       no_wrap=True, width=4)
        tbl_o.add_column("Model",  style="cyan", no_wrap=True, width=32)
        tbl_o.add_column("Tools",  no_wrap=True, width=7, justify="center")
        tbl_o.add_column("Usar",   style="dim",  no_wrap=True)

        for m in ollama_models:
            active = m == active_model and active_provider == "ollama"
            mark   = "[yellow]▲[/yellow]" if active else " "
            tbl_o.add_row(mark, m, _tool_support(m), f"jarvis-ctl models use ollama {m}")
        console.print(tbl_o)

    elif ollama_ok and not ollama_models:
        console.print("  [dim]  Nenhum modelo instalado.[/dim]")
        console.print("  [dim]  → [cyan]ollama pull llama3.2[/cyan]   ou   [cyan]ollama pull qwen2.5:14b[/cyan][/dim]")

    else:
        console.print(f"  [dim]  Iniciar Ollama ou: [cyan]jarvis-ctl config set llm.ollama_url <url>[/cyan][/dim]")

    # ── Legenda + activo ──────────────────────────────────────────────────────
    console.print()
    console.print(
        "  [yellow]▲[/yellow] [dim]activo[/dim]   "
        "[green]✓[/green] [dim]suporta tool calling[/dim]   "
        "[dim]? suporte incerto[/dim]"
    )
    console.print()

    t = Text()
    t.append("  Activo: ", style="dim")
    t.append(active_provider, style="cyan")
    t.append("  /  ", style="dim")
    t.append(active_model, style="bold cyan")
    console.print(t)
    console.print()


def run_use(args: str):
    ensure_config_table()
    parts    = args.strip().split(None, 1)
    provider = get_config_value("llm.provider", "foundry")
    model    = get_config_value("llm.model",    "claude-sonnet-4-6")

    if not parts:
        console.print("\n  [red]Uso: jarvis-ctl models use [foundry|ollama] <model_id>[/red]\n")
        return

    if parts[0].lower() in ("foundry", "ollama"):
        provider = parts[0].lower()
        if len(parts) > 1:
            model = parts[1].strip()
        else:
            console.print(f"\n  [red]Falta o model_id após '{provider}'[/red]\n")
            return
    else:
        model = parts[0]

    set_config_value("llm.provider", provider)
    set_config_value("llm.model",    model)
    console.print(f"\n  [green]▲ Activo: [bold]{provider}[/bold]  /  [bold]{model}[/bold][/green]\n")
