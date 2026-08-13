"""
Agentless Operations API — endpoints FastAPI para investigações sem agente.

Montado em /agentless/* pelo ingestion_api.py principal.
"""

import os
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, field_validator

from agentless.investigators.security_investigator import SecurityInvestigator
from agentless.agentless_store import AgentlessStore
from agentless.agentless_chat import AgentlessChat

router = APIRouter(prefix="/agentless", tags=["agentless"])

_store = AgentlessStore()
_chat  = AgentlessChat()

# CRÍTICO — encontrado por auditoria de segurança ao vivo (2026-07-24, ver
# COORDINATION.md): estes endpoints executavam PowerShell/consultavam dados
# de investigação SEM QUALQUER autenticação, e o processo escuta em 0.0.0.0
# (não só localhost) — qualquer host da rede que alcançasse a porta 8080
# conseguia correr `agentless_execute` sem credenciais nenhumas. Réplica
# local do mesmo mecanismo de `ingestion_api._check_api_key` (não se importa
# directamente de lá para evitar import circular — ingestion_api já importa
# este módulo no arranque).
_API_KEY = os.getenv("JARVIS_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _check_api_key(key: str | None = Depends(_api_key_header)):
    if not _API_KEY:
        raise HTTPException(
            status_code=503,
            detail="API key not configured on server. Set JARVIS_API_KEY.",
        )
    if key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


# ---- Modelos de entrada ----

class SecurityInvestigateRequest(BaseModel):
    machine:         str
    username:        str
    reason:          str
    access_method:   str          # "laps" ou "password_reset"
    credential_time: str          # ISO 8601 — hora em que a credencial foi gerada
    machine_type:    str = "workstation"  # "workstation" ou "server"

    @field_validator("access_method")
    @classmethod
    def validate_access_method(cls, v):
        if v not in ("laps", "password_reset"):
            raise ValueError("access_method deve ser 'laps' ou 'password_reset'")
        return v

    @field_validator("machine_type")
    @classmethod
    def validate_machine_type(cls, v):
        if v not in ("workstation", "server"):
            raise ValueError("machine_type deve ser 'workstation' ou 'server'")
        return v

    @field_validator("credential_time")
    @classmethod
    def validate_credential_time(cls, v):
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("credential_time deve ser ISO 8601 (ex: 2026-06-01T14:30:00)")
        return v


# ---- Background worker ----

def _run_security_investigation(
    investigation_id: str,
    req: SecurityInvestigateRequest,
):
    """Corre em thread separada — a API responde imediatamente com o ID."""
    try:
        cred_time = datetime.fromisoformat(
            req.credential_time.replace("Z", "+00:00")
        )
        if cred_time.tzinfo is None:
            cred_time = cred_time.replace(tzinfo=timezone.utc)

        investigator = SecurityInvestigator()
        result = investigator.investigate(
            machine         = req.machine,
            username        = req.username,
            reason          = req.reason,
            access_method   = req.access_method,
            credential_time = cred_time,
            machine_type    = req.machine_type,
        )
        _store.save_security_investigation(result)
    except Exception as e:
        _store.save_security_investigation({
            "investigation_id": investigation_id,
            "machine":          req.machine,
            "username":         req.username,
            "credential_time":  req.credential_time,
            "access_method":    req.access_method,
            "stated_reason":    req.reason,
            "verdict":          "ERRO",
            "report":           f"Erro interno na investigação: {e}",
            "evidence_summary": {},
            "error":            str(e),
            "investigated_at":  datetime.now(timezone.utc).isoformat(),
        })


# ---- Endpoints ----

@router.post("/security/investigate", dependencies=[Depends(_check_api_key)])
def security_investigate(req: SecurityInvestigateRequest):
    """
    Inicia uma investigação de acesso administrativo numa máquina sem agente.

    Responde imediatamente com o investigation_id.
    O relatório fica disponível via GET /agentless/security/{investigation_id}.
    """
    cred_time = datetime.fromisoformat(
        req.credential_time.replace("Z", "+00:00")
    )
    if cred_time.tzinfo is None:
        cred_time = cred_time.replace(tzinfo=timezone.utc)

    from agentless.investigators.security_investigator import SecurityInvestigator as _SI
    inv_id = _SI()._make_id(req.machine, req.username, cred_time)

    # Verificar se já existe
    existing = _store.get_security_investigation(inv_id)
    if existing:
        return {
            "investigation_id": inv_id,
            "status":           "already_exists",
            "verdict":          existing.get("verdict"),
            "message":          "Investigação já realizada. Use GET para obter o relatório.",
        }

    # Lançar em background
    threading.Thread(
        target  = _run_security_investigation,
        args    = (inv_id, req),
        daemon  = True,
        name    = f"agentless-sec-{inv_id[:20]}",
    ).start()

    return {
        "investigation_id": inv_id,
        "status":           "running",
        "message":          "Investigação iniciada. Use GET /agentless/security/{investigation_id} para obter o relatório.",
    }


@router.get("/security/{investigation_id}", dependencies=[Depends(_check_api_key)])
def get_security_investigation(investigation_id: str):
    """Obtém o resultado de uma investigação de segurança pelo ID."""
    result = _store.get_security_investigation(investigation_id)
    if not result:
        raise HTTPException(status_code=404, detail="Investigação não encontrada.")

    # Serializar campos datetime para string
    for key in ("credential_time", "investigated_at"):
        if result.get(key) and not isinstance(result[key], str):
            result[key] = str(result[key])

    return result


@router.get("/security", dependencies=[Depends(_check_api_key)])
def list_security_investigations(
    machine:  str | None = None,
    username: str | None = None,
    limit:    int        = 50,
):
    """Lista investigações de segurança com filtros opcionais."""
    results = _store.list_security_investigations(
        machine  = machine,
        username = username,
        limit    = min(limit, 200),
    )
    for r in results:
        for key in ("credential_time", "investigated_at"):
            if r.get(key) and not isinstance(r[key], str):
                r[key] = str(r[key])
    return {"investigations": results, "total": len(results)}


# ---- Chat agentless ----

class ChatRequest(BaseModel):
    machine:      str
    question:     str
    machine_type: str = "workstation"

    @field_validator("machine_type")
    @classmethod
    def validate_machine_type(cls, v):
        if v not in ("workstation", "server"):
            raise ValueError("machine_type deve ser 'workstation' ou 'server'")
        return v

    @field_validator("question")
    @classmethod
    def validate_question(cls, v):
        if not v or len(v.strip()) < 3:
            raise ValueError("question deve ter pelo menos 3 caracteres")
        if len(v) > 500:
            raise ValueError("question não pode exceder 500 caracteres")
        return v.strip()


class ExecuteRequest(BaseModel):
    machine:      str
    script:       str
    machine_type: str = "workstation"
    timeout_s:    int = 60

    @field_validator("machine_type")
    @classmethod
    def validate_machine_type(cls, v):
        if v not in ("workstation", "server"):
            raise ValueError("machine_type deve ser 'workstation' ou 'server'")
        return v


@router.post("/execute", dependencies=[Depends(_check_api_key)])
def agentless_execute(req: ExecuteRequest):
    """
    Executa um script PowerShell numa máquina sem agente via WinRM.
    Equivalente ao agent_run mas sem necessidade de agente instalado.
    """
    from agentless.remote_executor import RemoteExecutor, RemoteExecutorError
    try:
        executor = RemoteExecutor(host=req.machine, machine_type=req.machine_type)
        result = executor.run(req.script, timeout=req.timeout_s)
        return {
            "machine":   req.machine,
            "exit_code": result["exit_code"],
            "stdout":    result["stdout"],
            "stderr":    result["stderr"],
            "ok":        result["ok"],
            "warning":   result.get("warning"),
        }
    except RemoteExecutorError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat", dependencies=[Depends(_check_api_key)])
def agentless_chat(req: ChatRequest):
    """
    Responde a uma pergunta em linguagem natural sobre uma máquina sem agente.
    Claude gera o PowerShell, executa e interpreta o resultado.
    """
    result = _chat.ask(
        machine      = req.machine,
        question     = req.question,
        machine_type = req.machine_type,
    )
    if not result["ok"]:
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@router.get("/chat/ping", dependencies=[Depends(_check_api_key)])
def agentless_ping(machine: str, machine_type: str = "workstation"):
    """Verifica se uma máquina está acessível via WinRM."""
    from agentless.remote_executor import RemoteExecutor, RemoteExecutorError
    try:
        executor = RemoteExecutor(host=machine, machine_type=machine_type)
        ok = executor.ping()
        return {"machine": machine, "reachable": ok, "error": None}
    except RemoteExecutorError as e:
        return {"machine": machine, "reachable": False, "error": str(e)}
    except Exception as e:
        return {"machine": machine, "reachable": False, "error": str(e)}


# ---- Beacon polling (outbound, igual ao /agent/commands/pending do agent real) ----
#
# Estes endpoints são chamados pelo mini-agente PowerShell lançado via WinRM
# bootstrap — nunca pelo browser/operador. O mini-agente faz polling de saída
# (GET /next) e devolve resultados (POST /result), exactamente como o agente
# permanente faz com /agent/commands/pending e /agent/commands/{id}/result.
# Não há conexão de entrada na máquina remota em momento nenhum.

class BeaconResultPayload(BaseModel):
    job_id: str
    ok: bool = False
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""


def _check_beacon_key(beacon_id: str, request_key: str | None):
    from agentless.execution_broker import get_broker
    broker = get_broker()
    beacon = broker.registry.get_by_id(beacon_id)
    if beacon is None or request_key != beacon.api_key:
        raise HTTPException(status_code=403, detail="Beacon inválido ou expirado.")
    return broker


@router.get("/beacon/{beacon_id}/next")
def beacon_next(beacon_id: str, request: Request):
    """Polling de saída do mini-agente — devolve o próximo job pendente."""
    api_key = request.headers.get("X-Jarvis-Key", "")
    from agentless.execution_broker import get_broker
    broker = get_broker()
    job = broker.beacon_poll_next(beacon_id, api_key)
    if job is None:
        return {}
    return job


@router.post("/beacon/{beacon_id}/result")
def beacon_result(beacon_id: str, payload: BeaconResultPayload, request: Request):
    """O mini-agente devolve aqui o resultado da execução de um job."""
    api_key = request.headers.get("X-Jarvis-Key", "")
    from agentless.execution_broker import get_broker
    broker = get_broker()
    ok = broker.beacon_submit_result(beacon_id, api_key, payload.model_dump())
    if not ok:
        raise HTTPException(status_code=403, detail="Beacon inválido ou expirado.")
    return {"ok": True}
