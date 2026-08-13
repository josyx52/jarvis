"""
Lachesis — interpretação de agendamentos em linguagem natural via LLM.

Segue o padrão de jarvis_center/clotho/clotho_tester.py: AnthropicFoundry,
prompt JSON-only, remoção de markdown fences.
"""

import json
import os
import re
from datetime import datetime

from lachesis.lachesis_schedule import compute_next_run, describe_schedule

_SCHEDULE_SYSTEM = """Você é o Lachesis, o motor de agendamento do Jarvis Fates Engine.

O utilizador descreve em linguagem natural (português) quando uma tarefa deve correr.
Converta essa descrição num descritor JSON de agendamento, segundo um destes formatos:

{"kind": "once", "at": "YYYY-MM-DDTHH:MM:SS"}
{"kind": "daily", "time": "HH:MM"}
{"kind": "weekly", "weekday": 0-6, "time": "HH:MM"}   (0=segunda ... 6=domingo)
{"kind": "interval", "minutes": N}
{"kind": "monthly", "day": 1-31, "time": "HH:MM"}

A data/hora actual será fornecida — use-a para resolver expressões relativas
("amanhã", "hoje à tarde", "dentro de 2 horas", etc.) e para "once" use sempre
uma data/hora no futuro.

Responda APENAS com o objecto JSON do descritor, sem markdown."""


def _client():
    from anthropic import AnthropicFoundry
    return AnthropicFoundry(
        api_key  = os.getenv("FOUNDRY_API_KEY",  ""),
        base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
    )


def parse_schedule_description(description: str, now: datetime | None = None) -> dict:
    """Converte texto livre num descritor de agendamento.

    Devolve {"schedule": {...}, "summary": "...", "next_run_at": "...",
    "parse_warning": <str|None>}.
    """
    now = now or datetime.now()

    resp = _client().messages.create(
        model      = "claude-sonnet-4-6",
        system     = _SCHEDULE_SYSTEM,
        messages   = [{"role": "user", "content": f"Data/hora actual: {now.isoformat()}\n\nDescrição: {description}"}],
        max_tokens = 256,
    )

    text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    parse_warning = None
    schedule = None
    try:
        schedule = json.loads(text)
    except json.JSONDecodeError:
        parse_warning = "Não foi possível interpretar o agendamento; aplicado valor por omissão (diário às 08:00)."

    if not isinstance(schedule, dict) or compute_next_run(schedule, now) is None and schedule.get("kind") != "once":
        if schedule is not None:
            parse_warning = parse_warning or "Agendamento inválido; aplicado valor por omissão (diário às 08:00)."
        schedule = {"kind": "daily", "time": "08:00"}

    next_run = compute_next_run(schedule, now)
    return {
        "schedule": schedule,
        "summary": describe_schedule(schedule),
        "next_run_at": next_run.isoformat() if next_run else None,
        "parse_warning": parse_warning,
    }
