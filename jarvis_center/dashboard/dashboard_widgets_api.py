"""
Dashboard Widgets API — construtor de widgets por linguagem natural para a
página inicial do Jarvis (Fase 3 do redesign da UI).

O utilizador escreve um pedido em português normal (ex: "hosts do Zabbix com
mais alertas") e o Jarvis usa o mesmo loop de raciocínio das automações
(`run_jarvis_loop`, channel="automation") para chamar as ferramentas Clotho
ao vivo que precisar e devolver um pequeno JSON de widget. Sem camada de
cache/histórico própria — cada criação ou atualização de widget é uma
execução real (chamadas às integrações + LLM), por isso a atualização é
manual, não automática.

Montado em /dashboard/* pelo ingestion_api.py.
"""

import json
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from dashboard.dashboard_widgets_store import DashboardWidgetsStore

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_API_KEY = os.getenv("JARVIS_API_KEY", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _check_api_key(key: str | None = Depends(_api_key_header)):
    if not _API_KEY:
        raise HTTPException(
            status_code=503,
            detail="API key not configured on server. Set JARVIS_API_KEY."
        )
    if key != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


_store = DashboardWidgetsStore()

_WIDGET_TYPES = {"metric", "list", "table", "text", "chart"}

_WIDGET_SYSTEM_INSTRUCTION = """\
Estás a criar um widget para a página inicial de uma dashboard, não uma \
conversa. A tua resposta final tem de ser APENAS um objecto JSON, sem \
markdown, sem ```` ``` ````, sem texto antes ou depois.

Usa call_integration_tool para qualquer dado ao vivo que precises (ex: \
Zabbix). Se o pedido mencionar uma ferramenta que não está configurada, \
não inventes dados — devolve um widget widget_type="text" a explicar isso \
com honestidade.

Formato exacto a devolver (escolhe UM widget_type consoante o pedido, a \
não ser que o utilizador tenha forçado um tipo específico — nesse caso \
usa sempre esse):

{"title": "...", "widget_type": "metric", "value": "7", "sub": "hosts críticos"}
{"title": "...", "widget_type": "list", "items": [{"label": "...", "detail": "...", "severity": "ok"|"warn"|"crit"|null}]}
{"title": "...", "widget_type": "table", "columns": ["Host","Severidade"], "rows": [["SVX01","Crítico"]]}
{"title": "...", "widget_type": "chart", "chart_type": "bar"|"line", "labels": ["Seg","Ter"], "values": [3, 7]}
{"title": "...", "widget_type": "text", "text": "..."}
"""


class CreateWidgetRequest(BaseModel):
    prompt: str
    created_by: str | None = None
    widget_type: str | None = None


class UpdateWidgetRequest(BaseModel):
    prompt: str | None = None
    widget_type: str | None = None
    col_span: int | None = None
    row_span: int | None = None


class ReorderRequest(BaseModel):
    order: list[int]


def _extract_json(text: str) -> str:
    """Extrai o objecto JSON da resposta do LLM, tolerando texto de
    raciocínio antes/depois do bloco (o modelo nem sempre segue a instrução
    de devolver só JSON, sobretudo em pedidos que exigem mais cruzamento).

    Localiza o primeiro '{' e avança um contador de profundidade (a
    respeitar strings/escapes) até encontrar o '}' que realmente fecha esse
    objecto — um simples regex greedy/non-greedy parte objectos JSON com
    valores aninhados (ex: widget_type="list" tem {"items": [{...}]}).
    """
    start = text.find("{")
    if start == -1:
        return text.strip()

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return text[start:].strip()


def _run_widget_prompt(
    prompt: str, widget_type_hint: str | None = None
) -> tuple[str | None, str | None, dict | None, str | None]:
    """Corre o prompt através do run_jarvis_loop e valida o JSON devolvido.

    Devolve (title, widget_type, data, error) — data nunca é preenchido a
    par de error; um dos dois é sempre None. widget_type_hint, se dado,
    força o tipo de visualização em vez de deixar o LLM escolher.
    """
    from api.chat_engine import run_jarvis_loop

    user_content = prompt
    if widget_type_hint in _WIDGET_TYPES:
        user_content = f'{prompt}\n\n(widget_type tem de ser exatamente "{widget_type_hint}")'

    try:
        result_text = run_jarvis_loop(
            [
                {"role": "system", "content": _WIDGET_SYSTEM_INSTRUCTION},
                {"role": "user", "content": user_content},
            ],
            channel="automation",
        )
    except Exception as e:
        return None, None, None, f"Falha a executar o pedido: {e}"

    try:
        parsed = json.loads(_extract_json(result_text))
    except (ValueError, TypeError):
        return None, None, None, "O Jarvis não devolveu um widget válido para este pedido."

    if not isinstance(parsed, dict):
        return None, None, None, "O Jarvis não devolveu um widget válido para este pedido."

    title = parsed.get("title")
    widget_type = parsed.get("widget_type")
    if widget_type not in _WIDGET_TYPES:
        return None, None, None, "O Jarvis devolveu um tipo de widget desconhecido."

    required = {
        "metric": ("value",),
        "list": ("items",),
        "table": ("columns", "rows"),
        "chart": ("labels", "values"),
        "text": ("text",),
    }[widget_type]
    if not all(k in parsed for k in required):
        return None, None, None, "O widget devolvido está incompleto."

    return title, widget_type, parsed, None


@router.get("/widgets")
async def list_widgets(_=Depends(_check_api_key)):
    widgets = await _to_thread(_store.list_widgets)
    return {"widgets": widgets}


@router.post("/widgets")
async def create_widget(body: CreateWidgetRequest, _=Depends(_check_api_key)):
    widget = await _to_thread(_store.create_widget, body.prompt, body.created_by)
    title, widget_type, data, error = await _to_thread(
        _run_widget_prompt, body.prompt, body.widget_type
    )
    updated = await _to_thread(_store.set_result, widget["id"], title, widget_type, data, error)
    return updated


@router.post("/widgets/{widget_id}/refresh")
async def refresh_widget(widget_id: int, _=Depends(_check_api_key)):
    widget = await _to_thread(_store.get_widget, widget_id)
    if not widget:
        raise HTTPException(status_code=404, detail="Widget not found")
    # Mantém o mesmo tipo de visualização entre atualizações, se já tiver
    # sido decidido/forçado da primeira vez.
    title, widget_type, data, error = await _to_thread(
        _run_widget_prompt, widget["prompt"], widget.get("widget_type")
    )
    updated = await _to_thread(_store.set_result, widget_id, title, widget_type, data, error)
    return updated


@router.patch("/widgets/{widget_id}")
async def update_widget(widget_id: int, body: UpdateWidgetRequest, _=Depends(_check_api_key)):
    widget = await _to_thread(_store.get_widget, widget_id)
    if not widget:
        raise HTTPException(status_code=404, detail="Widget not found")

    if body.prompt is not None or body.widget_type is not None:
        prompt = body.prompt if body.prompt is not None else widget["prompt"]
        widget_type_hint = body.widget_type if body.widget_type is not None else widget.get("widget_type")
        title, widget_type, data, error = await _to_thread(
            _run_widget_prompt, prompt, widget_type_hint
        )
        widget = await _to_thread(_store.set_result, widget_id, title, widget_type, data, error)
        if body.prompt is not None and widget is not None:
            widget = await _to_thread(_store.set_prompt, widget_id, body.prompt)

    if body.col_span is not None or body.row_span is not None:
        widget = await _to_thread(_store.update_layout, widget_id, body.col_span, body.row_span)

    return widget


@router.post("/widgets/reorder")
async def reorder_widgets(body: ReorderRequest, _=Depends(_check_api_key)):
    await _to_thread(_store.reorder, body.order)
    return {"ok": True}


@router.delete("/widgets/{widget_id}")
async def delete_widget(widget_id: int, _=Depends(_check_api_key)):
    if not await _to_thread(_store.delete_widget, widget_id):
        raise HTTPException(status_code=404, detail="Widget not found")
    return {"deleted": widget_id}


async def _to_thread(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)
