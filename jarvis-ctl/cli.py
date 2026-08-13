import sys
import os
from typing import Optional

import typer
from rich.console import Console

sys.path.insert(0, os.path.dirname(__file__))

app     = typer.Typer(
    name="jarvis-ctl",
    help="▲ Jarvis — Infrastructure Intelligence Platform",
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=False,
)
console = Console()

# ── Sub-apps ──────────────────────────────────────────────────────────────────

agents_app   = typer.Typer(help="Manage monitored agents/hosts",      no_args_is_help=True)
problems_app = typer.Typer(help="View and manage Problems",            no_args_is_help=True)
config_app   = typer.Typer(help="Read and write Jarvis config",        no_args_is_help=True)
models_app   = typer.Typer(help="List and switch LLM models",          no_args_is_help=False)
center_app   = typer.Typer(help="Manage the Jarvis Center service",    no_args_is_help=True)
solution_app = typer.Typer(help="Manage Solution Driver and webhooks", no_args_is_help=True)
webhook_app  = typer.Typer(help="Manage notification webhooks",        no_args_is_help=True)

app.add_typer(agents_app,   name="agents")
app.add_typer(problems_app, name="problems")
app.add_typer(config_app,   name="config")
app.add_typer(models_app,   name="models")
app.add_typer(center_app,   name="center")
app.add_typer(solution_app, name="solution")
solution_app.add_typer(webhook_app, name="webhook")


# ── Default: no subcommand → welcome screen ───────────────────────────────────

@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        from commands.welcome import run_welcome
        run_welcome()


# ── status ────────────────────────────────────────────────────────────────────

@app.command()
def status(
    refresh: int = typer.Option(5, "--refresh", "-r", help="Refresh interval in seconds"),
):
    """Live dashboard — agents, problems, recent events."""
    from commands.status import run_status
    run_status(refresh=refresh)


# ── chat ──────────────────────────────────────────────────────────────────────

@app.command()
def chat():
    """Interactive AI chat — ask questions about your infrastructure."""
    from commands.chat import run_chat
    run_chat()


# ── models ────────────────────────────────────────────────────────────────────

@models_app.callback(invoke_without_command=True)
def models_default(ctx: typer.Context):
    """List available LLM models (Foundry + Ollama)."""
    if ctx.invoked_subcommand is None:
        from commands.models import run_models
        run_models()


@models_app.command("use")
def models_use(
    args: list[str] = typer.Argument(..., help="[foundry|ollama] <model_id>"),
):
    """Activate a model.  Examples: use llama3.2   use ollama qwen2.5:14b"""
    from commands.models import run_use
    run_use(" ".join(args))


# ── agents ────────────────────────────────────────────────────────────────────

@agents_app.command("list")
def agents_list():
    """List all monitored hosts."""
    from commands.agents import run_list
    run_list()


@agents_app.command("show")
def agents_show(host: str = typer.Argument(..., help="Host name or partial key")):
    """Show details for a specific host."""
    from commands.agents import run_show
    run_show(host)


# ── problems ──────────────────────────────────────────────────────────────────

@problems_app.command("list")
def problems_list(
    status: str = typer.Option("open", "--status", "-s", help="open | resolved | all"),
):
    """List Problems."""
    from commands.problems import run_list
    run_list(status_filter=status)


@problems_app.command("show")
def problems_show(problem_id: str = typer.Argument(..., help="Problem ID")):
    """Show details for a Problem."""
    from commands.problems import run_show
    run_show(problem_id)


@problems_app.command("close")
def problems_close(problem_id: str = typer.Argument(..., help="Problem ID to close")):
    """Manually close a Problem."""
    from commands.problems import run_close
    run_close(problem_id)


# ── config ────────────────────────────────────────────────────────────────────

@config_app.command("show")
def config_show(
    category: Optional[str] = typer.Argument(None, help="Category filter (optional)"),
):
    """Show all configuration values."""
    from commands.config import run_show
    run_show(category=category)


@config_app.command("set")
def config_set(
    key:   str = typer.Argument(..., help="Config key  e.g. llm.model"),
    value: str = typer.Argument(..., help="New value"),
):
    """Set a configuration value."""
    from commands.config import run_set
    run_set(key, value)


@config_app.command("thresholds")
def config_thresholds():
    """Interactive threshold editor."""
    from commands.config import run_thresholds
    run_thresholds()


@config_app.command("export")
def config_export():
    """Export config as JSON."""
    from commands.config import run_export
    print(run_export())
    
# ── center ────────────────────────────────────────────────────────────────────

@center_app.command("status")
def center_status():
    """Show Jarvis Center service state and component health."""
    from commands.center import run_status
    run_status()


@center_app.command("start")
def center_start():
    """Start the Jarvis Center Windows service."""
    from commands.center import run_start
    run_start()


@center_app.command("stop")
def center_stop():
    """Stop the Jarvis Center Windows service."""
    from commands.center import run_stop
    run_stop()


@center_app.command("restart")
def center_restart():
    """Restart the Jarvis Center Windows service."""
    from commands.center import run_restart
    run_restart()


@center_app.command("logs")
def center_logs(
    lines: int = typer.Option(50, "--lines", "-n", help="Number of log lines to show"),
):
    """Show recent Jarvis Center log output."""
    from commands.center import run_logs
    run_logs(lines=lines)


# ── solution ──────────────────────────────────────────────────────────────────

@solution_app.command("status")
def solution_status():
    """Show Solution Driver state and webhook health."""
    from commands.solution import run_status
    run_status()


@solution_app.command("enable")
def solution_enable():
    """Enable the Solution Driver."""
    from commands.solution import run_enable
    run_enable()


@solution_app.command("disable")
def solution_disable():
    """Disable the Solution Driver."""
    from commands.solution import run_disable
    run_disable()


# ── solution webhook ──────────────────────────────────────────────────────────

@webhook_app.command("list")
def webhook_list():
    """List all configured notification webhooks."""
    from commands.solution import run_webhook_list
    run_webhook_list()


@webhook_app.command("add")
def webhook_add(
    url:          str = typer.Argument(...,                  help="Webhook URL"),
    name:         str = typer.Option("",     "--name",  "-n", help="Friendly name"),
    min_severity: str = typer.Option("high", "--severity",    help="Minimum severity: low|medium|high|critical"),
    host_filter:  str = typer.Option("",     "--hosts",       help="Comma-separated host prefixes (empty = all)"),
):
    """Register a new notification webhook and test it immediately."""
    from commands.solution import run_webhook_add
    run_webhook_add(url, name, min_severity, host_filter)


@webhook_app.command("enable")
def webhook_enable(url: str = typer.Argument(..., help="Webhook URL")):
    """Enable a webhook."""
    from commands.solution import run_webhook_toggle
    run_webhook_toggle(url, active=True)


@webhook_app.command("disable")
def webhook_disable(url: str = typer.Argument(..., help="Webhook URL")):
    """Disable a webhook (keeps it registered)."""
    from commands.solution import run_webhook_toggle
    run_webhook_toggle(url, active=False)


@webhook_app.command("remove")
def webhook_remove(url: str = typer.Argument(..., help="Webhook URL")):
    """Remove a webhook permanently."""
    from commands.solution import run_webhook_remove
    run_webhook_remove(url)


@webhook_app.command("test")
def webhook_test(url: str = typer.Argument(..., help="Webhook URL to test")):
    """Send a health-check POST to a webhook and show the result."""
    from commands.solution import run_webhook_test
    run_webhook_test(url)


if __name__ == "__main__":
    app()