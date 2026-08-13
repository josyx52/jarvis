"""
service.handle_root_message — ponto de entrada único do perfil de chat
"root-only" (ver api/ingestion_api.py::openai_chat_completions).

Determinístico, sem chamada nenhuma ao Claude/Anthropic. O reset já não
acontece na hora: cria um pedido pendente, notifica os admins (separador
"Pedidos" + webhook), e só executa (reset_root_rsi/LAPS via WinRM/AD
directo) quando um admin aprova em rootreset_api.py::approve_request. A
senha nunca é persistida — fica em Redis (result_cache.py) só até o
utilizador a receber reenviando o mesmo comando.
"""

import json
import os
import threading

import psycopg2

from rootreset.parser import RootCommandError, parse_root_command
from rootreset.store import RootResetStore

_HELP_MESSAGE = (
    "Esta conta só serve para pedir reset de senha root. Usa um dos formatos:\n"
    "  root_<motivo> <hostname>\n"
    "  $root <motivo por extenso> <hostname>\n"
    "Exemplo: root_instalacao_kalano wkslptak72"
)

_store = RootResetStore()
_PENDING_STATUSES = ("pending_approval",)
_PROCESSING_STATUSES = ("processing",)
_FINAL_STATUSES = ("ok", "error", "denied")


def _pg_conn():
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


def _write_alert(hostname: str, severity: str, title: str, payload: dict) -> None:
    conn = _pg_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO alerts (host, severity, title, payload) VALUES (%s,%s,%s,%s)",
            (f"HOST::{hostname}", severity, title, json.dumps(payload)),
        )
    finally:
        conn.close()


def _notify_admins(request_id: int, hostname: str, motivo_text: str, requested_by: str, auto: bool = False) -> None:
    """Avisa a equipa de um pedido novo — mensagem directa ao grupo Teams
    "Solutions Driver Endpoint" (rootreset/teams_notify.py), sem depender do
    webhook PowerAutomate genérico (notification_webhooks) usado pelas outras
    automações. Falha silenciosa: a notificação nunca deve impedir o registo
    do pedido. `auto=True` quando o motivo está na lista de auto-aprovação —
    é só um aviso informativo, não há nada para o admin aprovar."""
    from rootreset.teams_notify import notify_teams
    notify_teams(hostname, motivo_text, requested_by, request_id, auto=auto)


def run_approval(request_id: int, hostname: str, approved_by: str) -> None:
    """Executa o reset (WinRM, com fallback LAPS) e entrega o resultado ao
    utilizador — partilhado entre a aprovação manual (rootreset_api.py,
    thread de fundo lançada no click do admin) e a auto-aprovação (chamado
    directamente aqui em handle_root_message quando o motivo está na
    whitelist de root_reset_auto_approve). Isolado num try/except que apanha
    tudo: seja qual for o erro, o utilizador tem sempre de receber resposta,
    nunca pode ficar pendurado à espera sem saber o que aconteceu."""
    from rootreset.client import RootResetClientError, get_laps_password, reset_root_password
    from rootreset.openwebui_push import push_message
    from rootreset.result_cache import store_result

    mechanism = None
    status = "error"
    error_message = None
    senha = None

    try:
        try:
            result = reset_root_password(hostname)
            mechanism = "winrm_reset"
            senha = result["senha"]
            status = "ok"
        except RootResetClientError as e1:
            reset_error = str(e1)
            try:
                result = get_laps_password(hostname)
                mechanism = "laps"
                senha = result["senha"]
                status = "ok"
            except RootResetClientError as e2:
                error_message = f"WinRM: {reset_error} | LAPS: {e2}"
    except Exception as e:
        error_message = f"Erro inesperado: {e}"

    updated = _store.finish(
        request_id, status, mechanism, error_message,
        approved_by=approved_by, schedule_investigation=(status == "ok"),
    )

    if status == "ok":
        store_result(request_id, {"senha": senha, "mechanism": mechanism})

    delivered = push_message(
        updated.get("requester_user_id"), updated.get("chat_id"), updated.get("message_id"),
        format_final_message(updated),
    )
    if delivered:
        _store.mark_delivered(request_id)


