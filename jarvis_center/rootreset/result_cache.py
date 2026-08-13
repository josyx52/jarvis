"""
Cache transitório (Redis, com fallback em memória) para o resultado de um
pedido de reset root já aprovado — guarda a senha só até ser entregue ao
utilizador no chat, nunca na tabela root_reset_requests.

Mesmo padrão de chat_engine.py::_store_pending_action / _pop_pending_action.
"""

import json
import os
import threading
from collections import OrderedDict

import redis as _redis_lib

_PREFIX = "jarvis:rootreset:result:"
_TTL = 60 * 60 * 24  # 24h — janela generosa para o utilizador reenviar o comando

_redis_client = None
_redis_ok = True
_redis_lock = threading.Lock()

_MEMORY: "OrderedDict[str, dict]" = OrderedDict()
_MEMORY_LOCK = threading.Lock()
_MEMORY_MAX = 200


def _get_redis():
    global _redis_client, _redis_ok
    if _redis_ok and _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            _redis_ok = False
            _redis_client = None
    with _redis_lock:
        if _redis_ok and _redis_client is not None:
            return _redis_client
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
            _redis_ok = True
        except Exception:
            _redis_client = None
            _redis_ok = False
    return _redis_client


def store_result(request_id: int, result: dict) -> None:
    key = f"{_PREFIX}{request_id}"
    r = _get_redis()
    if r is not None:
        try:
            r.setex(key, _TTL, json.dumps(result))
            return
        except Exception:
            pass
    with _MEMORY_LOCK:
        _MEMORY[key] = result
        while len(_MEMORY) > _MEMORY_MAX:
            _MEMORY.popitem(last=False)


def get_result(request_id: int) -> dict | None:
    key = f"{_PREFIX}{request_id}"
    r = _get_redis()
    if r is not None:
        try:
            raw = r.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception:
            pass
    with _MEMORY_LOCK:
        return _MEMORY.get(key)
