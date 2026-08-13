"""
Clotho API — criação e gestão de integrações com ferramentas empresariais.

O utilizador descreve a ligação (nome, host, porta, autenticação, notas) e o
Jarvis (Clotho) identifica de que solução se trata — não há uma lista fixa de tipos.

Montado em /fates/clotho/* pelo fates_api.py.
"""

import time as _time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from clotho.clotho_store import ClothoStore
from clotho.clotho_analyzer import analyze_integration
from clotho.clotho_database_api import router as database_router
from clotho.scope_api import router as scope_router
from clotho.clotho_mentions import get_mentions_catalog
from clotho.clotho_tester import (
    prepare_test_request,
    execute_test_request,
    evaluate_test_attempt,
    summarize_test_attempts,
    generate_tools_catalog,
    generate_tool_from_description,
    edit_tool_from_description,
    build_tool_request,
)
from clotho.clotho_ingestor import (
    parse_curl,
    list_openapi_endpoints,
    parse_openapi_endpoint,
    fetch_spec,
    list_postman_requests,
    parse_postman_request,
    get_postman_item,
)

_MAX_TEST_ATTEMPTS = 5

router = APIRouter(prefix="/clotho", tags=["clotho"])

router.include_router(database_router)
router.include_router(scope_router)

_store = ClothoStore()


class IntegrationCreateRequest(BaseModel):
    name: str
    host: str | None = None
    port: int | None = None
    auth_type: str | None = None
    config: dict | None = None
    notes: str | None = None
    created_by: str | None = None


class TestExecuteRequest(BaseModel):
    request: dict


class ToolExecuteRequest(BaseModel):
    params: dict | None = None


class ToolCreateRequest(BaseModel):
    name: str
    description: str


class ToolUpdateRequest(BaseModel):
    tool: dict


class ApplyFixRequest(BaseModel):
    fixed_request: dict


class ToolEditFromDescriptionRequest(BaseModel):
    description: str


class CurlToolRequest(BaseModel):
    curl: str
    name: str


class SpecParseRequest(BaseModel):
    spec: dict | str | None = None
    spec_url: str | None = None


class SpecToolRequest(BaseModel):
    spec: dict | None = None
    spec_url: str | None = None
    path: str
    method: str
    name: str


class PostmanParseRequest(BaseModel):
    collection: dict


class PostmanToolRequest(BaseModel):
    collection: dict
    item_path: list[str]
    name: str


def _set_tool_meta(catalog: dict, tool_name: str, **meta: object) -> None:
    tool = (catalog.get("tools") or {}).get(tool_name)
    if tool is None:
        return
    existing = tool.get("_meta") or {}
    existing.update(meta)
    tool["_meta"] = existing


def _strip_secret(integ: dict) -> dict:
    integ = dict(integ)
    config = integ.pop("config", None) or {}
    integ["has_credentials"] = bool(config)
    return integ


@router.get("/mentions")
async def list_mentions():
    return {"mentions": get_mentions_catalog()}


@router.get("/integrations")
async def list_integrations():
    return {"integrations": _store.list_integrations()}


@router.get("/integrations/{integration_id}")
async def get_integration(integration_id: int):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")
    return _strip_secret(integ)


def _norm_host(host: str | None) -> str | None:
    if not host:
        return host
    import re as _re
    return _re.sub(r'^(https?):[\\/]+', lambda m: m.group(1) + '://', host.strip())


@router.post("/integrations")
async def create_integration(body: IntegrationCreateRequest):
    data = body.model_dump()
    data["host"] = _norm_host(data.get("host"))
    return _store.create_integration(data)


@router.post("/integrations/{integration_id}/analyze")
async def analyze(integration_id: int):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")

    result = analyze_integration(
        name=integ["name"],
        host=integ.get("host"),
        port=integ.get("port"),
        auth_type=integ.get("auth_type"),
        notes=integ.get("notes"),
    )
    updated = _store.set_analysis(integration_id, result["type"], result["analysis"])
    return updated


@router.post("/integrations/{integration_id}/test/prepare")
async def test_prepare(integration_id: int):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")
    return prepare_test_request(integ)


