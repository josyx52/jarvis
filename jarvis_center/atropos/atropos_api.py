"""
Atropos API — dashboard de estado de infraestrutura (hosts, alertas de
segurança, atividade Lachesis), sem custo de tokens LLM.

Montado em /fates/atropos/* pelo fates_api.py.
"""

import asyncio

from fastapi import APIRouter

from atropos.atropos_store import AtroposStore

router = APIRouter(prefix="/atropos", tags=["atropos"])

_store = AtroposStore()


@router.get("/summary")
async def summary():
    return await asyncio.to_thread(_store.summary)


@router.get("/hosts")
async def hosts():
    return {"hosts": await asyncio.to_thread(_store.list_hosts)}


@router.get("/activity")
async def activity(limit: int = 50):
    return await asyncio.to_thread(_store.activity, limit)


# ── Detalhe de host (página de detalhe da máquina) ─────────────────────────

@router.get("/hosts/{host}/alerts")
async def host_alerts(host: str, limit: int = 50):
    return {"alerts": await asyncio.to_thread(_store.host_alerts, host, limit)}


@router.get("/hosts/{host}/predictions")
async def host_predictions(host: str, limit: int = 50):
    return {"predictions": await asyncio.to_thread(_store.host_predictions, host, limit)}


@router.get("/hosts/{host}/investigations")
async def host_investigations(host: str, limit: int = 50):
    return {"investigations": await asyncio.to_thread(_store.host_investigations, host, limit)}


@router.get("/hosts/{host}/problems")
async def host_problems(host: str, limit: int = 50):
    return {"problems": await asyncio.to_thread(_store.host_problems, host, limit)}
