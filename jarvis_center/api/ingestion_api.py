import gzip
import json
import os
import re
import time
import uuid
from collections import defaultdict

import psycopg2
import psycopg2.extras

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
import uvicorn

from ingestion.frame_router import FrameRouter
from agentless.agentless_api import router as agentless_router
from api.fates_api import router as fates_router
from teams.teams_api import router as teams_router
from teams.teams_admin_api import router as teams_admin_router
from lachesis.lachesis_webhook_api import router as lachesis_webhook_router
from dashboard.dashboard_widgets_api import router as dashboard_widgets_router
from rootreset.rootreset_api import router as rootreset_router
from access_requests.access_requests_api import router as access_requests_router

# --------------------------------
# CONFIG DE SEGURANÇA
# --------------------------------

# API key lida do ambiente — nunca hardcoded
# Se não estiver definida, a API recusa todos os pedidos
_API_KEY = os.getenv("JARVIS_API_KEY", "")

# Limite de tamanho do payload (bytes): 5 MB
_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024

# Rate limiting simples em memória: máx X pedidos por IP por janela de 60s
_RATE_LIMIT_MAX    = int(os.getenv("JARVIS_RATE_LIMIT", "200"))
_RATE_LIMIT_WINDOW = 60  # segundos
_rate_counters: dict[str, list[float]] = defaultdict(list)

# Swagger/docs só activo se JARVIS_DOCS=1
_DOCS_ENABLED = os.getenv("JARVIS_DOCS", "0") == "1"

# --------------------------------
# APP
# --------------------------------

app = FastAPI(
    title="Jarvis Ingestion API",
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)


@app.on_event("startup")
async def _start_teams_poller():
    import logging
    _log = logging.getLogger("jarvis.teams_poller")
    try:
        from teams.teams_poller import start_poller
        start_poller()
        _log.info("Teams Poller iniciado no startup")
    except Exception as e:
        _log.error(f"Falhou ao iniciar Teams Poller: {e}")


@app.on_event("shutdown")
async def _stop_teams_poller():
    try:
        from teams.teams_poller import stop_poller
        stop_poller()
    except Exception:
        pass


@app.on_event("startup")
async def _setup_asyncio_exc_handler():
    import asyncio
    loop = asyncio.get_event_loop()

    def _suppress_win_reset(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(_suppress_win_reset)

# CORS restritivo: só origens explicitamente permitidas
_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("JARVIS_CORS_ORIGINS", "").split(",")
    if o.strip()
]
if not _ALLOWED_ORIGINS:
    _ALLOWED_ORIGINS = []   # nenhuma origem cross-origin permitida por omissão

# CORRIGIDO (2026-08-11): a origem aceite era qualquer hostname (só a porta
# era restringida) — achado de severidade média do security-auditor
# (COORDINATION.md, 2026-07-24), fechado agora por instrução explícita do
# utilizador: só o hostname configurado em JARVIS_WEBUI_HOSTNAME deve
# conseguir chamar o jarvis_center a partir do browser.
_WEBUI_HOSTNAME = os.getenv("JARVIS_WEBUI_HOSTNAME", "jarvis.example.com")
_ALLOWED_ORIGIN_REGEX = rf"^https://{re.escape(_WEBUI_HOSTNAME)}(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_origin_regex=_ALLOWED_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Content-Encoding", "X-API-Key", "X-OpenWebUI-User-Jwt"],
)

PARTITIONS = int(os.getenv("JARVIS_PARTITIONS", "8"))
router = FrameRouter(partitions=PARTITIONS)

app.include_router(agentless_router)
app.include_router(fates_router)
app.include_router(teams_router)
app.include_router(teams_admin_router)
app.include_router(dashboard_widgets_router)
app.include_router(rootreset_router)
app.include_router(access_requests_router)
# Sem dependency de X-API-Key: a autenticação é o próprio token na URL (ver
# lachesis_webhook_api.py) — sistemas externos (Power Automate, Zabbix, etc.)
# não têm a chave interna do Jarvis.
app.include_router(lachesis_webhook_router)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# --------------------------------
# HEALTH (sem autenticação)
# --------------------------------
@app.get("/")
async def health():
    return {
        "status": "ok",
        "service": "jarvis-ingestion-api",
        "timestamp": time.time()
    }


