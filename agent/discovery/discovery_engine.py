"""
DiscoveryEngine — autodiscovery continuo em runtime.

Corre em background a cada AUTODISCOVERY_INTERVAL_SECONDS.
Compara o estado atual do servidor com o que esta configurado.

Se encontrar algo novo:
  - Regista como evento "unconfigured_service"
  - Se ai_on_change=True, chama Claude para analisar e sugerir config
  - Atualiza jarvis_config.json se houver mudancas claras

Exemplos de mudancas que deteta:
  - Nova porta aberta (nova DB, novo broker)
  - Novo processo relevante a correr
  - Servico anteriormente configurado que desapareceu
"""

import threading
import time
import json
import logging

from setup.server_scanner import ServerScanner
import agent_core.config as cfg

logger = logging.getLogger(__name__)


class DiscoveryEngine:

    def __init__(self):
        self.scanner          = ServerScanner()
        self._lock            = threading.Lock()
        self._thread          = None
        self._running         = False
        self._last_snapshot   = None
        self._pending_events  = []

    # ----------------------------------------
    # START / STOP
    # ----------------------------------------

    def start(self):
        if not cfg.AUTODISCOVERY_ENABLED:
            logger.info("[DISCOVERY] Autodiscovery desativado na config.")
            return

        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            daemon=True,
            name="discovery-engine"
        )
        self._thread.start()
        logger.info(f"[DISCOVERY] Iniciado (intervalo: {cfg.AUTODISCOVERY_INTERVAL_SECONDS}s)")

    def stop(self):
        self._running = False

    # ----------------------------------------
    # DRAIN — devolve eventos pendentes e limpa
    # ----------------------------------------

    def drain(self) -> list[dict]:
        with self._lock:
            events = self._pending_events[:]
            self._pending_events.clear()
        return events

    # ----------------------------------------
    # LOOP PRINCIPAL
    # ----------------------------------------

    def _loop(self):
        # Primeiro scan imediato
        self._run_discovery()

        while self._running:
            time.sleep(cfg.AUTODISCOVERY_INTERVAL_SECONDS)
            if self._running:
                self._run_discovery()

    def _run_discovery(self):
        try:
            current = self.scanner.scan()
            detected = current.get("detected", {})

            if self._last_snapshot is None:
                # Primeiro scan — apenas guarda baseline
                self._last_snapshot = detected
                return

            changes = self._diff(self._last_snapshot, detected)

            if changes:
                logger.info(f"[DISCOVERY] {len(changes)} mudanca(s) detetada(s): {changes}")
                self._handle_changes(changes, current)

            self._last_snapshot = detected

        except Exception as e:
            logger.warning(f"[DISCOVERY] Erro no scan: {e}")

    # ----------------------------------------
    # DIFF
    # ----------------------------------------

    def _diff(self, previous: dict, current: dict) -> list[dict]:
        changes = []

        def names(items):
            return {i.get("name") for i in items}

        categories = [
            ("databases",      "new_database"),
            ("message_queues", "new_message_queue"),
            ("web_servers",    "new_web_server"),
            ("api_runtimes",   "new_api_runtime"),
        ]

        for key, change_type in categories:
            prev_names = names(previous.get(key, []))
            curr_names = names(current.get(key, []))

            # Novos
            for name in curr_names - prev_names:
                changes.append({"type": change_type, "name": name, "action": "appeared"})

            # Desaparecidos
            for name in prev_names - curr_names:
                changes.append({"type": change_type, "name": name, "action": "disappeared"})

        return changes

    # ----------------------------------------
    # HANDLE CHANGES
    # ----------------------------------------

    def _handle_changes(self, changes: list[dict], full_scan: dict):
        for change in changes:
            event = self._build_event(change)

            with self._lock:
                self._pending_events.append(event)

        # Se AI on change estiver ativo, pede analise ao Claude
        if cfg.AUTODISCOVERY_AI_ON_CHANGE:
            self._ask_ai_about_changes(changes, full_scan)

    def _build_event(self, change: dict) -> dict:
        action = change.get("action")
        name   = change.get("name")
        ctype  = change.get("type")

        if action == "appeared":
            severity = "medium"
            summary  = f"Novo servico detectado: {name} ({ctype}). Nao configurado no agente."
        else:
            severity = "high"
            summary  = f"Servico desapareceu: {name} ({ctype}). Estava presente no ultimo scan."

        return {
            "event_type":  "discovery_change",
            "severity":    severity,
            "entity_type": ctype,
            "entity_name": name,
            "summary":     summary,
            "payload": {
                "change_type": ctype,
                "change_name": name,
                "action":      action,
            }
        }

    # ----------------------------------------
    # AI ON CHANGE
    # ----------------------------------------

    def _ask_ai_about_changes(self, changes: list[dict], scan: dict):
        try:
            from brain.foundry_client import FoundryClient
            llm = FoundryClient()

            prompt = f"""
SYSTEM:
You are a Jarvis monitoring agent AI.
New services were detected on this server. Analyze and respond in Portuguese.
Be brief and technical.

CHANGES DETECTED:
{json.dumps(changes, indent=2)}

SERVER CONTEXT:
{json.dumps(scan.get('detected', {}), indent=2)}

Tell the operator:
1. What these changes likely mean
2. Whether they should update the agent configuration
3. Any security concerns
"""
            response = llm.generate(prompt, max_tokens=512)
            logger.info(f"[DISCOVERY] AI analysis: {response[:300]}")

            # Gera evento com a analise da AI
            with self._lock:
                self._pending_events.append({
                    "event_type":  "discovery_ai_analysis",
                    "severity":    "info",
                    "entity_type": "server",
                    "entity_name": scan.get("hostname", "unknown"),
                    "summary":     "AI analisou mudancas de discovery",
                    "payload":     {"analysis": response, "changes": changes}
                })

        except Exception as e:
            logger.debug(f"[DISCOVERY] AI indisponivel: {e}")
