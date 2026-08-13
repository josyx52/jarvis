"""
Lachesis — cálculo de próxima execução a partir de um descritor de agendamento.

Descritores suportados (JSONB em lachesis_tasks.schedule):
  {"kind": "once",     "at": "2026-06-20T08:00:00"}
  {"kind": "daily",    "time": "08:00"}
  {"kind": "weekly",   "weekday": 1, "time": "08:00"}   # 0=segunda ... 6=domingo
  {"kind": "interval", "minutes": 30}
  {"kind": "monthly",  "day": 1, "time": "08:00"}

Sem dependências externas — usa apenas datetime + calendar (stdlib).
"""

import calendar
from datetime import datetime, timedelta


def _parse_time(value: str) -> tuple[int, int]:
    hh, mm = value.split(":")
    return int(hh), int(mm)


def _day_in_month(year: int, month: int, day: int) -> int:
    """Faz clamp de `day` ao último dia válido de year/month (ex: 31 em Fevereiro -> 28/29)."""
    return min(day, calendar.monthrange(year, month)[1])


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def compute_next_run(schedule: dict, now: datetime | None = None) -> datetime | None:
    """Devolve o próximo datetime de execução para o descritor dado.

    Devolve None se o agendamento não tiver mais execuções futuras
    (ex: "once" cuja data já passou).
    """
    now = now or datetime.now()
    kind = (schedule or {}).get("kind")

    if kind == "once":
        at = datetime.fromisoformat(schedule["at"])
        return at if at > now else None

    if kind == "interval":
        minutes = int(schedule.get("minutes", 60))
        return now + timedelta(minutes=minutes)

    if kind == "daily":
        hh, mm = _parse_time(schedule.get("time", "08:00"))
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if kind == "weekly":
        hh, mm = _parse_time(schedule.get("time", "08:00"))
        target_weekday = int(schedule.get("weekday", 0))
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        days_ahead = (target_weekday - now.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if kind == "monthly":
        hh, mm = _parse_time(schedule.get("time", "08:00"))
        day = int(schedule.get("day", 1))
        year, month = now.year, now.month
        candidate = datetime(year, month, _day_in_month(year, month, day), hh, mm)
        if candidate <= now:
            year, month = _next_month(year, month)
            candidate = datetime(year, month, _day_in_month(year, month, day), hh, mm)
        return candidate

    return None


def describe_schedule(schedule: dict) -> str:
    """Resumo legível (português) de um descritor, para a UI."""
    kind = (schedule or {}).get("kind")

    if kind == "once":
        return f"Uma vez em {schedule.get('at', '?')}"
    if kind == "interval":
        return f"A cada {schedule.get('minutes', '?')} minutos"
    if kind == "daily":
        return f"Todos os dias às {schedule.get('time', '?')}"
    if kind == "weekly":
        dias = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
        weekday = int(schedule.get("weekday", 0))
        nome = dias[weekday] if 0 <= weekday < 7 else "?"
        return f"Toda {nome} às {schedule.get('time', '?')}"
    if kind == "monthly":
        return f"No dia {schedule.get('day', '?')} de cada mês às {schedule.get('time', '?')}"

    return "Agendamento desconhecido"
