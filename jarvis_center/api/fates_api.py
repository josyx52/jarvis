"""
Jarvis Fates Engine API — esqueleto inicial.

Subsistemas:
- Clotho:    criação/análise de novas integrações (Zabbix, Checkpoint, Palo Alto, etc.)
- Lachesis:  tarefas agendadas, relatórios, comportamentos ensinados (+ Asclepion Systems)
- Atropos:   dashboard de estado de infraestrutura (sem custo de tokens LLM)

Montado em /fates/* pelo ingestion_api.py principal.
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import APIKeyHeader

from clotho.clotho_api import router as clotho_router
from lachesis.lachesis_api import router as lachesis_router
from atropos.atropos_api import router as atropos_router

router = APIRouter(prefix="/fates", tags=["fates"])

_API_KEY = os.getenv("JARVIS_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_user_jwt_header = APIKeyHeader(name="X-OpenWebUI-User-Jwt", auto_error=False)


def _check_api_key(key: str | None = Depends(_api_key_header)):
    if not _API_KEY:
        raise HTTPException(
            status_code=503,
            detail="API key not configured on server. Set JARVIS_API_KEY."
        )
    if key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def _check_fates_access(
    key: str | None = Depends(_api_key_header),
    user_jwt: str | None = Depends(_user_jwt_header),
):
    """Como _check_api_key, mas exige também um X-OpenWebUI-User-Jwt válido com
    role=admin ou fates_engine=true — mesma verificação já usada para as tools
    Fates do chat (ver has_fates_access em ingestion_api.py:1057-1067). O guard
    client-side (fates/+layout.svelte) sozinho era decorativo (achado #4,
    COORDINATION.md 2026-07-24): qualquer X-API-Key válida bastava."""
    _check_api_key(key)

    jwt_secret = os.getenv("OPENWEBUI_JWT_SECRET", "")
    if not jwt_secret:
        # Sem segredo configurado não há como validar assinatura — mesmo
        # fallback aberto que ingestion_api.py já usa neste caso.
        return

    if not user_jwt:
        raise HTTPException(status_code=401, detail="Missing X-OpenWebUI-User-Jwt.")

    from api.chat_engine import verify_openwebui_jwt
    payload = verify_openwebui_jwt(user_jwt, jwt_secret)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired X-OpenWebUI-User-Jwt.")

    has_access = payload.get("role") == "admin" or bool(payload.get("fates_engine"))
    if not has_access:
        raise HTTPException(status_code=403, detail="User lacks Fates Engine access.")


@router.get("/status")
async def status(_=Depends(_check_fates_access)):
    return {
        "engine": "jarvis-fates",
        "subsystems": {
            "clotho": "active",
            "lachesis": "active",
            "asclepion": "active",
            "atropos": "active",
        },
    }


router.include_router(clotho_router, dependencies=[Depends(_check_fates_access)])
router.include_router(lachesis_router, dependencies=[Depends(_check_fates_access)])
router.include_router(atropos_router, dependencies=[Depends(_check_fates_access)])
