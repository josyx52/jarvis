"""
Clotho Session Auth — gestão de tokens de sessão para integrações cujo modelo de
autenticação é "login por sessão" (ex: Check Point Management API: POST /login
devolve um "sid" que deve ser enviado num cabeçalho em cada pedido seguinte,
expirando por inactividade).

Genérico — não específico de nenhum vendor. Mantém uma cache em memória por
integration_id para evitar um novo login a cada pedido.
"""

import threading
import time

import requests

_lock = threading.Lock()
_cache: dict = {}  # integration_id -> {"token": str, "expires_at": float}


def _extract_field(data, path: str):
    """Resolve um caminho tipo 'sid' ou 'data.sid' num dict de resposta JSON."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def acquire_session_token(integration_id, login_url: str, login_body: dict,
                          session_field: str = "sid", ttl_seconds: int = 540,
                          method: str = "POST", force: bool = False) -> str:
    """Devolve um token de sessão válido, reutilizando a cache quando possível."""
    if not force:
        with _lock:
            cached = _cache.get(integration_id)
            if cached and cached["expires_at"] > time.time():
                return cached["token"]

    if not login_url:
        raise RuntimeError("Login URL não configurado.")

    resp = requests.request(
        method, login_url, json=login_body, timeout=30, verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    token = _extract_field(data, session_field)
    if not token:
        raise RuntimeError(f"Campo de sessão '{session_field}' não encontrado na resposta de login.")

    with _lock:
        _cache[integration_id] = {"token": token, "expires_at": time.time() + ttl_seconds}
    return token


def clear_cache(integration_id=None) -> None:
    """Remove entradas da cache (útil após uma sessão expirar no servidor)."""
    with _lock:
        if integration_id is not None:
            _cache.pop(integration_id, None)
        else:
            _cache.clear()