"""
SolutionEngine — decide quando o Jarvis intervém e gera a solução.

Critérios de activação (TODOS devem ser cumpridos):
  1. Problem.severity in ("high", "critical")
  2. Problem aberto > MIN_OPEN_S (5 min) — não é transiente
  3. Contém pelo menos 1 alert_type que coloca a máquina em risco
     OU severity="critical" (qualquer tipo qualifica)
  4. Não existe conversa activa para este problem
  5. Série ainda não está em cooldown (1 h por problem_id)
  6. BaselineEngine confirmou a anomalia (não é período de aprendizagem)

O Claude recebe contexto completo e devolve:
  - causa raiz (resumo para analista)
  - explicação técnica
  - impacto estimado
  - acção proposta (script PowerShell)
  - risk_level: low | medium | high
  - auto_execute: true apenas se low risk E configured
"""

import json
import os
import time

import psycopg2
import requests as _requests

from brain.foundry_client import FoundryClient
from storage.solution_store import SolutionStore

_HEALTH_CHECK_INTERVAL = 300   # testar webhooks a cada 5 min
_ENABLED_CHECK_INTERVAL = 60   # ler jarvis_config a cada 60s
_WEBHOOK_TEST_TIMEOUT   = 3    # segundos por webhook no health check


def _center_conn():
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD não configurada — defina a variável de ambiente")
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        dbname=os.getenv("POSTGRES_DB", "jarvis"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=password,
    )

# ── Tipos de alerta que colocam a máquina em risco ───────────────────────────

_MACHINE_RISK_TYPES = {
    # Infra crítica
    "service_stopped",
    "disk_usage_critical",
    "disk_pressure_with_process_growth",
    "disk_low_space",
    "cpu_saturation_trend",
    "cpu_high",
    "memory_high",
    # Anomalias baseline confirmadas (high severity)
    "cpu_percent_anomaly",
    "memory_percent_anomaly",
    "disk_percent_anomaly",
    # Telemetria de aplicação
    "trace_latency_anomaly",
    "trace_error_rate_anomaly",
    "db_query_latency_anomaly",
    # Segurança comportamental
    "behavioral_anomaly",
    "non_compliant_activity",
    "privilege_abuse",
    "high_risk_user",
    # Rede
    "network_throughput_spike",
}

# Acções de baixo risco que podem ser auto-executadas
_LOW_RISK_ACTIONS = {
    "restart_service",
    "recycle_app_pool",
    "clear_temp",
    "flush_dns",
}

_MIN_OPEN_S   = 300    # 5 minutos aberto antes de intervir
_COOLDOWN_S   = 3600   # 1 hora entre soluções para o mesmo problem_id
_TIMEOUT_MIN  = int(__import__("os").getenv("JARVIS_CONV_TIMEOUT_MINUTES", "60"))


