"""
API de aprovação de pedidos de reset root — consumida pela página
JarvisWeb /pedidos (separador "Pedidos", card "Workstations").

Montado em /rootreset/* pelo ingestion_api.py principal.
"""

import os
import threading

import psycopg2
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from rootreset.openwebui_push import push_message
from rootreset.reasons import REASONS, _slugify, all_reasons, display_text
from rootreset.service import format_final_message, run_approval
from rootreset.store import RootResetStore

router = APIRouter(prefix="/rootreset", tags=["rootreset"])

_API_KEY = os.getenv("JARVIS_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _check_api_key(key: str | None = Depends(_api_key_header)):
    if not _API_KEY:
        raise HTTPException(status_code=503, detail="API key not configured on server. Set JARVIS_API_KEY.")
    if key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


_store = RootResetStore()


class ApproveRequest(BaseModel):
    approved_by: str


class DenyRequest(BaseModel):
    approved_by: str
    reason: str | None = None


class AutoApproveSet(BaseModel):
    motivo_slug: str
    motivo_text: str | None = None
    updated_by: str


class CustomReasonCreate(BaseModel):
    title: str
    created_by: str


@router.get("/reasons", dependencies=[Depends(_check_api_key)])
def list_reasons():
    """Lista de motivos oficiais + motivos custom criados pelo admin (Fates →
    Clotho → "root_motivos"), para o autocomplete "$" do chat root-only (não
    é uma restrição — motivos fora desta lista continuam a ser aceites, ver
    rootreset/parser.py)."""
    return {"reasons": [{"slug": slug, "title": title} for slug, title in all_reasons().items()]}


@router.post("/custom-reasons", dependencies=[Depends(_check_api_key)])
def create_custom_reason(body: CustomReasonCreate):
    slug = _slugify(body.title)
    if not slug:
        raise HTTPException(status_code=400, detail="Título inválido para gerar slug.")
    if slug in REASONS:
        raise HTTPException(status_code=409, detail="Já existe um motivo oficial com este nome.")

    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"), database=os.getenv("POSTGRES_DB", "jarvis"),
        user=os.getenv("POSTGRES_USER", "postgres"), password=os.getenv("POSTGRES_PASSWORD", ""),
    )
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO root_reset_custom_reasons (slug, title, created_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET title = EXCLUDED.title
            """,
            (slug, body.title, body.created_by),
        )
    finally:
        conn.close()
    return {"slug": slug, "title": body.title}


@router.get("/pending", dependencies=[Depends(_check_api_key)])
def list_pending():
    rows = _store.list_pending()
    return {
        "requests": [
            {
                "id": r["id"],
                "hostname": r["hostname"],
                "motivo_text": r["motivo_text"],
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
def approve_request(request_id: int, body: ApproveRequest):
    req = _store.get(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    if req["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail="Pedido já não está pendente.")

    _store.mark_processing(request_id, approved_by=body.approved_by)

    # WinRM (+ fallback LAPS) pode demorar minutos — não bloquear o pedido
    # HTTP do admin. O utilizador que pediu o root recebe a resposta sozinho
    # no chat quando run_approval terminar (push_message), sem o admin
    # precisar de esperar nem de fazer nada mais. Mesma função usada pela
    # auto-aprovação (rootreset/service.py::handle_root_message).
    threading.Thread(
        target=run_approval,
        args=(request_id, req["hostname"], body.approved_by),
        daemon=True,
    ).start()

    return {"id": request_id, "status": "processing"}


@router.post("/{request_id}/deny", dependencies=[Depends(_check_api_key)])
def deny_request(request_id: int, body: DenyRequest):
    req = _store.get(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    if req["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail="Pedido já não está pendente.")

    updated = _store.deny(request_id, denied_by=body.approved_by, reason=body.reason)

    delivered = push_message(
        updated.get("requester_user_id"), updated.get("chat_id"), updated.get("message_id"),
        format_final_message(updated),
    )
    if delivered:
        _store.mark_delivered(request_id)

    return {"id": request_id, "status": updated["status"], "delivered": delivered}


# ── Gestão de motivos auto-aprovados (admin) ────────────────────────────────

@router.get("/auto-approve", dependencies=[Depends(_check_api_key)])
def list_auto_approve():
    """Todos os motivos possíveis (whitelist oficial + custom_* já usados
    em pedidos reais), marcados com se estão ou não em auto-aprovação —
    para a secção "Motivos automáticos" em /pedidos."""
    active = {row["motivo_slug"]: row for row in _store.list_auto_approved()}
    seen_slugs = set(active.keys())

    reasons = all_reasons()
    catalogo = [
        {"motivo_slug": slug, "motivo_text": title, "auto_approved": slug in active}
        for slug, title in reasons.items()
    ]
    seen_slugs |= set(reasons.keys())

    for row in _store.distinct_motivos_used():
        slug = row["motivo_slug"]
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        catalogo.append({
            "motivo_slug": slug,
            "motivo_text": row.get("motivo_text") or display_text(slug),
            "auto_approved": slug in active,
        })

    return {"motivos": catalogo}


@router.post("/auto-approve", dependencies=[Depends(_check_api_key)])
def enable_auto_approve(body: AutoApproveSet):
    _store.set_auto_approved(body.motivo_slug, body.motivo_text, body.updated_by)
    return {"motivo_slug": body.motivo_slug, "auto_approved": True}


@router.delete("/auto-approve/{motivo_slug}", dependencies=[Depends(_check_api_key)])
def disable_auto_approve(motivo_slug: str):
    _store.unset_auto_approved(motivo_slug)
    return {"motivo_slug": motivo_slug, "auto_approved": False}


# ── Card "Auditoria de Root" (agregação por máquina, Início) ───────────────

_ALERT_VERDICTS = {"NÃO CONFORME", "ERRO"}   # vermelho
_SUSPECT_VERDICTS = {"SUSPEITO"}             # amarelo
_OK_VERDICTS = {"CONFORME"}                  # verde


def _alert_color(verdict: str | None) -> str:
    if verdict in _ALERT_VERDICTS:
        return "red"
    if verdict in _SUSPECT_VERDICTS:
        return "yellow"
    if verdict in _OK_VERDICTS:
        return "green"
    return "gray"  # sem investigação ainda, ou INDETERMINADO


def _machine_summary(row: dict) -> dict:
    color = _alert_color(row.get("verdict"))
    investigation_id = row.get("investigation_id")
    seen = investigation_id is None or row.get("last_seen_investigation_id") == investigation_id
    return {
        "hostname": row["hostname"],
        "total_requests": row["total_requests"],
        "last_request_at": str(row["last_request_at"]) if row.get("last_request_at") else None,
        "pending_investigation_count": row["pending_investigation_count"],
        "verdict": row.get("verdict"),
        "alert_color": color,
        "has_alert": color in ("red", "yellow"),
        "seen": seen,
    }


@router.get("/machines", dependencies=[Depends(_check_api_key)])
def list_machines():
    return {"machines": [_machine_summary(r) for r in _store.list_machines()]}


@router.get("/machines/alerts/count", dependencies=[Depends(_check_api_key)])
def machines_alert_count():
    summaries = [_machine_summary(r) for r in _store.list_machines()]
    count = sum(1 for m in summaries if m["has_alert"] and not m["seen"])
    return {"count": count}


@router.get("/machines/{hostname}", dependencies=[Depends(_check_api_key)])
def machine_detail(hostname: str, date: str | None = None):
    rows = _store.list_requests_for_machine(hostname, on_date=date)
    return {
        "hostname": hostname,
        "requests": [
            {
                "id": r["id"],
                "motivo_text": r["motivo_text"],
                "requested_by": r["requested_by"],
                "status": r["status"],
                "mechanism": r["mechanism"],
                "error_message": r["error_message"],
                "created_at": str(r["created_at"]),
                "approved_at": str(r["approved_at"]) if r["approved_at"] else None,
                "investigated_at": str(r["investigated_at"]) if r["investigated_at"] else None,
                "investigation_id": r["investigation_id"],
                "verdict": r.get("verdict"),
                "alert_color": _alert_color(r.get("verdict")) if r["investigated_at"] else None,
                "can_investigate_now": r["status"] == "ok" and r["investigated_at"] is None,
            }
            for r in rows
        ],
    }


class AckAlert(BaseModel):
    acknowledged_by: str


@router.post("/machines/{hostname}/ack", dependencies=[Depends(_check_api_key)])
def ack_machine(hostname: str, body: AckAlert):
    row = next((r for r in _store.list_machines() if r["hostname"].upper() == hostname.upper()), None)
    investigation_id = row.get("investigation_id") if row else None
    _store.ack_machine_alert(hostname, investigation_id, body.acknowledged_by)
    return {"hostname": hostname, "acknowledged": investigation_id is not None}


@router.post("/{request_id}/investigate-now", dependencies=[Depends(_check_api_key)])
def investigate_now(request_id: int):
    req = _store.get(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    if req["status"] != "ok":
        raise HTTPException(status_code=409, detail="Só é possível investigar pedidos aprovados com sucesso.")
    if req["investigated_at"] is not None:
        raise HTTPException(status_code=409, detail="Este pedido já foi investigado.")

    def _run():
        from rootreset.scheduler import investigate_request
        try:
            investigate_request(req, _store)
        except Exception as e:
            print(f"[ROOTRESET] investigar-agora falhou para pedido {request_id}: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"id": request_id, "status": "investigating"}


@router.get("/{request_id}/investigation", dependencies=[Depends(_check_api_key)])
def get_investigation(request_id: int):
    inv = _store.get_investigation_for_request(request_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Sem investigação para este pedido.")
    inv = dict(inv)
    for k in ("credential_time", "investigated_at"):
        if inv.get(k):
            inv[k] = str(inv[k])
    return inv
