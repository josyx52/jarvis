"""
Lachesis — scheduler de tarefas/relatórios.

Thread de fundo (padrão de start_center._retention_worker): periodicamente
verifica tarefas devidas (lachesis_tasks.next_run_at <= NOW()), executa-as via
run_jarvis_loop e regista o resultado em lachesis_runs.
"""

import time
from datetime import datetime, timezone

import requests

from lachesis.lachesis_schedule import compute_next_run
from lachesis.lachesis_store import LachesisStore

_POLL_INTERVAL = 45  # segundos
_SEND_TIMEOUT = 10


def execute_task(store: LachesisStore, task: dict, trigger_payload=None) -> dict:
    """Executa uma tarefa imediatamente (chamado pelo scheduler, por /run_now ou por um
    webhook_in recebido — nesse caso trigger_payload traz o corpo do pedido).

    Se a tarefa tiver um flow_definition compilado, corre-o deterministicamente
    (lachesis_flow_executor). Caso contrário (tarefas antigas), mantém o
    caminho legado: reinterpretar a instrução em texto livre a cada corrida.
    """
    run_id = store.record_run_start(task["id"])

    result_text = None
    error = None
    last_status = "ok"
    try:
        if task.get("flow_definition"):
            from lachesis.lachesis_flow_executor import run_flow
            outcome = run_flow(task["flow_definition"], trigger_payload)
            result_text = outcome["result_text"]
            if outcome["error"]:
                error = outcome["error"]
                last_status = "error"
        else:
            from api.chat_engine import run_jarvis_loop
            result_text = run_jarvis_loop(
                [{"role": "user", "content": task["instruction"]}],
                channel="automation",
            )
    except Exception as e:
        error = str(e)
        last_status = "error"

    delivered = False
    if last_status == "ok" and task.get("webhook_id"):
        delivered = _deliver_webhook(store, task, result_text)

    store.record_run_finish(run_id, last_status, result_text, error, delivered)

    next_run = compute_next_run(task["schedule"])
    store.update_after_run(task["id"], last_status, next_run)
    if next_run is None and task["schedule"].get("kind") == "once":
        store.set_enabled(task["id"], False)

    return {
        "run_id": run_id,
        "status": last_status,
        "result_text": result_text,
        "error": error,
        "webhook_delivered": delivered,
    }


def _deliver_webhook(store: LachesisStore, task: dict, result_text: str | None) -> bool:
    webhook = store.get_webhook(task["webhook_id"])
    if not webhook:
        return False

    from engines.notification_engine import _sign_payload

    payload = {
        "task": task["name"],
        "result": result_text,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    body = __import__("json").dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    sig = _sign_payload(webhook.get("secret"), body)
    if sig:
        headers["X-Jarvis-Signature"] = sig

    try:
        resp = requests.post(webhook["url"], data=body, headers=headers, timeout=_SEND_TIMEOUT)
        return resp.ok
    except Exception:
        return False


def run_due_tasks(store: LachesisStore) -> int:
    due = store.due_tasks()
    for task in due:
        execute_task(store, task)
    return len(due)


def lachesis_scheduler_worker():
    import start_center as _sc

    store = LachesisStore()
    while not _sc.shutdown_flag:
        time.sleep(_POLL_INTERVAL)
        try:
            run_due_tasks(store)
        except Exception as e:
            _sc.log(f"[LACHESIS] erro no scheduler: {e}")