# --------------------------------
# AUTENTICAÇÃO
# --------------------------------
def _check_api_key(key: str | None = Depends(_api_key_header)):
    if not _API_KEY:
        # API key não configurada no servidor — bloquear tudo por segurança
        raise HTTPException(
            status_code=503,
            detail="API key not configured on server. Set JARVIS_API_KEY."
        )
    if key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


# --------------------------------
# RATE LIMITING
# --------------------------------
def _check_rate_limit(request: Request):
    # X-Real-IP definido pelo Nginx; fallback para conexão directa
    ip = request.headers.get("X-Real-IP") or (request.client.host if request.client else "unknown")
    now = time.time()
    window_start = now - _RATE_LIMIT_WINDOW
    calls = _rate_counters[ip]
    # remover entradas fora da janela
    _rate_counters[ip] = [t for t in calls if t > window_start]
    if len(_rate_counters[ip]) >= _RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")
    _rate_counters[ip].append(now)


# --------------------------------
# DECODE BODY (suporta gzip)
# --------------------------------
def decode_body(raw: bytes, headers) -> bytes:
    encoding = headers.get("content-encoding", "")
    if "gzip" in encoding.lower() or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Gzip decode error: {e}")
    return raw


# --------------------------------
# PROCESS PAYLOAD
# --------------------------------
def process_payload(payload):
    if isinstance(payload, dict):
        partition = router.route(payload)
        return {"status": "ok", "mode": "single", "partition": partition}
    elif isinstance(payload, list):
        if len(payload) > 1000:
            raise HTTPException(status_code=400, detail="Batch too large (max 1000 items).")
        result = router.route_many(payload)
        return {"status": "ok", "mode": "batch", "distribution": result}
    return {"status": "invalid_payload"}


# --------------------------------
# CORE HANDLER
# --------------------------------
async def handle_request(request: Request):
    try:
        raw = await request.body()

        if not raw:
            return {"status": "empty_body"}

        if len(raw) > _MAX_PAYLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large (max 5 MB).")

        decoded = decode_body(raw, request.headers)
        payload = json.loads(decoded.decode("utf-8"))
        return process_payload(payload)

    except HTTPException:
        raise
    except Exception as e:
        # não expor detalhes internos ao cliente
        print("[INGEST ERROR]", type(e).__name__, str(e)[:200])
        raise HTTPException(status_code=500, detail="Internal processing error.")


# --------------------------------
# ENDPOINTS (autenticados)
# --------------------------------
@app.post("/ingest")
async def ingest(request: Request, _=Depends(_check_api_key)):
    _check_rate_limit(request)
    return await handle_request(request)


@app.post("/telemetry")
async def telemetry(request: Request, _=Depends(_check_api_key)):
    _check_rate_limit(request)
    return await handle_request(request)


# --------------------------------
# AUTO-UPDATE
# --------------------------------

# Versão actual do agente disponível para deploy
# Actualizar este valor quando houver nova versão
_AGENT_CURRENT_VERSION = os.getenv("JARVIS_AGENT_VERSION", "0.4.0")
_INSTALLER_PATH        = os.getenv("JARVIS_INSTALLER_PATH", "")
_OSQUERY_PATH          = os.getenv("JARVIS_OSQUERY_PATH", "")


@app.get("/agent/version")
async def agent_version():
    """Endpoint público — agents verificam aqui se há nova versão."""
    return {"version": _AGENT_CURRENT_VERSION}


@app.get("/agent/download")
async def agent_download(_=Depends(_check_api_key)):
    """Download do installer — requer API key."""
    from fastapi.responses import FileResponse
    path = _INSTALLER_PATH
    if not path or not os.path.exists(path):
        raise HTTPException(
            status_code=404,
            detail="Installer não disponível. Define JARVIS_INSTALLER_PATH no .env do center."
        )
    return FileResponse(
        path=path,
        media_type="application/octet-stream",
        filename=os.path.basename(path),
    )


