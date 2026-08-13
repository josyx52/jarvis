"""
Teams Admin API — endpoints REST para gerir permissões de grupos e ACL de utilizadores.

Rotas (prefixo /teams/admin):
  GET    /status                      — estado do poller
  GET    /chats                       — lista grupos com permissões
  PUT    /chats/{chat_id}/permission  — activar/desactivar grupo
  GET    /chats/sync                  — sincroniza grupos activos do Graph
  GET    /acl                         — lista utilizadores da ACL
  POST   /acl                         — adicionar utilizador à ACL
  PUT    /acl/{upn}/permission        — activar/desactivar utilizador
  DELETE /acl/{upn}                   — remover utilizador da ACL
"""

import json
import os
import time

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

router = APIRouter(prefix="/teams/admin", tags=["teams-admin"])

_API_KEY        = os.getenv("JARVIS_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _check_api_key(key: str | None = Depends(_api_key_header)):
    if not _API_KEY or key != _API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _pg():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        database=os.getenv("POSTGRES_DB", "jarvis"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        connect_timeout=5,
    )


# ── Status ─────────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status(_=Depends(_check_api_key)):
    from teams.teams_poller import poller_status
    return poller_status()


# ── Grupos ─────────────────────────────────────────────────────────────────────

@router.get("/chats")
async def list_chats(_=Depends(_check_api_key)):
    conn = _pg()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT chat_id, chat_type, topic, auto_permitted, admin_permitted,
               members_snapshot, last_seen_at, updated_at
        FROM teams_chat_permissions
        ORDER BY updated_at DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    # Serializar timestamps
    for r in rows:
        for k in ("last_seen_at", "updated_at"):
            if r[k]:
                r[k] = r[k].isoformat()
        if r["members_snapshot"]:
            r["members_snapshot"] = r["members_snapshot"] if isinstance(r["members_snapshot"], list) else []
    return rows


class PermissionUpdate(BaseModel):
    permitted: bool


@router.put("/chats/{chat_id}/permission")
async def set_chat_permission(chat_id: str, body: PermissionUpdate, _=Depends(_check_api_key)):
    conn = _pg()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE teams_chat_permissions
        SET admin_permitted = %s, updated_at = NOW()
        WHERE chat_id = %s
    """, (body.permitted, chat_id))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Chat não encontrado")
    conn.commit()
    conn.close()
    return {"chat_id": chat_id, "admin_permitted": body.permitted}


@router.get("/chats/sync")
async def sync_chats(_=Depends(_check_api_key)):
    """Sincroniza a lista de grupos activos do Microsoft Graph."""
    from teams.teams_poller import _poller, _upsert_group_chat

    token = _poller._get_token()
    if not token:
        raise HTTPException(status_code=503, detail="Não foi possível obter token Graph")

    data = _poller._get(
        "/me/chats",
        params={
            "$top": "50",
            "$filter": "chatType ne 'oneOnOne'",
            "$select": "id,chatType,topic,members",
            "$expand": "members($select=displayName,email)",
        },
    )
    if data is None:
        raise HTTPException(status_code=502, detail="Erro ao chamar Microsoft Graph")

    synced = 0
    for chat in data.get("value", []):
        chat_id  = chat["id"]
        ctype    = chat.get("chatType", "group")
        topic    = chat.get("topic") or ""
        members  = [
            {"name": m.get("displayName", ""), "email": m.get("email", "")}
            for m in chat.get("members", [])
        ]
        _upsert_group_chat(chat_id, topic, ctype, False, members)
        synced += 1

    return {"synced": synced}


# ── ACL ────────────────────────────────────────────────────────────────────────

@router.get("/acl")
async def list_acl(_=Depends(_check_api_key)):
    conn = _pg()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT upn, display_name, permitted, source, updated_at
        FROM teams_acl
        ORDER BY display_name
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        if r["updated_at"]:
            r["updated_at"] = r["updated_at"].isoformat()
    return rows


class AclEntry(BaseModel):
    upn: str
    display_name: str = ""


@router.post("/acl")
async def add_acl(entry: AclEntry, _=Depends(_check_api_key)):
    conn = _pg()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO teams_acl (upn, display_name, permitted, source)
        VALUES (LOWER(%s), %s, TRUE, 'manual')
        ON CONFLICT (upn) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            permitted    = TRUE,
            updated_at   = NOW()
    """, (entry.upn, entry.display_name))
    conn.commit()
    conn.close()
    return {"upn": entry.upn.lower(), "permitted": True}


@router.put("/acl/{upn}/permission")
async def set_acl_permission(upn: str, body: PermissionUpdate, _=Depends(_check_api_key)):
    conn = _pg()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE teams_acl SET permitted = %s, updated_at = NOW()
        WHERE LOWER(upn) = LOWER(%s)
    """, (body.permitted, upn))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Utilizador não encontrado")
    conn.commit()
    conn.close()
    return {"upn": upn.lower(), "permitted": body.permitted}


@router.delete("/acl/{upn}")
async def delete_acl(upn: str, _=Depends(_check_api_key)):
    conn = _pg()
    cur  = conn.cursor()
    cur.execute("DELETE FROM teams_acl WHERE LOWER(upn) = LOWER(%s)", (upn,))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Utilizador não encontrado")
    conn.commit()
    conn.close()
    return {"deleted": upn.lower()}
