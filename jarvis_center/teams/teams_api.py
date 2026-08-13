"""
Teams Bridge API — recebe mensagens do Power Automate e devolve resposta do Jarvis.

Fluxo:
  Power Automate (Teams trigger) → POST /teams/chat → run_jarvis_loop → reply

Histórico de conversa mantido por chat_id no Redis (TTL 4h).
Auth: X-API-Key header (mesmo JARVIS_API_KEY do resto da API).
"""

import json
import os
import time

import redis as _redis_lib
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

router = APIRouter(prefix="/teams", tags=["teams"])

_API_KEY = os.getenv("JARVIS_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_HISTORY_TTL = 4 * 3600   # 4 horas
_HISTORY_MAX = 40          # máx mensagens no histórico (20 turnos)
_HISTORY_PREFIX = "jarvis:teams:history:"

_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            _redis_client = None
    try:
        r = _redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        r.ping()
        _redis_client = r
        return _redis_client
    except Exception:
        return None


def _load_history(chat_id: str) -> list:
    r = _get_redis()
    if r is None:
        return []
    try:
        raw = r.get(f"{_HISTORY_PREFIX}{chat_id}")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return []


def _save_history(chat_id: str, history: list):
    r = _get_redis()
    if r is None:
        return
    try:
        trimmed = history[-_HISTORY_MAX:]
        r.setex(f"{_HISTORY_PREFIX}{chat_id}", _HISTORY_TTL, json.dumps(trimmed))
    except Exception:
        pass


def _check_api_key(key: str | None = Depends(_api_key_header)):
    if not _API_KEY:
        raise HTTPException(status_code=503, detail="JARVIS_API_KEY not configured.")
    if key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


class TeamsChatRequest(BaseModel):
    chat_id: str
    user_id: str = ""
    user_name: str = ""
    message: str


class TeamsChatResponse(BaseModel):
    reply: str
    chat_id: str
    elapsed_s: float


@router.post("/chat", response_model=TeamsChatResponse)
async def teams_chat(req: TeamsChatRequest, _=Depends(_check_api_key)):
    """
    Recebe uma mensagem do Power Automate, processa via Jarvis e devolve a resposta.
    O histórico de conversa é mantido por chat_id no Redis (TTL 4h).
    """
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message is empty.")

    from api.chat_engine import run_jarvis_loop

    history = _load_history(req.chat_id)

    user_prefix = f"[{req.user_name}] " if req.user_name else ""
    history.append({"role": "user", "content": f"{user_prefix}{req.message}"})

    t0 = time.time()
    try:
        reply = run_jarvis_loop(history, max_tokens=4096, has_fates_access=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Jarvis loop error: {e}")
    elapsed = round(time.time() - t0, 2)

    history.append({"role": "assistant", "content": reply})
    _save_history(req.chat_id, history)

    return TeamsChatResponse(reply=reply, chat_id=req.chat_id, elapsed_s=elapsed)


@router.delete("/chat/{chat_id}/history")
async def clear_history(chat_id: str, _=Depends(_check_api_key)):
    """Limpa o histórico de conversa de um chat_id (reset de contexto)."""
    r = _get_redis()
    if r:
        try:
            r.delete(f"{_HISTORY_PREFIX}{chat_id}")
        except Exception:
            pass
    return {"cleared": True, "chat_id": chat_id}
