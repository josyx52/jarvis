"""
Lachesis — endpoint público de entrada para automações do tipo trigger_type='webhook_in'.

Ao contrário do resto do Lachesis (montado em /fates/lachesis/* e protegido pelo
X-API-Key do Jarvis), esta rota é chamada por sistemas externos (Power Automate,
Zabbix, etc.) que não conhecem essa chave. A autenticação é o próprio token —
aleatório, 256 bits, gerado em LachesisStore.create_task/update_task e único por
tarefa — que faz parte do path, ao estilo "When an HTTP request is received" do
Power Automate: POST https://.../lachesis/webhook_in/<token>.

Montado directamente em ingestion_api.py (fora do router /fates), sem
dependency de API key.
"""

import asyncio
import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from lachesis import lachesis_scheduler
from lachesis.lachesis_store import LachesisStore

router = APIRouter(prefix="/lachesis/webhook_in", tags=["lachesis-webhook-in"])

_store = LachesisStore()


@router.post("/{token}")
async def trigger_webhook(token: str, request: Request, background_tasks: BackgroundTasks):
    task = await asyncio.to_thread(_store.get_task_by_webhook_token, token)
    if not task or task.get("trigger_type") != "webhook_in":
        raise HTTPException(status_code=404, detail="Webhook not found")
    if not task["enabled"]:
        raise HTTPException(status_code=403, detail="Task disabled")

    raw = await request.body()
    trigger_payload = None
    if raw:
        try:
            trigger_payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            trigger_payload = raw.decode("utf-8", errors="replace")

    # Responde de imediato: sistemas externos (Zabbix, Power Automate, etc.) têm
    # timeouts HTTP curtos e reenviam o pedido — disparando a automação outra vez
    # — se a resposta só chegar depois de a automação inteira terminar (WinRM,
    # LLM, envio de mensagens). execute_task corre em background, já depois da
    # resposta ter sido enviada; BackgroundTasks despacha automaticamente para
    # uma thread por ser uma função síncrona.
    background_tasks.add_task(lachesis_scheduler.execute_task, _store, task, trigger_payload)
    return {"triggered": True, "task_id": task["id"]}