@app.get("/osquery/download")
async def osquery_download(_=Depends(_check_api_key)):
    """Download do osquery MSI — requer API key."""
    from fastapi.responses import FileResponse
    path = _OSQUERY_PATH
    if not path or not os.path.exists(path):
        raise HTTPException(
            status_code=404,
            detail="osquery MSI não disponível. Define JARVIS_OSQUERY_PATH no .env do center."
        )
    return FileResponse(
        path=path,
        media_type="application/octet-stream",
        filename=os.path.basename(path),
    )


# --------------------------------
# INSTRUMENTATION POLLING
# --------------------------------

def _get_memory_store():
    """Lazy singleton MemoryStore for instrumentation endpoints."""
    if not hasattr(_get_memory_store, "_store"):
        try:
            from storage.memory_store import MemoryStore
            _get_memory_store._store = MemoryStore()
        except Exception:
            _get_memory_store._store = None
    return _get_memory_store._store


@app.get("/instrumentation/pending")
async def instrumentation_pending(
    host: str,
    _=Depends(_check_api_key),
):
    """Agent polls here for pending AI instrumentation recommendations."""
    store = _get_memory_store()
    if store is None:
        return {"recommendations": []}
    recs = store.get_pending_instrumentation(host)
    return {"host": host, "recommendations": recs}


@app.post("/instrumentation/applied")
async def instrumentation_applied(
    request: Request,
    _=Depends(_check_api_key),
):
    """Agent reports that a recommendation was applied (or failed)."""
    try:
        body   = json.loads((await request.body()).decode())
        rec_id = int(body.get("id", 0))
        status = body.get("status", "applied")
        if rec_id <= 0:
            raise HTTPException(status_code=400, detail="Missing id")
        store = _get_memory_store()
        if store:
            store.mark_instrumentation_applied(rec_id, status)
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# --------------------------------
# SOLUTION DRIVER — agent_commands
# O chat escreve comandos; o agente executa e devolve resultado.
# Modelo idêntico ao Claude Code: IA escreve PS, agente executa.
# --------------------------------

def _cmd_db():
    """Ligação directa ao Postgres para a tabela agent_commands."""
    return psycopg2.connect(
        host     = os.getenv("POSTGRES_HOST",     "localhost"),
        database = os.getenv("POSTGRES_DB",       "jarvis"),
        user     = os.getenv("POSTGRES_USER",     "postgres"),
        password = os.getenv("POSTGRES_PASSWORD", ""),
    )


