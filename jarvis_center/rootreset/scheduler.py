"""
rootreset_scheduler_worker — thread de fundo (mesmo padrão de
lachesis.lachesis_scheduler.lachesis_scheduler_worker) que, passado o atraso
configurado (root_reset_requests.investigate_at, por omissão 24h depois do
reset), corre o SecurityInvestigator já existente para comparar o motivo
declarado com a actividade real na máquina.

Reaproveita directamente agentless/investigators/security_investigator.py e
agentless_store.py — o mesmo mecanismo usado por POST /agentless/security/investigate
— em vez de duplicar a lógica de investigação.
"""

import time
from datetime import datetime, timezone

from agentless.agentless_store import AgentlessStore
from agentless.investigators.security_investigator import SecurityInvestigator
from rootreset.store import RootResetStore

_POLL_INTERVAL = 300  # segundos — não há urgência (janela é de horas)


def investigate_request(
    req: dict,
    store: RootResetStore,
    investigator: SecurityInvestigator | None = None,
    agentless_store: AgentlessStore | None = None,
) -> dict:
    """Corre a investigação para UM pedido já aprovado e grava o resultado —
    partilhado entre o scheduler de 24h (_run_due) e o botão "Investigar
    agora" (rootreset_api.py::investigate_now). Propaga excepções: quem
    chama decide se marca erro ou deixa para nova tentativa."""
    investigator = investigator or SecurityInvestigator()
    agentless_store = agentless_store or AgentlessStore()
    access_method = "laps" if req["mechanism"] == "laps" else "password_reset"
    result = investigator.investigate(
        machine=req["hostname"],
        username="root",
        reason=req["motivo_text"],
        access_method=access_method,
        credential_time=req["approved_at"],
        machine_type="workstation",
    )
    agentless_store.save_security_investigation(result)
    store.mark_investigated(req["id"], result["investigation_id"])
    return result


def _run_due(store: RootResetStore) -> int:
    due = store.due_for_investigation()
    if not due:
        return 0

    investigator = SecurityInvestigator()
    agentless_store = AgentlessStore()

    for req in due:
        try:
            investigate_request(req, store, investigator, agentless_store)
        except Exception as e:
            # Não marca investigated_at — fica devido de novo no próximo ciclo.
            print(f"[ROOTRESET] falha ao investigar pedido {req['id']} ({req['hostname']}): {e}")

    return len(due)


def rootreset_scheduler_worker():
    import start_center as _sc

    store = RootResetStore()
    while not _sc.shutdown_flag:
        time.sleep(_POLL_INTERVAL)
        try:
            _run_due(store)
        except Exception as e:
            _sc.log(f"[ROOTRESET] erro no scheduler: {e}")