@router.post("/integrations/{integration_id}/test/execute")
async def test_execute(integration_id: int, body: TestExecuteRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")

    attempts = []
    current_request = body.request
    success = False

    for _ in range(_MAX_TEST_ATTEMPTS):
        response = execute_test_request(integ, current_request)
        evaluation = evaluate_test_attempt(integ, current_request, response)
        attempts.append({
            "request": current_request,
            "response": response,
            "note": evaluation["note"],
        })
        if evaluation["success"]:
            success = True
            break
        if not evaluation["fixed_request"]:
            break
        current_request = evaluation["fixed_request"]

    summary = summarize_test_attempts(integ, attempts, success)
    return {"attempts": attempts, "success": success, "summary": summary}


@router.post("/integrations/{integration_id}/tools/generate")
async def tools_generate(integration_id: int):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")

    catalog = generate_tools_catalog(integ)
    for tname in list((catalog.get("tools") or {})):
        _set_tool_meta(catalog, tname, test_status="pending", human_edited_after_test=False)
    return _strip_secret(_store.set_tools(integration_id, catalog))


@router.post("/integrations/{integration_id}/tools")
async def tools_create(integration_id: int, body: ToolCreateRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")

    tool = generate_tool_from_description(integ, body.description)
    if not tool:
        raise HTTPException(status_code=422, detail="Could not generate tool from description")

    tool["_meta"] = {"test_status": "pending", "human_edited_after_test": False}

    catalog = integ.get("tools") or {}
    catalog.setdefault("tools", {})
    catalog["tools"][body.name] = tool
    return _strip_secret(_store.set_tools(integration_id, catalog))


@router.post("/integrations/{integration_id}/tools/from_curl")
async def tools_from_curl(integration_id: int, body: CurlToolRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")

    tool = parse_curl(body.curl, integ)
    if not tool:
        raise HTTPException(status_code=422, detail="Não foi possível parsear o cURL")

    tool["_meta"] = {"test_status": "pending", "human_edited_after_test": False, "source": "curl"}
    catalog = integ.get("tools") or {}
    catalog.setdefault("tools", {})
    catalog["tools"][body.name] = tool
    return _strip_secret(_store.set_tools(integration_id, catalog))


@router.post("/integrations/{integration_id}/spec/parse")
async def spec_parse(integration_id: int, body: SpecParseRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")

    spec = body.spec
    if not spec and body.spec_url:
        try:
            spec = fetch_spec(body.spec_url)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if not spec:
        raise HTTPException(status_code=422, detail="Forneça spec (JSON/dict) ou spec_url")
    if isinstance(spec, str):
        import json as _json
        try:
            spec = _json.loads(spec)
        except Exception:
            raise HTTPException(status_code=422, detail="spec deve ser JSON válido")

    endpoints = list_openapi_endpoints(spec)
    title = (spec.get("info") or {}).get("title", "")
    version = (spec.get("info") or {}).get("version", "")
    return {"title": title, "version": version, "endpoints": endpoints, "count": len(endpoints)}


@router.post("/integrations/{integration_id}/tools/from_spec")
async def tools_from_spec(integration_id: int, body: SpecToolRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")

    spec = body.spec
    if not spec and body.spec_url:
        try:
            spec = fetch_spec(body.spec_url)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if not spec:
        raise HTTPException(status_code=422, detail="Forneça spec ou spec_url")

    tool = parse_openapi_endpoint(spec, body.path, body.method, integ)
    if not tool:
        raise HTTPException(status_code=422, detail=f"Endpoint {body.method} {body.path} não encontrado na spec")

    tool["_meta"] = {"test_status": "pending", "human_edited_after_test": False, "source": "openapi"}
    catalog = integ.get("tools") or {}
    catalog.setdefault("tools", {})
    catalog["tools"][body.name] = tool
    return _strip_secret(_store.set_tools(integration_id, catalog))


@router.post("/integrations/{integration_id}/postman/parse")
async def postman_parse(integration_id: int, body: PostmanParseRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")
    requests_list = list_postman_requests(body.collection)
    name = (body.collection.get("info") or {}).get("name", "")
    return {"name": name, "requests": requests_list, "count": len(requests_list)}


@router.post("/integrations/{integration_id}/tools/from_postman")
async def tools_from_postman(integration_id: int, body: PostmanToolRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")

    item = get_postman_item(body.collection, body.item_path)
    if not item or "request" not in item:
        raise HTTPException(status_code=422, detail=f"Request não encontrado no path {body.item_path}")

    tool = parse_postman_request(item, integ)
    if not tool:
        raise HTTPException(status_code=422, detail="Não foi possível parsear o request Postman")

    tool["_meta"] = {"test_status": "pending", "human_edited_after_test": False, "source": "postman"}
    catalog = integ.get("tools") or {}
    catalog.setdefault("tools", {})
    catalog["tools"][body.name] = tool
    return _strip_secret(_store.set_tools(integration_id, catalog))


@router.post("/integrations/{integration_id}/tools/{tool_name}/execute")
async def tools_execute(integration_id: int, tool_name: str, body: ToolExecuteRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")

    catalog = integ.get("tools") or {}
    tool = (catalog.get("tools") or {}).get(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    body_encoding = tool.get("body_encoding")
    has_required_params = bool(tool.get("params_schema"))
    user_provided_params = bool(body.params)

    # CASO B: execução com params — directa, sem avaliador LLM
    if user_provided_params:
        resolved = build_tool_request(tool["request"], body.params)
        response = execute_test_request(integ, resolved, body_encoding=body_encoding)
        _set_tool_meta(catalog, tool_name, last_executed_at=int(_time.time()))
        _store.set_tools(integration_id, catalog)
        return {
            "mode": "execute",
            "attempts": [{"request": resolved, "response": response, "note": f"HTTP {response.get('status_code')}"}],
            "success": response.get("ok", False),
        }

    # Tool precisa de params mas nenhum fornecido — skip
    if has_required_params and not user_provided_params:
        return {
            "mode": "skipped",
            "attempts": [],
            "success": None,
            "note": f"Esta tool requer parâmetros ({', '.join(tool['params_schema'].keys())}). Forneça valores para testar.",
            "params_schema": tool["params_schema"],
        }

    # CASO A: teste de conectividade — avaliador + propor fix (NUNCA guardar sobre template)
    attempts = []
    current_request = build_tool_request(tool["request"], {})
    success = False
    proposed_fix = None

    for _ in range(_MAX_TEST_ATTEMPTS):
        response = execute_test_request(integ, current_request, body_encoding=body_encoding)
        evaluation = evaluate_test_attempt(integ, current_request, response)
        attempts.append({"request": current_request, "response": response, "note": evaluation["note"]})
        if evaluation["success"]:
            success = True
            break
        if not evaluation["fixed_request"]:
            break
        proposed_fix = evaluation["fixed_request"]
        current_request = evaluation["fixed_request"]

    if success:
        _set_tool_meta(catalog, tool_name, test_status="pass", last_tested_at=int(_time.time()))
    else:
        _set_tool_meta(catalog, tool_name, test_status="fail")
    _store.set_tools(integration_id, catalog)
    return {
        "mode": "test",
        "attempts": attempts,
        "success": success,
        "proposed_fix": proposed_fix if not success else None,
    }


@router.put("/integrations/{integration_id}/tools/{tool_name}")
async def tools_update(integration_id: int, tool_name: str, body: ToolUpdateRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")
    catalog = integ.get("tools") or {}
    tools = catalog.get("tools") or {}
    if tool_name not in tools:
        raise HTTPException(status_code=404, detail="Tool not found")
    tool = body.tool
    req = tool.get("request") if isinstance(tool, dict) else None
    if not isinstance(req, dict) or not req.get("url"):
        raise HTTPException(status_code=422, detail="Tool must have request.url")
    # Preservar _meta existente e marcar como editado por humano
    existing_meta = (tools[tool_name].get("_meta") or {}) if isinstance(tools.get(tool_name), dict) else {}
    tool["_meta"] = {**existing_meta, "human_edited_after_test": True, "test_status": "pending"}
    tools[tool_name] = tool
    catalog["tools"] = tools
    return _strip_secret(_store.set_tools(integration_id, catalog))


@router.post("/integrations/{integration_id}/tools/{tool_name}/edit_from_description")
async def tools_edit_from_description(integration_id: int, tool_name: str, body: ToolEditFromDescriptionRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")
    catalog = integ.get("tools") or {}
    tool = (catalog.get("tools") or {}).get(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    updated = edit_tool_from_description(integ, tool, body.description)
    if not updated:
        raise HTTPException(status_code=422, detail="Could not update tool from description")

    existing_meta = (tool.get("_meta") or {})
    updated["_meta"] = {**existing_meta, "human_edited_after_test": True, "test_status": "pending"}
    catalog["tools"][tool_name] = updated
    return _strip_secret(_store.set_tools(integration_id, catalog))


@router.post("/integrations/{integration_id}/tools/{tool_name}/apply_fix")
async def tools_apply_fix(integration_id: int, tool_name: str, body: ApplyFixRequest):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")

    catalog = integ.get("tools") or {}
    tool = (catalog.get("tools") or {}).get(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    fixed_req = body.fixed_request
    if not isinstance(fixed_req, dict) or not fixed_req.get("url"):
        raise HTTPException(status_code=422, detail="fixed_request must have url")

    # Aplicar o fix sem marcar como human_edited — aprovação humana libera o loop automático
    catalog["tools"][tool_name]["request"] = {
        "method": fixed_req.get("method"),
        "url": fixed_req.get("url"),
        "headers": fixed_req.get("headers"),
        "body": fixed_req.get("body"),
    }
    _set_tool_meta(catalog, tool_name, human_edited_after_test=False, test_status="pending")
    _store.set_tools(integration_id, catalog)

    # Re-fetch e re-testar com loop automático
    integ = _store.get_integration(integration_id)
    catalog = integ.get("tools") or {}
    tool = (catalog.get("tools") or {})[tool_name]

    attempts = []
    current_request = build_tool_request(tool["request"], {})
    success = False

    for _ in range(_MAX_TEST_ATTEMPTS):
        response = execute_test_request(integ, current_request)
        evaluation = evaluate_test_attempt(integ, current_request, response)
        attempts.append({"request": current_request, "response": response, "note": evaluation["note"]})
        if evaluation["success"]:
            success = True
            break
        if not evaluation["fixed_request"]:
            break
        current_request = evaluation["fixed_request"]

    if success:
        _set_tool_meta(catalog, tool_name, test_status="pass", last_tested_at=int(_time.time()))
        original_body = (tool.get("request") or {}).get("body") or ""
        has_placeholders = "{{params." in str(original_body) or "{{now" in str(original_body)
        if not has_placeholders and current_request != tool["request"]:
            catalog["tools"][tool_name]["request"] = {
                "method": current_request.get("method"),
                "url": current_request.get("url"),
                "headers": current_request.get("headers"),
                "body": current_request.get("body"),
            }
    else:
        _set_tool_meta(catalog, tool_name, test_status="fail")
    _store.set_tools(integration_id, catalog)
    return {"attempts": attempts, "success": success}


@router.delete("/integrations/{integration_id}/tools/{tool_name}")
async def tools_delete(integration_id: int, tool_name: str):
    integ = _store.get_integration(integration_id)
    if not integ:
        raise HTTPException(status_code=404, detail="Integration not found")
    catalog = integ.get("tools") or {}
    tools = catalog.get("tools") or {}
    if tool_name not in tools:
        raise HTTPException(status_code=404, detail="Tool not found")
    del tools[tool_name]
    catalog["tools"] = tools
    _store.set_tools(integration_id, catalog)
    return {"deleted": tool_name}


@router.delete("/integrations/{integration_id}")
async def delete_integration(integration_id: int):
    if not _store.delete_integration(integration_id):
        raise HTTPException(status_code=404, detail="Integration not found")
    return {"deleted": integration_id}