@app.get("/agent/commands/pending")
async def commands_pending(host: str, _=Depends(_check_api_key)):
    """
    Agent polls this endpoint every 3s.
    Returns all pending commands for this host (ILIKE match).
    """
    conn = None
    try:
        conn = _cmd_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            UPDATE agent_commands
               SET status = 'running'
             WHERE id IN (
               SELECT id FROM agent_commands
                WHERE status = 'pending'
                  AND host ILIKE %s
                  AND created_at > NOW() - INTERVAL '5 minutes'
                ORDER BY id
                LIMIT 5
             )
            RETURNING id, host, script, timeout_s
            """,
            (f"%{host}%",),
        )
        rows = cur.fetchall()
        conn.commit()
        return {"commands": [dict(r) for r in rows]}
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


@app.post("/agent/commands/{cmd_id}/result")
async def commands_result(cmd_id: int, request: Request, _=Depends(_check_api_key)):
    """
    Agent posts result here after executing.
    Body: {stdout, stderr, exit_code}
    """
    conn = None
    try:
        body      = json.loads((await request.body()).decode())
        stdout    = body.get("stdout", "")[:1_000_000]
        stderr    = body.get("stderr", "")[:100_000]
        exit_code = int(body.get("exit_code", -1))
        status    = "done" if exit_code == 0 else "error"

        conn = _cmd_db()
        cur  = conn.cursor()
        cur.execute(
            """
            UPDATE agent_commands
               SET status = %s, stdout = %s, stderr = %s,
                   exit_code = %s, completed_at = NOW()
             WHERE id = %s
            """,
            (status, stdout, stderr, exit_code, cmd_id),
        )
        conn.commit()
        return {"status": "ok"}
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


# ================================
# SOLUTION DRIVER — conversa analista <-> Jarvis
# Analista responde via POST /webhook/reply/{conv_id}
# Jarvis processa e reenvia resposta para o webhook original.
# Sem autenticação (a URL já é secreta por design — UUID por conversa).
# ================================

def _get_conversation_engine():
    """Lazy singleton ConversationEngine."""
    if not hasattr(_get_conversation_engine, "_engine"):
        try:
            from storage.solution_store import SolutionStore
            from engines.conversation_engine import ConversationEngine
            _get_conversation_engine._engine = ConversationEngine(SolutionStore())
        except Exception as e:
            print(f"[API] ConversationEngine indisponível: {e}")
            _get_conversation_engine._engine = None
    return _get_conversation_engine._engine


@app.post("/webhook/reply/{conv_id}")
async def webhook_reply(conv_id: str, request: Request):
    """
    Analista responde a uma notificação do Jarvis.
    Body: {"message": "texto do analista"}
    Jarvis processa em background e envia resposta de volta ao webhook original.
    """
    try:
        body = json.loads((await request.body()).decode())
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido.")

    analyst_message = (body.get("message") or "").strip()
    if not analyst_message:
        raise HTTPException(status_code=400, detail="Campo 'message' obrigatório.")

    engine = _get_conversation_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="ConversationEngine indisponível.")

    # Processar em background — não bloqueia o webhook do analista
    import threading
    threading.Thread(
        target=engine.process_reply,
        args=(conv_id, analyst_message),
        daemon=True,
        name=f"conv-reply-{conv_id[:8]}",
    ).start()

    return {
        "status":          "received",
        "conversation_id": conv_id,
        "message":         "O Jarvis está a analisar a tua mensagem e responderá em breve.",
    }


@app.get("/conversations/{conv_id}")
async def conversation_status(conv_id: str, _=Depends(_check_api_key)):
    """Estado actual de uma conversa — para polling ou debug."""
    try:
        from storage.solution_store import SolutionStore
        store    = SolutionStore()
        conv     = store.get_conversation(conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversa não encontrada.")
        messages = store.get_messages(conv_id)
        return {
            "conversation": {
                "id":         conv["id"],
                "problem_id": conv["problem_id"],
                "host":       conv["host"],
                "status":     conv["status"],
                "risk_level": conv["risk_level"],
                "created_at": str(conv["created_at"]),
                "expires_at": str(conv.get("expires_at") or ""),
            },
            "messages": [
                {
                    "role":       m["role"],
                    "content":    m["content"],
                    "created_at": str(m["created_at"]),
                }
                for m in messages
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================================
# ARTIFACT STORE
# Repositório interno de binários — agentes descarregam daqui em vez de
# sites de terceiros (GitHub, PyPI, etc.).
# Directório configurável via JARVIS_ARTIFACTS_DIR.
# ================================

import hashlib
import pathlib

_ARTIFACTS_DIR = pathlib.Path(
    os.getenv(
        "JARVIS_ARTIFACTS_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "artifacts"),
    )
).resolve()


def _artifact_sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@app.get("/artifacts/")
async def artifacts_list():
    """Lista todos os artefactos disponíveis (sem autenticação)."""
    if not _ARTIFACTS_DIR.is_dir():
        return {"artifacts": []}
    items = []
    for p in sorted(_ARTIFACTS_DIR.iterdir()):
        if p.is_file() and not p.name.startswith("."):
            items.append({
                "name":       p.name,
                "size_bytes": p.stat().st_size,
                "updated_at": p.stat().st_mtime,
            })
    return {"artifacts": items}


@app.get("/artifacts/{name:path}")
async def artifact_download(name: str):
    """Download de um artefacto (sem autenticação — binários públicos)."""
    # Prevenir path traversal
    safe_name = pathlib.Path(name).name
    if not safe_name or safe_name != name or ".." in name:
        raise HTTPException(status_code=400, detail="Nome inválido.")
    path = _ARTIFACTS_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Artefacto '{safe_name}' não encontrado.")
    from fastapi.responses import FileResponse
    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        filename=safe_name,
    )


@app.put("/artifacts/{name:path}")
async def artifact_upload(name: str, request: Request, _=Depends(_check_api_key)):
    """Upload/actualização de um artefacto (requer API key)."""
    safe_name = pathlib.Path(name).name
    if not safe_name or safe_name != name or ".." in name:
        raise HTTPException(status_code=400, detail="Nome inválido.")
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _ARTIFACTS_DIR / safe_name
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Body vazio.")
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(body)
        tmp.rename(path)
    except Exception as e:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(e))
    sha = _artifact_sha256(path)
    return {"status": "ok", "name": safe_name, "size_bytes": len(body), "sha256": sha}


# ================================
# AGENT HEALTH
# O agente reporta a saúde dos seus componentes como um "paciente".
# Falhas geram eventos high/critical que o AlertManager converte em alertas.
# ================================

@app.post("/agent/health")
async def agent_health(request: Request, _=Depends(_check_api_key)):
    """
    Agente envia relatório de saúde dos seus componentes.
    Body: {host, components: {name: {status, detail, ...}}, instrumentation: {...}}
    """
    conn = None
    try:
        body       = json.loads((await request.body()).decode())
        host       = body.get("host", "unknown")
        components = body.get("components", {})
        instr      = body.get("instrumentation", {})
        ts         = time.time()

        conn = _cmd_db()
        conn.autocommit = True
        cur  = conn.cursor()

        rows_written = 0
        for comp_name, comp_data in components.items():
            status = comp_data.get("status", "unknown")
            detail = comp_data.get("detail", "")
            cur.execute(
                """
                INSERT INTO agent_health (host, component, status, detail, payload, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                """,
                (host, comp_name, status, detail, json.dumps(comp_data)),
            )
            rows_written += 1

        # Falhas de instrumentação individuais
        for failed in instr.get("failed", []):
            cur.execute(
                """
                INSERT INTO agent_health (host, component, status, detail, payload, created_at)
                VALUES (%s, 'sitecustomize', 'error', %s, %s, NOW())
                """,
                (
                    host,
                    f"Injecção falhou: pid={failed.get('pid')} name={failed.get('name')}",
                    json.dumps(failed),
                ),
            )
            rows_written += 1

        return {"status": "ok", "rows": rows_written}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


# ================================
# AGENT HEALTH QUERY (para o chat/dashboard)
# ================================

@app.get("/agent/health")
async def agent_health_query(host: str, limit: int = 50, _=Depends(_check_api_key)):
    """Últimas entradas de saúde do agente para um host."""
    conn = None
    try:
        conn = _cmd_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT DISTINCT ON (component)
                   component, status, detail, payload, created_at
            FROM agent_health
            WHERE host ILIKE %s
            ORDER BY component, created_at DESC
            """,
            (f"%{host}%",),
        )
        rows = cur.fetchall()
        conn.commit()
        return {"host": host, "components": [dict(r) for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


# ================================
# INVESTIGATION TRIGGER
# O InvestigationEngine já não corre automaticamente no pipeline (ver
# runtime/central_runtime.py) — só é disparado aqui, sob pedido explícito
# do utilizador a partir de um alerta já persistido (botão "Investigar" na
# página de detalhe da máquina).
# ================================

def _get_investigation_engine():
    """Lazy singleton InvestigationEngine — sem estado além do cliente LLM."""
    if not hasattr(_get_investigation_engine, "_engine"):
        try:
            from engines.investigation_engine import InvestigationEngine
            _get_investigation_engine._engine = InvestigationEngine()
        except Exception as e:
            print(f"[API] InvestigationEngine indisponível: {e}")
            _get_investigation_engine._engine = None
    return _get_investigation_engine._engine


def _fetch_alert(conn, alert_id: int) -> dict | None:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT id, host, severity, title, payload, created_at FROM alerts WHERE id = %s",
        (alert_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_cached_investigation(conn, alert_id: int) -> dict | None:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT id, host, severity, summary, details, alert_id, created_at
        FROM investigations
        WHERE alert_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (alert_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_host_context(conn, host: str, around_ts, window_minutes: int = 5, limit: int = 20) -> dict:
    """Reconstrói o contexto que existia por volta do momento do alerta,
    a partir do que já está persistido — sem re-executar o pipeline."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT snapshot_json FROM snapshots
        WHERE host = %s AND created_at <= %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (host, around_ts),
    )
    row = cur.fetchone()
    snapshot = (row["snapshot_json"] or {}) if row else {}

    def _window_rows(table: str, payload_col: str = "payload"):
        cur.execute(
            f"""
            SELECT {payload_col} FROM {table}
            WHERE host = %s
              AND created_at BETWEEN %s - (%s * INTERVAL '1 minute')
                                  AND %s + (%s * INTERVAL '1 minute')
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (host, around_ts, window_minutes, around_ts, window_minutes, limit),
        )
        return [r[payload_col] for r in cur.fetchall() if r[payload_col]]

    return {
        "snapshot":     snapshot,
        "events":       _window_rows("events"),
        "correlations": _window_rows("correlations"),
        "predictions":  _window_rows("predictions"),
    }


@app.post("/investigations/trigger")
async def trigger_investigation(request: Request, _=Depends(_check_api_key)):
    """
    Body: {"alert_id": <int>}
    Se já existir uma investigação para este alerta, devolve-a (cached=true,
    sem nova chamada LLM). Caso contrário reconstrói o contexto a partir de
    Postgres e invoca o InvestigationEngine.
    """
    conn = None
    try:
        body     = json.loads((await request.body()).decode())
        alert_id = int(body.get("alert_id", 0))
        if alert_id <= 0:
            raise HTTPException(status_code=400, detail="alert_id obrigatório")

        conn = _cmd_db()

        cached = _fetch_cached_investigation(conn, alert_id)
        if cached:
            return {"status": "ok", "cached": True, "investigation": cached}

        alert = _fetch_alert(conn, alert_id)
        if not alert:
            raise HTTPException(status_code=404, detail=f"Alerta {alert_id} não encontrado")

        engine = _get_investigation_engine()
        if engine is None:
            raise HTTPException(status_code=503, detail="InvestigationEngine indisponível")

        host    = alert["host"]
        context = _fetch_host_context(conn, host, alert["created_at"])

        results = engine.investigate(
            [alert["payload"]],
            context["snapshot"],
            context["events"],
            context["correlations"],
            {},
            context["predictions"],
            host=host,
            force=True,
        )

        if not results:
            raise HTTPException(status_code=502, detail="Investigação não produziu resultado")

        store  = _get_memory_store()
        saved  = store.save_investigation(host, results[0], alert_id=alert_id) if store else None

        investigation = {
            "id":         saved["id"] if saved else None,
            "host":       host,
            "severity":   results[0].get("severity"),
            "summary":    results[0].get("summary"),
            "details":    results[0].get("details"),
            "alert_id":   alert_id,
            "created_at": saved["created_at"].isoformat() if saved else None,
        }
        return {"status": "ok", "cached": False, "investigation": investigation}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


# ================================
# AGENTLESS BROKER — métricas e controlo
# ================================

@app.get("/agentless/broker/status")
async def broker_status(_=Depends(_check_api_key)):
    try:
        import sys as _sys
        if os.path.dirname(os.path.dirname(__file__)) not in _sys.path:
            _sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agentless.execution_broker import get_broker
        return get_broker().status()
    except Exception as e:
        return {"error": str(e)}


@app.post("/agentless/broker/beacon/{host}/shutdown")
async def broker_shutdown_beacon(host: str, _=Depends(_check_api_key)):
    try:
        import sys as _sys
        if os.path.dirname(os.path.dirname(__file__)) not in _sys.path:
            _sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agentless.execution_broker import get_broker
        ok = get_broker().shutdown_beacon(host)
        return {"ok": ok, "host": host}
    except Exception as e:
        return {"error": str(e)}


# ================================
# DB PROXY — jarvis-ctl remoto envia queries aqui; center executa e devolve rows
# ================================

@app.post("/db/query")
async def db_query(request: Request, _=Depends(_check_api_key)):
    """Proxy SQL — ctl remoto sem acesso directo à DB envia queries aqui."""
    conn = None
    try:
        body       = json.loads((await request.body()).decode())
        sql        = body.get("sql", "").strip()
        params     = body.get("params") or []
        dict_rows  = bool(body.get("dict_cursor", True))

        if not sql:
            raise HTTPException(status_code=400, detail="sql obrigatório")

        conn = _cmd_db()
        if dict_rows:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()

        cur.execute(sql, params or None)
        conn.commit()

        try:
            rows = cur.fetchall()
            rows = [dict(r) if dict_rows else list(r) for r in rows]
        except psycopg2.ProgrammingError:
            rows = []

        return {"rows": rows, "rowcount": cur.rowcount}

    except HTTPException:
        raise
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


# ================================
# LLM PROXY — jarvis-ctl envia aqui, center chama Foundry
# Permite que máquinas sem acesso directo ao Foundry usem o chat.
# ================================

def _serialize_block(b) -> dict:
    t = getattr(b, "type", None)
    if t == "text":
        return {"type": "text", "text": getattr(b, "text", "")}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    try:
        return b.model_dump()
    except Exception:
        return {"type": str(t)}


@app.post("/llm/chat")
async def llm_chat(request: Request, _=Depends(_check_api_key)):
    """Proxy LLM — ctl remoto envia aqui; center chama Foundry e devolve resultado."""
    try:
        body       = json.loads((await request.body()).decode())
        model      = body.get("model", "claude-sonnet-4-6")
        system     = body.get("system", "")
        messages   = body.get("messages", [])
        tools      = body.get("tools", [])
        max_tokens = int(body.get("max_tokens", 4096))

        from anthropic import AnthropicFoundry
        import asyncio
        client = AnthropicFoundry(
            api_key  = os.getenv("FOUNDRY_API_KEY", ""),
            base_url = os.getenv("FOUNDRY_ENDPOINT", ""),
        )
        kwargs = dict(model=model, system=system, messages=messages, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = tools

        # Executar em thread pool para não bloquear o event loop durante a chamada à Foundry
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: client.messages.create(**kwargs))

        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        tool_calls = [
            {"id": b.id, "name": b.name, "args": b.input}
            for b in resp.content
            if getattr(b, "type", None) == "tool_use"
        ]
        content = [_serialize_block(b) for b in resp.content]

        return {
            "text":          text,
            "tool_calls":    tool_calls,
            "stop_reason":   resp.stop_reason,
            "input_tokens":  resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "content":       content,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================================
# OPENAI-COMPATIBLE API — para OpenWebUI e clientes OpenAI
# Aceita Authorization: Bearer <key> ou X-API-Key
# ================================

def _check_openai_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    key  = auth[7:] if auth.startswith("Bearer ") else request.headers.get("X-API-Key", "")
    if not _API_KEY or key != _API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/v1/models")
async def openai_models(request: Request):
    _check_openai_auth(request)
    return {
        "object": "list",
        "data": [{
            "id":         "claude-sonnet-4-6",
            "object":     "model",
            "created":    1700000000,
            "owned_by":   "jarvis",
            "permission": [],
        }],
    }


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    """
    Loop completo de raciocínio Jarvis — prompt do sistema + ferramentas + execução multi-turno.
    Equivalente ao jarvis-ctl chat mas acessível via OpenAI API para a Web UI.
    """
    _check_openai_auth(request)
    try:
        from api.chat_engine import run_jarvis_loop, run_jarvis_loop_stream, verify_openwebui_jwt
        body       = json.loads((await request.body()).decode())
        messages   = body.get("messages", [])
        do_stream  = bool(body.get("stream", False))
        max_tokens = int(body.get("max_tokens", 4096))

        # JWT do utilizador (X-OpenWebUI-User-Jwt) — usado para dois fins
        # independentes:
        #   1. Controlo de acesso ao Fates Engine (só se OPENWEBUI_JWT_SECRET
        #      estiver configurado, para validar a assinatura).
        #   2. Reencaminhado tal-e-qual para save_memory/recall_memory, que o
        #      usam como Bearer token para chamar de volta a API de memórias
        #      do próprio Open WebUI — este uso não exige OPENWEBUI_JWT_SECRET
        #      aqui, porque é o Open WebUI que valida o token do seu lado.
        user_jwt = request.headers.get("X-OpenWebUI-User-Jwt", "") or None

        has_fates_access = True
        root_only = False
        root_only_requester = None
        root_only_requester_id = None
        jwt_secret = os.getenv("OPENWEBUI_JWT_SECRET", "")
        if jwt_secret:
            has_fates_access = False
            if user_jwt:
                payload = verify_openwebui_jwt(user_jwt, jwt_secret)
                if payload:
                    has_fates_access = payload.get("role") == "admin" or bool(payload.get("fates_engine"))
                    root_only = bool(payload.get("root_only"))
                    root_only_requester = payload.get("email") or payload.get("sub")
                    root_only_requester_id = payload.get("sub")

        # Perfil de chat "root-only" — conta dedicada a pedidos de reset root
        # (ver rootreset/service.py). Determinístico, nunca chama o Claude:
        # zero custo de tokens, incluindo para respostas de erro.
        if root_only:
            from rootreset.service import handle_root_message

            last_user_text = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    content = m.get("content", "")
                    last_user_text = content if isinstance(content, str) else str(content)
                    break

            # chat_id/message_id (se o Open WebUI os reencaminhar) permitem
            # entregar a senha automaticamente ao aprovar, sem o utilizador
            # ter de reenviar a mensagem — ver rootreset/openwebui_push.py.
            reply_text = handle_root_message(
                last_user_text,
                root_only_requester or "desconhecido",
                requester_user_id=root_only_requester_id,
                chat_id=request.headers.get("X-OpenWebUI-Chat-Id") or None,
                message_id=request.headers.get("X-OpenWebUI-Message-Id") or None,
            )

            if do_stream:
                async def _root_only_stream():
                    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                    created = int(time.time())
                    chunk = {
                        "id": cid, "object": "chat.completion.chunk", "created": created,
                        "model": "claude-sonnet-4-6",
                        "choices": [{"index": 0, "delta": {"content": reply_text}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                    stop_chunk = {
                        "id": cid, "object": "chat.completion.chunk", "created": created,
                        "model": "claude-sonnet-4-6",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(stop_chunk)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(_root_only_stream(), media_type="text/event-stream")

            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "claude-sonnet-4-6",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": reply_text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

        if do_stream:
            return StreamingResponse(
                run_jarvis_loop_stream(messages, max_tokens, has_fates_access, user_jwt=user_jwt),
                media_type="text/event-stream",
            )

        # Non-streaming: executar loop e devolver resposta completa
        import asyncio
        loop    = asyncio.get_event_loop()
        text    = await loop.run_in_executor(
            None, lambda: run_jarvis_loop(messages, max_tokens, has_fates_access, user_jwt=user_jwt)
        )
        cid     = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        return {
            "id":      cid,
            "object":  "chat.completion",
            "created": created,
            "model":   "claude-sonnet-4-6",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================================
# EMBEDDINGS (OpenAI-compatible stub)
# ================================

def _word_hash_embedding(text: str, dims: int = 1536) -> list[float]:
    """Bag-of-words hash embedding with L2 normalisation.
    Texts sharing words get higher cosine similarity — good enough for RAG retrieval."""
    import re, math, hashlib
    vec = [0.0] * dims
    words = re.findall(r'\w+', text.lower())
    if words:
        for word in words:
            idx = int(hashlib.md5(word.encode()).hexdigest(), 16) % dims
            vec[idx] += 1.0
    else:
        seed = int(hashlib.sha256(text.encode()).digest().hex(), 16)
        for i in range(dims):
            seed = (seed * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
            vec[i] = ((seed >> 11) * (1.0 / (1 << 53))) * 2 - 1
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


@app.post("/v1/embeddings")
async def create_embeddings(request: Request):
    body = await request.json()
    raw_input = body.get("input", "")
    texts = raw_input if isinstance(raw_input, list) else [raw_input]
    data = [
        {"object": "embedding", "index": i, "embedding": _word_hash_embedding(str(t))}
        for i, t in enumerate(texts)
    ]
    total_tokens = sum(len(str(t).split()) for t in texts)
    return {
        "object": "list",
        "data": data,
        "model": body.get("model", "text-embedding-ada-002"),
        "usage": {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
    }


# ================================
# START
# ================================
def start_api():
    host = os.getenv("JARVIS_API_HOST", "0.0.0.0")
    port = int(os.getenv("JARVIS_API_PORT", "8080"))
    uvicorn.run(
        "api.ingestion_api:app",
        host=host,
        port=port,
        workers=1,
        timeout_keep_alive=120,
    )


if __name__ == "__main__":
    start_api()
