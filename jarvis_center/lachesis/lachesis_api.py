"""
Lachesis API — tarefas agendadas, relatórios, comportamentos ensinados e
Asclepion Systems.

Montado em /fates/lachesis/* pelo fates_api.py.
"""

import asyncio
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from lachesis.lachesis_store import LachesisStore
from lachesis.lachesis_schedule import describe_schedule
from lachesis.lachesis_tasks_llm import parse_schedule_description
from lachesis.lachesis_flow_llm import compile_instruction_to_flow, summarize_flow_to_text
from lachesis.lachesis_flow_executor import run_flow
from lachesis.lachesis_export import render_task_run, render_asclepion_run
from lachesis import lachesis_scheduler
from lachesis import asclepion_engine

router = APIRouter(prefix="/lachesis", tags=["lachesis"])

_store = LachesisStore()


# ── Models ──────────────────────────────────────────────────────────────────

class TaskCreateRequest(BaseModel):
    name: str
    instruction: str
    task_type: Literal["integration", "agentless", "database"] = "agentless"
    schedule: dict
    webhook_id: int | None = None
    enabled: bool = True
    created_by: str | None = None
    trigger_type: Literal["schedule", "event", "webhook_in", "manual"] = "schedule"
    trigger_config: dict | None = None
    flow_definition: dict | None = None


class TaskUpdateRequest(BaseModel):
    name: str | None = None
    instruction: str | None = None
    task_type: Literal["integration", "agentless", "database"] | None = None
    schedule: dict | None = None
    webhook_id: int | None = None
    enabled: bool | None = None
    trigger_type: Literal["schedule", "event", "webhook_in", "manual"] | None = None
    trigger_config: dict | None = None
    flow_definition: dict | None = None


class ScheduleParseRequest(BaseModel):
    description: str


class FlowCompileRequest(BaseModel):
    instruction: str
    task_type: Literal["integration", "agentless", "database"] = "agentless"
    sample_payload: dict | None = None


class FlowSummarizeRequest(BaseModel):
    flow_definition: dict


class FlowTestRunRequest(BaseModel):
    flow_definition: dict


class BehaviorCreateRequest(BaseModel):
    title: str
    instruction: str
    active: bool = True
    created_by: str | None = None
    kind: Literal["behavior", "infra_fact"] = "behavior"
    category: Literal["edr_xdr", "firewall", "siem_agent", "workstation_baseline", "application", "infra_general"] | None = None
    version: str | None = None
    source: str = "manual"
    evidence: str | None = None
    scope_type: Literal["global", "host", "target"] = "global"
    scope_value: str | None = None


class BehaviorUpdateRequest(BaseModel):
    title: str | None = None
    instruction: str | None = None
    active: bool | None = None
    category: Literal["edr_xdr", "firewall", "siem_agent", "workstation_baseline", "application", "infra_general"] | None = None
    version: str | None = None
    evidence: str | None = None
    scope_type: Literal["global", "host", "target"] | None = None
    scope_value: str | None = None


class ProfileCreateRequest(BaseModel):
    name: str
    targets: list[str]
    os_type: str = "windows"
    machine_type: str = "workstation"
    benchmark: str
    created_by: str | None = None


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    targets: list[str] | None = None
    os_type: str | None = None
    machine_type: str | None = None
    benchmark: str | None = None


class ProfileRunRequest(BaseModel):
    target: str | None = None


# URL pela qual sistemas externos (Zabbix, Power Automate, etc.) alcançam este
# Center via outbound — nunca localhost/JARVIS_CENTER_URL (esse é para uso interno).
# Ver comentário de BROKER_CENTER_URL no .env: nginx porta 8443 -> 127.0.0.1:8080.
_PUBLIC_CENTER_URL = os.getenv("BROKER_CENTER_URL", "http://localhost:8000")


def _task_view(task: dict) -> dict:
    task = dict(task)
    task["schedule_summary"] = describe_schedule(task["schedule"])
    if task.get("trigger_type") == "webhook_in" and task.get("webhook_token"):
        task["webhook_url"] = f"{_PUBLIC_CENTER_URL.rstrip('/')}/lachesis/webhook_in/{task['webhook_token']}"
    return task


# ── Tarefas / Relatórios ──────────────────────────────────────────────────────

@router.get("/tasks")
async def list_tasks():
    tasks = await asyncio.to_thread(_store.list_tasks)
    return {"tasks": [_task_view(t) for t in tasks]}


@router.post("/tasks")
async def create_task(body: TaskCreateRequest):
    data = body.model_dump()
    if not data.get("flow_definition"):
        sample_payload = (data.get("trigger_config") or {}).get("sample_payload")
        compiled = await asyncio.to_thread(
            compile_instruction_to_flow, data["instruction"], data["task_type"], sample_payload
        )
        data["flow_definition"] = compiled["flow_definition"]
    return _task_view(_store.create_task(data))


