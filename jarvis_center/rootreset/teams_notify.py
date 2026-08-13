"""
Notificação directa ao grupo Teams "Solutions Driver Endpoint" quando um
pedido de reset root é criado — sem passar pelo webhook PowerAutomate
(notification_webhooks), que é genérico e serve outras automações. Reaproveita
a mesma conta/token Graph já autenticado por teams_poller.py (bot
jarvis-bot@example.com, já membro deste grupo).

chat_id descoberto via GET /me/chats (ver COORDINATION.md se precisar de
repetir a descoberta) — configurável em ROOT_RESET_TEAMS_CHAT_ID para não
ficar hardcoded no código caso o grupo seja recriado.

Best-effort: uma falha aqui nunca deve impedir o registo do pedido — este
continua visível em /pedidos mesmo que a notificação ao Teams falhe.
"""

import html
import os

_CHAT_ID = os.getenv("ROOT_RESET_TEAMS_CHAT_ID", "")


def notify_teams(hostname: str, motivo_text: str, requested_by: str, request_id: int, auto: bool = False) -> bool:
    if not _CHAT_ID:
        return False
    try:
        from teams.teams_poller import _poller

        if auto:
            action_line = "Motivo pré-aprovado — a executar automaticamente, sem acção necessária."
        else:
            action_line = "Aprovar em: JarvisWeb → Pedidos → Workstations (id " + str(request_id) + ")"

        titulo = "Reset root auto-aprovado" if auto else "Novo pedido de reset root"
        body = (
            f"🔐 <b>{titulo}</b><br>"
            f"Host: <b>{html.escape(hostname)}</b><br>"
            f"Motivo: {html.escape(motivo_text)}<br>"
            f"Pedido por: {html.escape(requested_by)}<br>"
            f"{action_line}"
        )
        return _poller._post(
            f"/chats/{_CHAT_ID}/messages",
            {"body": {"content": body, "contentType": "html"}},
        )
    except Exception:
        return False
