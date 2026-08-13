"""
Clotho OAuth — gestão de tokens OAuth2 Client Credentials.

Duas variantes:
- Azure AD / Microsoft Entra ID (ex: Microsoft Graph) — via MSAL, autoridade
  fixa em login.microsoftonline.com. Só serve integrações Microsoft.
- Genérica (RFC 6749) — POST directo de client_id/client_secret a um
  "token_url" arbitrário, para qualquer outra API com client_credentials
  (ex: CrowdStrike, Okta, Auth0, ...).

Mantém caches em memória para evitar pedir um novo token a cada pedido.
"""

import threading
import time

import requests

try:
    import msal
except ImportError:
    msal = None

_lock = threading.Lock()
_app_cache: dict = {}

_generic_lock = threading.Lock()
_generic_cache: dict = {}  # (token_url, client_id) -> {"token": str, "expires_at": float}


def _get_app(tenant_id: str, client_id: str, client_secret: str):
    if msal is None:
        raise RuntimeError("msal not installed — run: pip install msal")
    key = (tenant_id, client_id)
    with _lock:
        app = _app_cache.get(key)
        if app is None:
            app = msal.ConfidentialClientApplication(
                client_id,
                authority=f"https://login.microsoftonline.com/{tenant_id}",
                client_credential=client_secret,
            )
            _app_cache[key] = app
        return app


def acquire_token(tenant_id: str, client_id: str, client_secret: str,
                  scope: str = "https://graph.microsoft.com/.default") -> str:
    """Obtém um access token via Client Credentials flow (Application permissions)."""
    app = _get_app(tenant_id, client_id, client_secret)

    result = app.acquire_token_silent(scopes=[scope], account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=[scope])

    if "access_token" in result:
        return result["access_token"]

    error = result.get("error_description") or result.get("error") or "unknown error"
    raise RuntimeError(f"OAuth2 token acquisition failed: {error}")


def acquire_token_ropc(tenant_id: str, client_id: str, client_secret: str,
                       username: str, password: str,
                       scope: str = "https://graph.microsoft.com/.default") -> str:
    """Obtém um access token via ROPC (Resource Owner Password Credentials).

    Activa Delegated permissions (ex: ChatMessage.Send, ChannelMessage.Send)
    que não existem como Application permissions no Microsoft Graph.
    Requer username + password da conta de serviço; incompatível com MFA.
    """
    app = _get_app(tenant_id, client_id, client_secret)

    accounts = app.get_accounts(username=username)
    result = app.acquire_token_silent(scopes=[scope], account=accounts[0]) if accounts else None
    if not result:
        result = app.acquire_token_by_username_password(username, password, scopes=[scope])

    if "access_token" in result:
        return result["access_token"]

    error = result.get("error_description") or result.get("error") or "unknown error"
    raise RuntimeError(f"OAuth2 ROPC token acquisition failed: {error}")


def acquire_token_generic(token_url: str, client_id: str, client_secret: str,
                          scope: str = "", force: bool = False) -> str:
    """Obtém um access token via Client Credentials (RFC 6749) genérico — POST
    directo de client_id/client_secret a um token_url arbitrário, sem MSAL/Azure
    AD. Serve qualquer API que não seja Microsoft Entra (ex: CrowdStrike, Okta).
    """
    key = (token_url, client_id)
    if not force:
        with _generic_lock:
            cached = _generic_cache.get(key)
            if cached and cached["expires_at"] > time.time():
                return cached["token"]

    body = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        body["scope"] = scope

    resp = requests.post(token_url, data=body, timeout=30, verify=False)
    resp.raise_for_status()
    data = resp.json()

    token = data.get("access_token")
    if not token:
        raise RuntimeError("Resposta do token_url não contém 'access_token'.")

    ttl = int(data.get("expires_in") or 3600) - 60
    with _generic_lock:
        _generic_cache[key] = {"token": token, "expires_at": time.time() + max(ttl, 60)}
    return token


def clear_cache(tenant_id: str | None = None, client_id: str | None = None) -> None:
    """Remove entradas da cache (útil quando as credenciais mudam)."""
    with _lock:
        if tenant_id and client_id:
            _app_cache.pop((tenant_id, client_id), None)
        else:
            _app_cache.clear()