@router.get("/tasks/{task_id}")
async def get_task(task_id: int):
    task = await asyncio.to_thread(_store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_view(task)


@router.put("/tasks/{task_id}")
async def update_task(task_id: int, body: TaskUpdateRequest):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if ("instruction" in data or "task_type" in data) and "flow_definition" not in data:
        existing = await asyncio.to_thread(_store.get_task, task_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Task not found")
        instruction = data.get("instruction", existing["instruction"])
        task_type = data.get("task_type", existing.get("task_type", "agentless"))
        sample_payload = (existing.get("trigger_config") or {}).get("sample_payload")
        compiled = await asyncio.to_thread(
            compile_instruction_to_flow, instruction, task_type, sample_payload
        )
        data["flow_definition"] = compiled["flow_definition"]
    task = _store.update_task(task_id, data)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_view(task)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int):
    if not _store.delete_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"deleted": task_id}


@router.post("/tasks/{task_id}/toggle")
async def toggle_task(task_id: int):
    task = _store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    updated = _store.set_enabled(task_id, not task["enabled"])
    return _task_view(updated)


@router.post("/tasks/{task_id}/run_now")
async def run_task_now(task_id: int):
    task = _store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    # execute_task é bloqueante (WinRM, chamadas ao LLM) — corre numa thread à
    # parte para não travar o event loop do FastAPI (health-check, telemetria,
    # etc.) durante o tempo de execução da automação.
    return await asyncio.to_thread(lachesis_scheduler.execute_task, _store, task)


@router.get("/tasks/{task_id}/runs")
async def list_task_runs(task_id: int):
    task = await asyncio.to_thread(_store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    runs = await asyncio.to_thread(_store.list_runs, task_id)
    return {"runs": runs}


@router.get("/tasks/{task_id}/runs/{run_id}/download")
async def download_task_run(task_id: int, run_id: int, format: str = "txt"):
    task = _store.get_task(task_id)
    run = _store.get_run(run_id)
    if not task or not run or run["task_id"] != task_id:
        raise HTTPException(status_code=404, detail="Run not found")
    content, media_type, filename = render_task_run(run, task, format)
    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/schedule/parse")
async def schedule_parse(body: ScheduleParseRequest):
    return parse_schedule_description(body.description)


@router.post("/automations/compile")
async def compile_flow(body: FlowCompileRequest):
    return await asyncio.to_thread(
        compile_instruction_to_flow, body.instruction, body.task_type, body.sample_payload
    )


@router.post("/automations/summarize")
async def summarize_flow(body: FlowSummarizeRequest):
    """Inverso do /compile — gera texto a partir de um flow editado directamente no canvas."""
    summary = await asyncio.to_thread(summarize_flow_to_text, body.flow_definition)
    return {"instruction": summary}


@router.post("/automations/test_run")
async def test_run_flow(body: FlowTestRunRequest):
    """Corre um flow_definition ainda não gravado (rascunho do editor), sem tocar na BD."""
    return await asyncio.to_thread(run_flow, body.flow_definition)


@router.post("/tasks/{task_id}/flow")
async def save_task_flow(task_id: int, body: dict):
    task = _store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    flow_definition = body.get("flow_definition")
    if not isinstance(flow_definition, dict):
        raise HTTPException(status_code=422, detail="flow_definition em falta")
    updated = _store.save_flow(task_id, flow_definition, body.get("instruction_summary"))
    return _task_view(updated)


@router.get("/webhooks")
async def list_webhooks():
    webhooks = await asyncio.to_thread(_store.list_webhooks_brief)
    return {"webhooks": webhooks}


# ── Comportamentos ensinados ─────────────────────────────────────────────────

@router.get("/behaviors")
async def list_behaviors(kind: Literal["behavior", "infra_fact"] | None = None):
    behaviors = await asyncio.to_thread(_store.list_behaviors, kind)
    return {"behaviors": behaviors}


@router.get("/knowledge/facts")
async def list_infra_facts():
    """Factos de infraestrutura activos, agrupados por categoria (Casa do Conhecimento)."""
    facts = await asyncio.to_thread(_store.list_infra_facts)
    return {"facts": facts}


@router.post("/behaviors")
async def create_behavior(body: BehaviorCreateRequest):
    return _store.create_behavior(body.model_dump())


@router.put("/behaviors/{behavior_id}")
async def update_behavior(behavior_id: int, body: BehaviorUpdateRequest):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    behavior = _store.update_behavior(behavior_id, data)
    if not behavior:
        raise HTTPException(status_code=404, detail="Behavior not found")
    return behavior


@router.delete("/behaviors/{behavior_id}")
async def delete_behavior(behavior_id: int):
    if not _store.delete_behavior(behavior_id):
        raise HTTPException(status_code=404, detail="Behavior not found")
    return {"deleted": behavior_id}


@router.post("/behaviors/{behavior_id}/toggle")
async def toggle_behavior(behavior_id: int):
    behavior = _store._get_behavior(behavior_id)
    if not behavior:
        raise HTTPException(status_code=404, detail="Behavior not found")
    return _store.set_active(behavior_id, not behavior["active"])


# ── Casa do Conhecimento — propostas pendentes do Jarvis ─────────────────────
# Factos que o Jarvis propôs sozinho numa investigação (tool propose_infra_knowledge)
# e que vivem em Redis (memória de curto prazo) até um humano aprovar/rejeitar.
# Partilha o mesmo mecanismo de aprovação usado pelos cartões jarvis-approval no chat
# — aprovar aqui ou aprovar no chat chegam ao mesmo resultado.

@router.get("/knowledge/pending")
async def list_pending_knowledge():
    from api.chat_engine import _get_redis, _APPROVAL_PREFIX
    import json as _json

    pending = []
    r = _get_redis()
    if r is not None:
        try:
            for key in r.scan_iter(f"{_APPROVAL_PREFIX}*"):
                raw = r.get(key)
                if not raw:
                    continue
                action = _json.loads(raw)
                if action.get("tool") != "propose_infra_knowledge":
                    continue
                token = key.decode() if isinstance(key, bytes) else key
                token = token[len(_APPROVAL_PREFIX):]
                pending.append({**action, "token": token})
        except Exception:
            pass
    return {"pending": pending}


class KnowledgeApproveRequest(BaseModel):
    """Correções opcionais ao que o Jarvis propôs — o utilizador pode editar
    antes de aprovar, em vez de só poder aceitar tal e qual ou rejeitar."""
    title: str | None = None
    instruction: str | None = None
    category: str | None = None
    version: str | None = None
    evidence: str | None = None
    scope_type: Literal["global", "host", "target"] | None = None
    scope_value: str | None = None


@router.post("/knowledge/pending/{token}/approve")
async def approve_pending_knowledge(token: str, body: KnowledgeApproveRequest = KnowledgeApproveRequest()):
    from api.chat_engine import _pop_pending_action, _persist_infra_knowledge
    import json as _json

    action = _pop_pending_action(token)
    if not action or action.get("tool") != "propose_infra_knowledge":
        raise HTTPException(status_code=404, detail="Proposta não encontrada ou já processada.")

    overrides = body.model_dump(exclude_none=True)
    result_json, _sql = _persist_infra_knowledge(
        title       = overrides.get("title", action.get("title", "")),
        instruction = overrides.get("instruction", action.get("instruction", "")),
        category    = overrides.get("category", action.get("category")),
        version     = overrides.get("version", action.get("version")),
        evidence    = overrides.get("evidence", action.get("evidence")),
        scope_type  = overrides.get("scope_type", action.get("scope_type", "global")),
        scope_value = overrides.get("scope_value", action.get("scope_value")),
    )
    return _json.loads(result_json)


@router.post("/knowledge/pending/{token}/reject")
async def reject_pending_knowledge(token: str):
    from api.chat_engine import _pop_pending_action

    action = _pop_pending_action(token)
    if not action or action.get("tool") != "propose_infra_knowledge":
        raise HTTPException(status_code=404, detail="Proposta não encontrada ou já processada.")
    return {"rejected": token}


# ── Asclepion Systems ─────────────────────────────────────────────────────────

@router.get("/asclepion/profiles")
async def list_profiles():
    return {"profiles": _store.list_profiles()}


@router.post("/asclepion/profiles")
async def create_profile(body: ProfileCreateRequest):
    return _store.create_profile(body.model_dump())


@router.get("/asclepion/profiles/{profile_id}")
async def get_profile(profile_id: int):
    profile = _store.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.put("/asclepion/profiles/{profile_id}")
async def update_profile(profile_id: int, body: ProfileUpdateRequest):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    profile = _store.update_profile(profile_id, data)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.delete("/asclepion/profiles/{profile_id}")
async def delete_profile(profile_id: int):
    if not _store.delete_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"deleted": profile_id}