class SolutionEngine:

    def __init__(self):
        self.llm    = FoundryClient()
        self._store = SolutionStore()
        self._triggered: dict[str, float] = {}  # problem_id → last triggered ts

        # Cache: enabled flag
        self._enabled_cache:    bool  = False
        self._enabled_last_ts:  float = 0.0

        # Cache: webhook health (url → (ok: bool, ts: float))
        self._webhook_health:   dict[str, tuple[bool, float]] = {}
        self._health_last_ts:   float = 0.0

        self._http = _requests.Session()
        self._http.headers["Content-Type"] = "application/json"

    # ── Ponto de entrada ──────────────────────────────────────────────────────

    def maybe_trigger(
        self,
        host_key:      str,
        problems:      list[dict],
        snapshot:      dict,
    ):
        """
        Chamado a cada frame. Avalia cada Problem aberto e dispara
        notificação + conversa se os critérios forem cumpridos.
        """
        # Verificar se está activo e se há webhooks a responder
        if not self._is_enabled():
            return
        if not self._webhooks_available():
            return

        from engines.notification_engine import NotificationEngine
        notif = NotificationEngine(self._store)

        for problem in problems:
            pid = problem["problem_id"]

            # Verificação pós-solução: se já disparámos uma solução para
            # este problem e ele continua aberto, verificar se o comando
            # foi executado e registar que a solução não resolveu.
            if pid in self._triggered and problem.get("status") == "open":
                elapsed_since_trigger = time.time() - self._triggered[pid]
                if 600 < elapsed_since_trigger < _COOLDOWN_S:
                    if not self._store.has_active_conversation(pid):
                        print(f"[SOLUTION] Problem {pid} continua aberto {elapsed_since_trigger/60:.0f}min após solução — solução pode não ter resolvido")

            if problem.get("status") != "open":
                continue

            if not self._is_significant(problem):
                continue
            if self._in_cooldown(pid):
                continue
            if self._store.has_active_conversation(pid):
                continue

            solution = self._generate(host_key, problem, snapshot)
            if not solution:
                continue

            notif.send(
                problem   = problem,
                solution  = solution,
                snapshot  = snapshot,
                host_key  = host_key,
                timeout_m = _TIMEOUT_MIN,
            )
            self._triggered[pid] = time.time()
            print(f"[SOLUTION] Problem {pid} -> solucao gerada e notificacao enviada")

    # ── Enable / webhook health ───────────────────────────────────────────────

    def _is_enabled(self) -> bool:
        """Reads solution_driver.enabled from jarvis_config. Cached 60 s."""
        now = time.time()
        if (now - self._enabled_last_ts) < _ENABLED_CHECK_INTERVAL:
            return self._enabled_cache
        try:
            with _center_conn() as cx, cx.cursor() as cur:
                cur.execute(
                    "SELECT value FROM jarvis_config WHERE key='solution_driver.enabled'"
                )
                row     = cur.fetchone()
                enabled = (row[0] if row else "false").lower() == "true"
        except Exception:
            enabled = self._enabled_cache   # keep last known on DB error
        self._enabled_cache   = enabled
        self._enabled_last_ts = now
        if not enabled:
            print("[SOLUTION] desactivado via jarvis_config — use manage_solution_engine no chat para activar")
        return enabled

    def _webhooks_available(self) -> bool:
        """Tests at least one active webhook is responding. Cached 5 min."""
        now = time.time()
        if (now - self._health_last_ts) < _HEALTH_CHECK_INTERVAL:
            return any(ok for ok, _ in self._webhook_health.values())

        webhooks = self._store.get_active_webhooks("low")
        if not webhooks:
            print("[SOLUTION] nenhum webhook configurado — Solution Driver em pausa")
            self._health_last_ts = now
            return False

        any_ok = False
        for wh in webhooks:
            url = wh["url"]
            try:
                resp = self._http.post(
                    url,
                    json={"type": "jarvis_health_check", "version": "1.0"},
                    timeout=_WEBHOOK_TEST_TIMEOUT,
                )
                ok = True   # any HTTP response = endpoint alive
            except Exception:
                ok = False
            self._webhook_health[url] = (ok, now)
            if ok:
                any_ok = True

        self._health_last_ts = now
        if not any_ok:
            print("[SOLUTION] nenhum webhook a responder — Solution Driver em pausa")
        return any_ok

    # ── Filtro de significância ───────────────────────────────────────────────

    def _is_significant(self, problem: dict) -> bool:
        severity = problem.get("severity", "low")
        if severity not in ("high", "critical"):
            return False

        elapsed = time.time() - problem.get("opened_at", time.time())
        if elapsed < _MIN_OPEN_S:
            return False

        alert_titles = set(problem.get("alert_titles") or [])

        # Critical sempre qualifica após 5 min
        if severity == "critical":
            return True

        # High precisa de pelo menos 1 tipo de risco
        return bool(alert_titles & _MACHINE_RISK_TYPES)

    def _in_cooldown(self, problem_id: str) -> bool:
        last = self._triggered.get(problem_id, 0)
        return (time.time() - last) < _COOLDOWN_S

    # ── Geração de solução com Claude ─────────────────────────────────────────

    def _generate(
        self,
        host_key:      str,
        problem:       dict,
        snapshot:      dict,
    ) -> dict | None:

        hostname = host_key.split("::")[0]
        metrics  = snapshot.get("metrics") or {}
        procs    = snapshot.get("processes") or []

        top_procs = [
            f"{p.get('name','?')} (cpu={p.get('cpu_percent',0):.1f}% mem={round(float(p.get('resident_size',0))/1_048_576,0):.0f}MB)"
            for p in sorted(procs, key=lambda x: float(x.get("cpu_percent") or 0), reverse=True)[:5]
        ]

        duration_min = round((time.time() - problem.get("opened_at", time.time())) / 60, 1)

        prompt = f"""És o Jarvis, um sistema de observabilidade com IA.
Um problema crítico foi detectado e tens de gerar uma solução estruturada.
Responde APENAS com JSON válido (sem texto adicional).

HOST: {hostname}
PROBLEMA: {problem.get("title", "Problema detectado")}
SEVERIDADE: {problem.get("severity")}
ABERTO HÁ: {duration_min} minutos
TIPOS DE ALERTA: {", ".join(problem.get("alert_titles") or [])}

ESTADO DA MÁQUINA:
  CPU:    {metrics.get("cpu_percent", "?")}%
  Memória: {metrics.get("memory_percent", "?")}%
  Disco:  {metrics.get("disk_percent", "?")}%
  Top processos: {", ".join(top_procs) or "N/A"}

Responde APENAS com JSON:
{{
  "root_cause": "<causa raiz em 2-3 frases, linguagem clara para analista>",
  "technical_detail": "<explicação técnica detalhada>",
  "impact": "<impacto actual e potencial>",
  "action_type": "<restart_service|recycle_app_pool|clear_temp|flush_dns|kill_process|manual>",
  "risk_level": "<low|medium|high>",
  "proposed_script": "<script PowerShell completo e executável, ou empty string se manual>",
  "script_explanation": "<o que o script faz, passo a passo>",
  "auto_execute": <true apenas se risk_level=low E action_type está na lista de acções seguras>,
  "urgency": "<immediate|soon|monitor>"
}}

Critérios de risk_level:
  low    — reversível sem risco de perda de dados (restart serviço, limpar temp)
  medium — pode afectar disponibilidade temporariamente
  high   — risco de perda de dados ou indisponibilidade prolongada

auto_execute deve ser true APENAS se risk_level="low" e action_type em:
  restart_service, recycle_app_pool, clear_temp, flush_dns"""

        try:
            raw     = self.llm.generate(prompt, max_tokens=1500, temperature=0.1)
            result  = self._parse_json(raw)
            result["raw_llm"] = raw
            return result
        except Exception as e:
            print(f"[SOLUTION] generate falhou para {host_key}: {e}")
            return None

    @staticmethod
    def _parse_json(raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        return json.loads(raw[start:end])
