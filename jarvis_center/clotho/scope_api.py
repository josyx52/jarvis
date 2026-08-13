"""
Scope API — gestão dos alvos monitorizados (scope_targets) e das sugestões
de melhoria geradas pelo Explorer Engine (exploration_tips).

Fase 1: CRUD simples, categoria atribuída manualmente (category_source="manual").
A sugestão de categoria por LLM (suggest-category) e o Explorer Engine em si
ficam para fases seguintes.

Montado em /fates/clotho/scope/* por clotho_api.py.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from scope_collector.scope_store import ScopeStore

router = APIRouter(prefix="/scope", tags=["clotho-scope"])

_store = ScopeStore()

_VALID_CATEGORIES = {"server", "application", "database", "network", "security", "business"}
_VALID_TARGET_KINDS = {"zabbix_host", "zabbix_item", "splunk_index", "splunk_search", "agentless_host"}
_VALID_TIP_STATUSES = {"new", "acknowledged", "dismissed", "resolved"}
_VALID_OS_TYPES = {"windows", "linux", "network_device", "unknown"}
_VALID_ANALYSIS_MODES = {"live", "historical"}


class TargetCreateRequest(BaseModel):
    name: str
    target_kind: str
    target_ref: str
    integration_id: int | None = None
    category: str | None = None
    frame_type: str = "metrics"
    os_type: str = "windows"
    agentless_fallback: bool = False
    cadence_seconds: int = 60
    host_key_hint: str | None = None
    created_by: str | None = None
    # live = recolha contínua a partir de agora; historical = análise
    # retrospectiva de um intervalo já existente no Zabbix/Splunk.
    analysis_mode: str = "live"
    history_start: datetime | None = None
    history_end: datetime | None = None


class TargetParseRequest(BaseModel):
    instruction: str
    integration_id: int | None = None


class CategoryUpdateRequest(BaseModel):
    category: str


class EnabledUpdateRequest(BaseModel):
    enabled: bool


class TipStatusUpdateRequest(BaseModel):
    status: str


@router.post("/targets")
def create_target(req: TargetCreateRequest):
    if req.target_kind not in _VALID_TARGET_KINDS:
        raise HTTPException(status_code=400, detail=f"target_kind deve ser um de {sorted(_VALID_TARGET_KINDS)}")
    if req.category and req.category not in _VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category deve ser um de {sorted(_VALID_CATEGORIES)}")
    if req.os_type not in _VALID_OS_TYPES:
        raise HTTPException(status_code=400, detail=f"os_type deve ser um de {sorted(_VALID_OS_TYPES)}")
    if req.analysis_mode not in _VALID_ANALYSIS_MODES:
        raise HTTPException(status_code=400, detail=f"analysis_mode deve ser um de {sorted(_VALID_ANALYSIS_MODES)}")
    if req.analysis_mode == "historical":
        if not req.history_start or not req.history_end:
            raise HTTPException(status_code=400, detail="analysis_mode='historical' requer history_start e history_end")
        if req.history_start >= req.history_end:
            raise HTTPException(status_code=400, detail="history_start tem de ser anterior a history_end")

    return _store.create_target(req.model_dump())


@router.post("/targets/parse")
def parse_target(req: TargetParseRequest):
    """Interpreta a instrução em texto livre (com $nome$ para a integração) e
    devolve os campos estruturados (target_kind/target_ref/categoria/SO) que o
    scope_collector precisa — mesmo padrão do /fates/lachesis/schedule/parse."""
    if not req.instruction.strip():
        raise HTTPException(status_code=400, detail="instruction não pode estar vazia.")

    integration_name = None
    integration_type = None
    if req.integration_id is not None:
        from clotho.clotho_store import ClothoStore
        integration = ClothoStore().get_integration(req.integration_id)
        if not integration:
            raise HTTPException(status_code=404, detail="Integração não encontrada.")
        integration_name = integration.get("name")
        integration_type = integration.get("type")

    from clotho.scope_target_llm import parse_target_instruction

    try:
        return parse_target_instruction(req.instruction.strip(), integration_name, integration_type)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao interpretar a instrução: {e}")


@router.get("/targets")
def list_targets(enabled_only: bool = False):
    return {"targets": _store.list_targets(enabled_only=enabled_only)}


@router.get("/targets/{target_id}")
def get_target(target_id: int):
    target = _store.get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Alvo não encontrado.")
    return target


@router.patch("/targets/{target_id}/category")
def update_category(target_id: int, req: CategoryUpdateRequest):
    if req.category not in _VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category deve ser um de {sorted(_VALID_CATEGORIES)}")
    target = _store.update_category(target_id, req.category, source="manual")
    if not target:
        raise HTTPException(status_code=404, detail="Alvo não encontrado.")
    return target


@router.patch("/targets/{target_id}/enabled")
def update_enabled(target_id: int, req: EnabledUpdateRequest):
    if not _store.get_target(target_id):
        raise HTTPException(status_code=404, detail="Alvo não encontrado.")
    _store.set_enabled(target_id, req.enabled)
    return _store.get_target(target_id)


@router.delete("/targets/{target_id}")
def delete_target(target_id: int):
    if not _store.delete_target(target_id):
        raise HTTPException(status_code=404, detail="Alvo não encontrado.")
    return {"status": "ok"}


@router.post("/targets/{target_id}/explore")
def explore_target_now(target_id: int):
    """Corre o Explorer Engine imediatamente sobre 1 alvo (fora da cadência agendada)."""
    target = _store.get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Alvo não encontrado.")
    if not target.get("category"):
        raise HTTPException(status_code=400, detail="Alvo sem categoria confirmada — define a categoria primeiro.")

    from explorer.state_reader import build_evidence
    from explorer.explorer_engine import run_explorer
    from scope_collector.frame_builder import host_key as compute_host_key

    evidence = build_evidence(compute_host_key(target))
    if not evidence.get("snapshot"):
        raise HTTPException(status_code=409, detail="Ainda sem telemetria suficiente para este alvo.")

    tips = run_explorer(target, evidence, _store)
    _store.record_exploration(target_id)
    return {"target_id": target_id, "tips_generated": len(tips), "tips": tips}


@router.post("/targets/{target_id}/backfill")
def backfill_target(target_id: int):
    """
    Análise retrospectiva de um alvo com analysis_mode='historical' — busca
    a série temporal completa entre history_start/history_end e gera tips
    directamente (não passa pelo BaselineEngine/Predictor, ver historical_engine.py).
    """
    target = _store.get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Alvo não encontrado.")
    if target.get("analysis_mode") != "historical":
        raise HTTPException(status_code=400, detail="Este alvo não está em analysis_mode='historical'.")

    from clotho.clotho_store import ClothoStore
    from explorer.historical_engine import run_historical_backfill

    try:
        tips = run_historical_backfill(target, ClothoStore(), _store)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"target_id": target_id, "tips_generated": len(tips), "tips": tips}


# ---- Tips (Explorer Engine — populado a partir da Fase 2) ----

@router.get("/tips")
def list_tips(category: str | None = None, status: str | None = None,
              scope_target_id: int | None = None, limit: int = 100):
    return {"tips": _store.list_tips(category=category, status=status,
                                      scope_target_id=scope_target_id, limit=limit)}


@router.patch("/tips/{tip_id}/status")
def update_tip_status(tip_id: int, req: TipStatusUpdateRequest):
    if req.status not in _VALID_TIP_STATUSES:
        raise HTTPException(status_code=400, detail=f"status deve ser um de {sorted(_VALID_TIP_STATUSES)}")
    tip = _store.update_tip_status(tip_id, req.status)
    if not tip:
        raise HTTPException(status_code=404, detail="Tip não encontrado.")
    return tip
