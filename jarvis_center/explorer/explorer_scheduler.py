"""
Explorer Scheduler — poller próprio do Explorer Engine (padrão de
lachesis/lachesis_scheduler.py), desacoplado da maquinaria de flows/webhooks
do Lachesis — o Explorer não reutiliza lachesis_tasks, tem o seu próprio
campo de cadência (scope_targets.explore_cadence_seconds) e o seu próprio
"due" (scope_store.due_explorations()).

Corre com um intervalo de varredura curto (60s), mas cada alvo só é
explorado de facto quando a sua própria cadência (default 1h) expira —
o intervalo de varredura não determina a frequência de chamadas ao LLM.
"""

import time

from scope_collector.scope_store import ScopeStore
from scope_collector.frame_builder import host_key as compute_host_key
from explorer.state_reader import build_evidence
from explorer.explorer_engine import run_explorer

_POLL_INTERVAL = 60  # segundos entre varreduras de scope_targets devidos para exploração


def run_due_explorations(store: ScopeStore) -> int:
    due = store.due_explorations()
    explored = 0

    for target in due:
        try:
            hk = compute_host_key(target)
            evidence = build_evidence(hk)

            if not evidence.get("snapshot"):
                # ainda sem telemetria suficiente deste alvo — não vale a pena gastar LLM
                continue

            run_explorer(target, evidence, store)
            explored += 1
        except Exception as e:
            print(f"[EXPLORER] erro no alvo '{target.get('name')}': {e}")
        finally:
            store.record_exploration(target["id"])

    return explored


def explorer_scheduler_worker():
    """Thread de fundo, arrancada por start_center.py (padrão de lachesis_scheduler_worker)."""
    import start_center as _sc

    store = ScopeStore()

    while not _sc.shutdown_flag:
        time.sleep(_POLL_INTERVAL)
        try:
            run_due_explorations(store)
        except Exception as e:
            _sc.log(f"[EXPLORER] erro no loop: {e}")
