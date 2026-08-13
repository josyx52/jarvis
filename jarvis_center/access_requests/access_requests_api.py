"""
API de tipos de pedido de acesso genéricos (laboratório) — cada tipo é uma
tool Clotho já testada, associada a um alvo/target e opcionalmente a
aprovação humana. Ver access_requests/service.py para a execução.

Montado em /access-requests/* pelo ingestion_api.py principal.
"""

import os
import re
import threading

import psycopg2.errors
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from access_requests.service import run_approval
from access_requests.store import AccessRequestsStore

router = APIRouter(prefix="/access-requests", tags=["access-requests"])

_API_KEY = os.getenv("JARVIS_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _check_api_key(key: str | None = Depends(_api_key_header)):
    if not _API_KEY:
        raise HTTPException(status_code=503, detail="API key not configured on server. Set JARVIS_API_KEY.")
    if key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


_store = AccessRequestsStore()
_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("_", text.strip().lower()).strip("_")[:60]


class CreateType(BaseModel):
    name: str
    description: str | None = None
    integration_name: str
    tool_name: str
    target_param: str = "target"
    result_field: str | None = None
    requires_approval: bool = True
    created_by: str


class CreateRequest(BaseModel):
    target: str
    requested_by: str
    requester_user_id: str | None = None
    chat_id: str | None = None
    message_id: str | None = None


class ApproveRequestBody(BaseModel):
    approved_by: str


class DenyRequestBody(BaseModel):
    approved_by: str
    reason: str | None = None


@router.get("/types", dependencies=[Depends(_check_api_key)])
def list_types():
    return {"types": _store.list_types()}


@router.post("/types", dependencies=[Depends(_check_api_key)])
def create_type(body: CreateType):
    slug = _slugify(body.name)
    if not slug:
        raise HTTPException(status_code=400, detail="Nome inválido para gerar slug.")
    try:
        return _store.create_type(
            slug, body.name, body.description, body.integration_name, body.tool_name,
            body.target_param, body.result_field, body.requires_approval, body.created_by,
        )
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail=f"Já existe um tipo de pedido com o nome '{body.name}'.")


@router.put("/types/{type_id}/active", dependencies=[Depends(_check_api_key)])
def set_type_active(type_id: int, active: bool):
    if not _store.get_type(type_id):
        raise HTTPException(status_code=404, detail="Tipo de pedido não encontrado.")
    _store.set_type_active(type_id, active)
    return {"id": type_id, "active": active}


@router.post("/types/{type_id}/request", dependencies=[Depends(_check_api_key)])
def create_request(type_id: int, body: CreateRequest):
    req_type = _store.get_type(type_id)
    if not req_type:
        raise HTTPException(status_code=404, detail="Tipo de pedido não encontrado.")
    if not req_type["active"]:
        raise HTTPException(status_code=409, detail="Este tipo de pedido está desactivado.")

    request = _store.create_request(
        type_id, body.requested_by, body.target,
        requester_user_id=body.requester_user_id, chat_id=body.chat_id, message_id=body.message_id,
    )

    if not req_type["requires_approval"]:
        _store.mark_processing(request["id"], approved_by="Automático (tipo sem aprovação)")
        threading.Thread(
            target=run_approval,
            args=(request["id"], "Automático (tipo sem aprovação)"),
            daemon=True,
        ).start()
        return {"id": request["id"], "status": "processing"}

    return {"id": request["id"], "status": "pending_approval"}


@router.get("/pending", dependencies=[Depends(_check_api_key)])
def list_pending():
    rows = _store.list_pending()
    return {
        "requests": [
            {
                "id": r["id"],
                "type_name": r["type_name"],
                "type_slug": r["type_slug"],
                "target": r["target"],
                "requested_by": r["requested_by"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]
    }


@router.get("/pending/count", dependencies=[Depends(_check_api_key)])
def pending_count():
    return {"count": _store.count_pending()}


@router.post("/{request_id}/approve", dependencies=[Depends(_check_api_key)])
def approve_request(request_id: int, body: ApproveRequestBody):
    req = _store.get(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    if req["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail="Pedido já não está pendente.")

    _store.mark_processing(request_id, approved_by=body.approved_by)
    threading.Thread(
        target=run_approval,
        args=(request_id, body.approved_by),
        daemon=True,
    ).start()
    return {"id": request_id, "status": "processing"}


@router.post("/{request_id}/deny", dependencies=[Depends(_check_api_key)])
def deny_request(request_id: int, body: DenyRequestBody):
    from access_requests.service import format_final_message
    from rootreset.openwebui_push import push_message

    req = _store.get(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    if req["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail="Pedido já não está pendente.")

    updated = _store.deny(request_id, denied_by=body.approved_by, reason=body.reason)
    req_type = _store.get_type(updated["request_type_id"]) or {"name": "Pedido"}

    delivered = push_message(
        updated.get("requester_user_id"), updated.get("chat_id"), updated.get("message_id"),
        format_final_message(updated, req_type),
    )
    if delivered:
        _store.mark_delivered(request_id)

    return {"id": request_id, "status": updated["status"], "delivered": delivered}
