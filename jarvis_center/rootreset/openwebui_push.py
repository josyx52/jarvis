"""
Entrega automática da senha ao utilizador root-only, sem ele ter de reenviar
a mensagem — chama a própria API do Open WebUI para substituir o conteúdo da
mensagem "aguarda aprovação" já existente no chat dele, o que também emite o
evento de socket que actualiza a UI ao vivo (se o browser ainda tiver o chat
aberto). Sem Redis configurado no Open WebUI, este processo (jarvis_center)
não consegue emitir eventos de socket directamente — por isso passa sempre
pela API HTTP normal do Open WebUI, que já tem o socket em memória.

Autenticação: mesmo mecanismo já usado por chat_engine.py::_mint_webui_session_token
para /api/v1/memories/* — token de SESSÃO do Open WebUI ({"id": user_id, "exp": ...},
HS256, OPENWEBUI_SESSION_SECRET), sem 'jti' de propósito. Implementado aqui via
stdlib (hmac/base64), sem depender de PyJWT — não está confirmado que essa
biblioteca esteja instalada no venv do jarvis_center, e o forward do
X-OpenWebUI-User-Jwt tal-e-qual como Bearer já falhou nos testes anteriores
(chaves diferentes: FORWARD_USER_INFO_HEADER_JWT_SECRET vs WEBUI_SECRET_KEY).

Best-effort: qualquer falha aqui (chat fechado, IDs em falta, rede) não deve
impedir a aprovação em si — o mecanismo de "pull" em service.py (reenviar a
mensagem) continua a funcionar como rede de segurança.
"""

import base64
import hashlib
import hmac
import json
import os
import time

import requests

_SECRET = os.getenv("OPENWEBUI_SESSION_SECRET", "")
# Mesma variável já usada por chat_engine.py para save_memory/recall_memory
# chamarem de volta a API do Open WebUI — evita um segundo nome de env para
# a mesma URL.
_BASE = os.getenv("OPENWEBUI_BASE_URL", "http://127.0.0.1:8006").rstrip("/")
_TIMEOUT = 10


def _mint_session_token(user_id: str, ttl_s: int = 300) -> str | None:
    """Espelha chat_engine.py::_mint_webui_session_token — mesmo formato,
    mesmo segredo (OPENWEBUI_SESSION_SECRET). Mantido como cópia local em
    vez de importar chat_engine.py para evitar puxar as dependências pesadas
    desse módulo (psycopg2/redis) só por causa desta função."""
    if not _SECRET or not user_id:
        return None
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"id": user_id, "exp": int(time.time()) + ttl_s}
    h_b64 = base64.urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode()).rstrip(b"=")
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signing_input = h_b64 + b"." + p_b64
    sig = hmac.new(_SECRET.encode(), signing_input, hashlib.sha256).digest()
    s_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
    return (signing_input + b"." + s_b64).decode()


def push_message(user_id: str | None, chat_id: str | None, message_id: str | None, content: str) -> bool:
    """Tenta substituir o conteúdo da mensagem `message_id` no chat `chat_id`
    pelo texto final. Devolve True só se a API confirmar sucesso (200)."""
    if not (_SECRET and user_id and chat_id and message_id):
        return False

    try:
        token = _mint_session_token(user_id)
        resp = requests.post(
            f"{_BASE}/api/v1/chats/{chat_id}/messages/{message_id}",
            json={"content": content},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=_TIMEOUT,
        )
        return resp.ok
    except Exception:
        return False
