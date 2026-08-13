"""
command_poller.py — Solution Driver (lado do agente)

Polled cada 3s do /agent/commands/pending.
Para cada comando recebido executa PowerShell em subprocess isolado,
captura stdout/stderr/exit_code e devolve ao centro via POST result.

Modelo idêntico ao Claude Code: a IA escreve o script, o agente executa.
Sem lista branca de comandos — o agente executa tudo o que a IA enviar.
"""

import json
import logging
import subprocess
import threading
import time

import requests

from agent_core.config import CORE_URL, API_KEY, HTTP_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 3          # segundos entre polls
_MAX_OUTPUT    = 1_000_000  # truncar stdout a 1 MB
_MAX_STDERR    = 100_000    # truncar stderr a 100 KB


class CommandPoller:

    def __init__(self, host: str):
        self._host    = host
        self._running = False
        self._thread  = None
        self._session = requests.Session()
        if API_KEY:
            self._session.headers["X-API-Key"] = API_KEY

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            daemon=True,
            name="cmd-poller",
        )
        self._thread.start()
        logger.info("[CmdPoller] iniciado para host=%s", self._host)

    def stop(self):
        self._running = False

    # ── Loop principal ─────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                self._poll_and_execute()
            except Exception as e:
                logger.debug("[CmdPoller] erro no loop: %s", e)
            time.sleep(_POLL_INTERVAL)

    def _poll_and_execute(self):
        url = f"{CORE_URL}/agent/commands/pending"
        try:
            resp = self._session.get(
                url,
                params={"host": self._host},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            if resp.status_code != 200:
                return
            commands = resp.json().get("commands", [])
        except Exception:
            return

        for cmd in commands:
            # Cada comando executa em thread separada para não bloquear o poll
            threading.Thread(
                target=self._execute,
                args=(cmd,),
                daemon=True,
                name=f"cmd-exec-{cmd.get('id')}",
            ).start()

    def _execute(self, cmd: dict):
        cmd_id    = cmd["id"]
        script    = cmd["script"]
        timeout_s = int(cmd.get("timeout_s", 30))

        logger.info("[CmdPoller] executando cmd_id=%d timeout=%ds", cmd_id, timeout_s)

        stdout = ""
        stderr = ""
        exit_code = -1

        try:
            result = subprocess.run(
                ["powershell", "-NonInteractive", "-NoProfile",
                 "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            stdout    = (result.stdout or "")[:_MAX_OUTPUT]
            stderr    = (result.stderr or "")[:_MAX_STDERR]
            exit_code = result.returncode

        except subprocess.TimeoutExpired:
            stderr    = f"[TIMEOUT] O script excedeu {timeout_s}s e foi terminado."
            exit_code = -2

        except Exception as e:
            stderr    = f"[EXEC ERROR] {type(e).__name__}: {e}"
            exit_code = -3

        self._post_result(cmd_id, stdout, stderr, exit_code)

    def _post_result(self, cmd_id: int, stdout: str, stderr: str, exit_code: int):
        url = f"{CORE_URL}/agent/commands/{cmd_id}/result"
        try:
            self._session.post(
                url,
                json={"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            logger.info("[CmdPoller] resultado enviado cmd_id=%d exit=%d", cmd_id, exit_code)
        except Exception as e:
            logger.warning("[CmdPoller] falha ao enviar resultado cmd_id=%d: %s", cmd_id, e)