@router.post("/asclepion/profiles/{profile_id}/generate")
async def generate_checklist(profile_id: int):
    profile = _store.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    checklist = await asyncio.to_thread(asclepion_engine.generate_checklist, profile)
    return _store.set_checklist(profile_id, checklist)


def _run_profile_all(profile: dict, targets: list[str]) -> list[dict]:
    return [asclepion_engine.run_profile(profile, target, _store) for target in targets]


@router.post("/asclepion/profiles/{profile_id}/run")
async def run_profile(profile_id: int, body: ProfileRunRequest):
    profile = _store.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not (profile.get("checklist") or {}).get("checks"):
        raise HTTPException(status_code=422, detail="Checklist not generated yet")

    targets = [body.target] if body.target else profile["targets"]
    # Corre em thread à parte — WinRM real contra 1..N hosts pode demorar bem
    # mais do que o timeout do health-check do start_center.py (ver run_now).
    runs = await asyncio.to_thread(_run_profile_all, profile, targets)
    return {"runs": runs}


@router.get("/asclepion/profiles/{profile_id}/runs")
async def list_profile_runs(profile_id: int):
    profile = _store.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"runs": _store.list_asclepion_runs(profile_id)}


@router.get("/asclepion/runs/{run_id}")
async def get_asclepion_run(run_id: int):
    run = _store.get_asclepion_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/asclepion/runs/{run_id}/download")
async def download_asclepion_run(run_id: int, format: str = "json"):
    run = _store.get_asclepion_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    profile = _store.get_profile(run["profile_id"])
    content, media_type, filename = render_asclepion_run(run, profile, format)
    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
