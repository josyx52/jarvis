"""
NotificationEngine — envia notificações ricas para webhooks configurados.

Fluxo de push:
  1. SolutionEngine detecta problema significativo → chama send()
  2. send() cria conversa na DB e POST para todos os webhooks activos
  3. Payload inclui callback_url único por conversa
  4. Analista responde via callback_url → ConversationEngine processa
  5. send_reply() envia resposta do Jarvis de volta a todos os webhooks

O payload é assinado com HMAC-SHA256 se o webhook tiver secret configurado.
"""

import hashlib
import hmac
import json
import os
import time

import requests

from storage.solution_store import SolutionStore

_CENTER_URL = os.getenv("JARVIS_CENTER_URL", "http://localhost:8000")
_SEND_TIMEOUT = 10   # segundos por webhook


def _sign_payload(secret: str | None, body: bytes) -> str | None:
    if not secret:
        return None
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


class NotificationEngine:

    def __init__(self, store: SolutionStore):
        self._store   = store
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"

    # ── Notificação inicial ───────────────────────────────────────────────────

    def send(
        self,
        problem:   dict,
        solution:  dict,
        snapshot:  dict,
        host_key:  str,
        timeout_m: int = 60,
    ):
        severity     = problem.get("severity", "high")
        webhooks     = self._store.get_active_webhooks(severity)
        webhook_urls = self._urls_for_host(webhooks, host_key)

        if not webhook_urls:
            print(f"[NOTIFICATION] nenhum webhook configurado para severity={severity}")
            return

        # Filtrar métricas relevantes do snapshot
        metrics = snapshot.get("metrics") or {}
        procs   = snapshot.get("processes") or []
        top_cpu = sorted(procs, key=lambda p: float(p.get("cpu_percent") or 0), reverse=True)[:5]
        machine_state = {
            "cpu_percent":    metrics.get("cpu_percent"),
            "memory_percent": metrics.get("memory_percent"),
            "disk_percent":   metrics.get("disk_percent"),
            "top_processes": [
                {
                    "name":    p.get("name"),
                    "pid":     p.get("pid"),
                    "cpu_pct": round(float(p.get("cpu_percent") or 0), 1),
                    "mem_mb":  round(float(p.get("resident_size") or 0) / 1_048_576, 0),
                }
                for p in top_cpu
            ],
            "services_down": [
                s.get("name") for s in (snapshot.get("services") or [])
                if (s.get("state") or s.get("status") or "").lower() in ("stopped", "down")
            ][:10],
        }

        conv_id = self._store.create_conversation(
            problem_id       = problem["problem_id"],
            host             = host_key,
            solution_summary = solution.get("root_cause", ""),
            proposed_script  = solution.get("proposed_script", ""),
            risk_level       = solution.get("risk_level", "medium"),
            snapshot         = machine_state,
            webhook_urls     = webhook_urls,
            timeout_minutes  = timeout_m,
        )

        duration_min = round((time.time() - problem.get("opened_at", time.time())) / 60, 1)

        payload = {
            "notification_id": f"notif_{int(time.time())}",
            "jarvis_version":  "1.0",
            "timestamp":       _utc_now(),
            "problem": {
                "id":             problem["problem_id"],
                "title":          problem.get("title", "Problema detectado"),
                "severity":       problem.get("severity"),
                "host":           host_key.split("::")[0],
                "host_key":       host_key,
                "opened_at":      _ts_to_iso(problem.get("opened_at")),
                "duration_minutes": duration_min,
                "alert_types":    problem.get("alert_titles") or [],
            },
            "machine_state": machine_state,
            "investigation": {
                "root_cause":       solution.get("root_cause"),
                "technical_detail": solution.get("technical_detail"),
                "impact":           solution.get("impact"),
                "urgency":          solution.get("urgency", "soon"),
            },
            "proposed_action": {
                "action_type":       solution.get("action_type"),
                "risk_level":        solution.get("risk_level"),
                "script":            solution.get("proposed_script", ""),
                "script_explanation": solution.get("script_explanation"),
                "auto_executed":     False,
            },
            "conversation": {
                "id":           conv_id,
                "callback_url": f"{_CENTER_URL}/webhook/reply/{conv_id}",
                "expires_in_minutes": timeout_m,
                "instructions": (
                    "Para responder, faça POST para callback_url com: "
                    '{"message": "o que pretende dizer ao Jarvis"}. '
                    "O Jarvis responderá neste webhook."
                ),
            },
        }

        # Auto-executar se low risk e configurado
        if (
            solution.get("auto_execute")
            and solution.get("risk_level") == "low"
            and solution.get("proposed_script")
        ):
            cmd_id = self._store.save_command(
                host       = host_key,
                script     = solution["proposed_script"],
                risk_level = "low",
                problem_id = problem["problem_id"],
                conv_id    = conv_id,
            )
            payload["proposed_action"]["auto_executed"] = True
            payload["proposed_action"]["command_id"]    = cmd_id
            print(f"[NOTIFICATION] acção low-risk auto-executada cmd_id={cmd_id}")

        self._post_to_all(webhooks, webhook_urls, payload)

        # Mensagem inicial na conversa
        self._store.add_message(
            conv_id, "jarvis",
            f"{solution.get('root_cause', '')} — {solution.get('impact', '')}",
            metadata={"type": "initial_notification"},
        )

    # ── Resposta de conversa ──────────────────────────────────────────────────

    def send_reply(self, conv: dict, jarvis_message: str):
        """Envia resposta do Jarvis de volta a todos os webhooks da conversa."""
        webhook_urls = conv.get("webhook_urls") or []
        if not webhook_urls:
            return

        payload = {
            "type":            "conversation_update",
            "conversation_id": conv["id"],
            "problem_id":      conv["problem_id"],
            "host":            conv["host"].split("::")[0],
            "role":            "jarvis",
            "message":         jarvis_message,
            "timestamp":       _utc_now(),
            "conversation_status": conv.get("status"),
            "callback_url":    f"{_CENTER_URL}/webhook/reply/{conv['id']}",
        }

        # Obter webhooks com secrets para assinar
        webhooks_map = {
            w["url"]: w
            for w in self._store.get_active_webhooks("low")
        }

        for url in webhook_urls:
            wh = webhooks_map.get(url, {"url": url})
            self._post_one(wh, payload)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _urls_for_host(self, webhooks: list[dict], host_key: str) -> list[str]:
        hostname = host_key.split("::")[0].lower()
        urls     = []
        for wh in webhooks:
            host_filter = wh.get("host_filter")
            if host_filter:
                if not any(hostname.startswith(h.lower()) for h in host_filter):
                    continue
            urls.append(wh["url"])
        return urls

    def _post_to_all(self, webhooks: list[dict], urls: list[str], payload: dict):
        webhooks_map = {w["url"]: w for w in webhooks}
        for url in urls:
            wh = webhooks_map.get(url, {"url": url})
            self._post_one(wh, payload)

    def _post_one(self, webhook: dict, payload: dict):
        url    = webhook["url"]
        secret = webhook.get("secret")
        body   = json.dumps(payload).encode()
        headers = {}
        sig = _sign_payload(secret, body)
        if sig:
            headers["X-Jarvis-Signature"] = sig

        try:
            resp = self._session.post(
                url, data=body, headers=headers, timeout=_SEND_TIMEOUT
            )
            print(f"[NOTIFICATION] POST {url} -> {resp.status_code}")
        except Exception as e:
            print(f"[NOTIFICATION] falha {url}: {e}")


# ── Utilidades ────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _ts_to_iso(ts) -> str:
    if ts is None:
        return ""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