def handle_root_message(
    text: str,
    requested_by: str,
    requester_user_id: str | None = None,
    chat_id: str | None = None,
    message_id: str | None = None,
) -> str:
    try:
        parsed = parse_root_command(text)
    except RootCommandError as e:
        return str(e)

    if parsed is None:
        return _HELP_MESSAGE

    hostname = parsed.hostname

    # 1. Já há um pedido aprovado/negado/falhado por entregar? Entrega agora.
    existing = _store.find_existing(requested_by, hostname, parsed.motivo_slug, _FINAL_STATUSES)
    if existing is not None and not existing["delivered"]:
        _store.mark_delivered(existing["id"])
        return format_final_message(existing)

    # 2. Já foi aprovado e está a correr (WinRM/LAPS em thread de fundo, ver
    # rootreset_api.py::approve_request)? Não duplica — o resultado chega
    # sozinho a este chat assim que a execução terminar, mesmo que demore.
    processing = _store.find_existing(requested_by, hostname, parsed.motivo_slug, _PROCESSING_STATUSES)
    if processing is not None:
        _store.update_chat_ref(processing["id"], requester_user_id, chat_id, message_id)
        return (
            f"O teu pedido para {hostname} ({parsed.motivo_text}) já foi aprovado e está a ser "
            f"processado — recebes a resposta aqui automaticamente assim que terminar."
        )

    # 3. Já há um pedido pendente para o mesmo host/motivo? Não duplica — mas
    # actualiza a referência do chat, para a entrega automática (push) ir
    # para a conversa mais recente caso o utilizador tenha voltado a perguntar
    # noutra janela/chat.
    pending = _store.find_existing(requested_by, hostname, parsed.motivo_slug, _PENDING_STATUSES)
    if pending is not None:
        _store.update_chat_ref(pending["id"], requester_user_id, chat_id, message_id)
        return (
            f"O pedido para {hostname} ({parsed.motivo_text}) ainda está pendente de aprovação. "
            f"Assim que for aprovado recebes a senha aqui automaticamente."
        )

    # 4. Pedido novo.
    request = _store.create_pending(
        requested_by, parsed.motivo_slug, parsed.motivo_text, hostname,
        requester_user_id=requester_user_id, chat_id=chat_id, message_id=message_id,
    )

    # Motivo na whitelist de auto-aprovação (gerida pelo admin em /pedidos)?
    # Executa já, sem passar por aprovação humana — mesma execução partilhada
    # (run_approval) usada quando um admin clica "Aprovar".
    if _store.is_auto_approved(parsed.motivo_slug):
        _store.mark_processing(request["id"], approved_by="Automático (motivo pré-aprovado)")
        threading.Thread(
            target=run_approval,
            args=(request["id"], hostname, "Automático (motivo pré-aprovado)"),
            daemon=True,
        ).start()
        _notify_admins(request["id"], hostname, parsed.motivo_text, requested_by, auto=True)
        _write_alert(
            hostname, "info", f"Pedido de reset root de {requested_by} auto-aprovado (motivo pré-autorizado)",
            {"motivo": parsed.motivo_text, "requested_by": requested_by, "request_id": request["id"]},
        )
        return (
            f"Pedido para {hostname} (motivo: {parsed.motivo_text}) — este motivo está pré-aprovado, "
            f"a processar automaticamente. Recebes a senha aqui assim que terminar."
        )

    _notify_admins(request["id"], hostname, parsed.motivo_text, requested_by)
    _write_alert(
        hostname, "info", f"Pedido de reset root de {requested_by} aguarda aprovação",
        {"motivo": parsed.motivo_text, "requested_by": requested_by, "request_id": request["id"]},
    )
    return (
        f"Pedido registado para {hostname} (motivo: {parsed.motivo_text}). "
        f"Aguarda aprovação de um administrador — assim que for aprovado recebes a senha aqui automaticamente."
    )


def format_final_message(request: dict) -> str:
    """Texto mostrado ao utilizador root-only no chat. Propositadamente nunca
    menciona o mecanismo interno (WinRM/RemoteExecutor vs LAPS) — isso é só
    para o administrador (visível em /pedidos e nos logs), o utilizador só
    precisa de saber se recebeu a senha ou não."""
    hostname = request["hostname"]
    if request["status"] == "denied":
        motivo_negado = request.get("error_message") or "sem motivo indicado"
        return f"🚫 **Pedido negado** — {hostname}\nMotivo do administrador: {motivo_negado}"

    if request["status"] == "error":
        return (
            f"⚠️ **Não foi possível concluir o pedido** — {hostname}\n"
            f"Já ficou registado e um administrador vai analisar. Podes tentar pedir de novo mais tarde."
        )

    # status == "ok"
    from rootreset.result_cache import get_result
    result = get_result(request["id"])
    if result is None:
        return (
            f"✅ O teu pedido de root para **{hostname}** foi aprovado, mas a senha já não está "
            f"disponível (expirou). Pede novamente se ainda precisares."
        )

    local_user = os.getenv("ROOT_RESET_LOCAL_USER", "root")
    # Bloco JSON em vez de texto simples: a maioria dos temas de syntax-highlight
    # colore valores string, dando destaque à senha sem os efeitos secundários
    # de um bloco "diff" (marcador "+", e risco de a senha conter um caracter
    # com significado especial em diff/yaml, ex. "#" ou "!" do charset em client.py).
    # json.dumps escapa correctamente qualquer caracter da senha.
    senha_block = json.dumps({"senha": result.get("senha")}, ensure_ascii=False, indent=2)
    return (
        f"✅ **Pedido aprovado — {hostname}**\n\n"
        f"Conta: `{local_user}`\n"
        f"```json\n{senha_block}\n```\n"
        f"⚠️ Esta senha não fica guardada — copia-a agora."
    )
