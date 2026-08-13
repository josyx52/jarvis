"""
ExecutionBroker — coordena todas as execuções agentless do Jarvis.

Responsabilidades:
  - Fila de prioridade (P0 emergência → P3 background)
  - Governors: semáforos global, por credencial, por host, por operador
  - Transport decision: WinRM directo vs beacon outbound-polling existente
  - Worker pool com ThreadPoolExecutor
  - Métricas em tempo real

Modelo de beacon (outbound polling, igual ao Agent permanente):
  O bootstrap lança um mini-agente PowerShell na máquina remota via WinRM.
  Esse processo faz polling de SAÍDA para o Center (GET /agentless/beacon/
  {id}/next), executa o que receber, e devolve o resultado via POST. Nunca
  há conexão de entrada na máquina remota — zero problemas de firewall,
  exactamente como agent_daemon.py faz com /agent/commands/pending.

Singleton: usar get_broker() para obter a instância global.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from agentless.agentless_store import AgentlessStore
from agentless.beacon_registry import Beacon, BeaconRegistry
from agentless.remote_executor import RemoteExecutor, RemoteExecutorError

log = logging.getLogger("jarvis.broker")

# ── Configuração ────────────────────────────────────────────────────────────

_MAX_GLOBAL_WINRM    = int(os.getenv("BROKER_MAX_GLOBAL_WINRM", "50"))
_MAX_PER_CREDENTIAL  = int(os.getenv("BROKER_MAX_PER_CREDENTIAL", "15"))
_MAX_PER_HOST        = int(os.getenv("BROKER_MAX_PER_HOST", "2"))
_MAX_PER_OPERATOR    = int(os.getenv("BROKER_MAX_PER_OPERATOR", "20"))
_WORKER_POOL_SIZE    = int(os.getenv("BROKER_WORKER_POOL_SIZE", "50"))
_BEACON_TTL          = int(os.getenv("BROKER_BEACON_TTL", "600"))
_BOOTSTRAP_TIMEOUT   = int(os.getenv("BROKER_BOOTSTRAP_TIMEOUT", "20"))
_BEACON_JOB_TIMEOUT  = int(os.getenv("BROKER_BEACON_JOB_TIMEOUT", "90"))

# Detecção automática de investigação multi-passo: janela de tempo e nº
# mínimo de chamadas ao mesmo host para activar bootstrap automaticamente.
_MULTI_STEP_WINDOW_S    = int(os.getenv("BROKER_MULTI_STEP_WINDOW_S", "120"))
_MULTI_STEP_THRESHOLD   = int(os.getenv("BROKER_MULTI_STEP_THRESHOLD", "2"))

# Intervalo do sweep periódico que fecha sessões de investigação (ver
# agentless_investigation_sessions) esquecidas — máquinas que nunca mais
# recebem uma chamada depois de ficarem inactivas, e por isso não disparam
# o fecho preguiçoso normal em _upsert_session.
_SESSION_SWEEP_INTERVAL_S = int(os.getenv("BROKER_SESSION_SWEEP_INTERVAL_S", "300"))

# Prioridades
P_EMERGENCY  = 0
P_URGENT     = 1
P_NORMAL     = 2
P_BACKGROUND = 3


# ── Job ─────────────────────────────────────────────────────────────────────

@dataclass(order=True)
class Job:
    priority: int
    created_at: float = field(compare=True)
    job_id: str = field(compare=False, default_factory=lambda: uuid.uuid4().hex[:16])
    host: str = field(compare=False, default="")
    hosts: list = field(compare=False, default_factory=list)
    script: str = field(compare=False, default="")
    machine_type: str = field(compare=False, default="server")
    timeout_s: int = field(compare=False, default=60)
    operator: str = field(compare=False, default="system")
    multi_step: bool = field(compare=False, default=False)
    is_bulk: bool = field(compare=False, default=False)
    reason: str = field(compare=False, default="")


# ── Mini-agente (beacon) — script PowerShell de polling outbound ────────────
#
# Corre num processo PowerShell detached lançado via WinRM, mas a sessão
# WinRM fecha-se logo a seguir ao lançamento — o processo sobrevive sozinho.
# O mini-agente nunca abre porta nenhuma; só faz pedidos HTTP de SAÍDA para
# o Center, em tudo igual ao CommandPoller do agent_daemon.py real.
#
# Minificado (nomes curtos, sem indentação) para caber num único comando
# WinRM — limite real medido: ~3000 chars com overhead NTLM/SOAP.

_BEACON_INNER_SCRIPT = (
    "$cu='{center_url}';$bid='{beacon_id}';$k='{api_key}';$tt={ttl};$iv={poll_interval};"
    "$h=@{{'X-Jarvis-Key'=$k}};$st=[DateTime]::UtcNow;$li=$st;"
    "while(([DateTime]::UtcNow-$li).TotalSeconds -lt $tt){{"
    "try{{"
    "$r=Invoke-RestMethod -Uri \"$cu/agentless/beacon/$bid/next\" -Headers $h -Method Get -TimeoutSec 10;"
    "if($r.job_id){{"
    "$li=[DateTime]::UtcNow;"
    "$so=@();$se=@();$cd=0;"
    "try{{& {{Invoke-Expression $r.script}} 2>&1|ForEach-Object{{"
    "if($_ -is [Management.Automation.ErrorRecord]){{$se+=$_.ToString()}}else{{$so+=$_}}"
    "}}}}catch{{$se+=$_.ToString();$cd=1}};"
    "$sot=if($so.Count -gt 0){{($so|Out-String -Width 4096).Trim()}}else{{''}};"
    "$res=@{{job_id=$r.job_id;ok=($cd -eq 0);exit_code=$cd;stdout=$sot;stderr=($se -join \"`n\").Trim()}}|ConvertTo-Json -Compress -Depth 5;"
    "Invoke-RestMethod -Uri \"$cu/agentless/beacon/$bid/result\" -Headers $h -Method Post -Body $res -ContentType 'application/json' -TimeoutSec 10|Out-Null"
    "}}"
    "}}catch{{Start-Sleep -Seconds 2}};"
    "Start-Sleep -Seconds $iv"
    "}};"
    "Remove-Item -Path \"$PSCommandPath\" -Force -EA SilentlyContinue"
)


def _generate_api_key() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]


# ── ExecutionBroker ─────────────────────────────────────────────────────────

class ExecutionBroker:

    def __init__(self):
        self.registry = BeaconRegistry()
        self._report_store = AgentlessStore()

        # Governors (semáforos)
        self._sem_global = threading.Semaphore(_MAX_GLOBAL_WINRM)
        self._sem_credentials: dict[str, threading.Semaphore] = defaultdict(
            lambda: threading.Semaphore(_MAX_PER_CREDENTIAL)
        )
        self._sem_hosts: dict[str, threading.Semaphore] = defaultdict(
            lambda: threading.Semaphore(_MAX_PER_HOST)
        )
        self._sem_operators: dict[str, threading.Semaphore] = defaultdict(
            lambda: threading.Semaphore(_MAX_PER_OPERATOR)
        )

        # Worker pool
        self._pool = ThreadPoolExecutor(
            max_workers=_WORKER_POOL_SIZE,
            thread_name_prefix="broker",
        )

        # Resultados de jobs submetidos via submit()/submit_bulk()
        self._results: dict[str, dict] = {}
        self._results_lock = threading.Lock()
        self._results_events: dict[str, threading.Event] = {}

        # Resultados de jobs empurrados para beacons (aguardam polling)
        self._beacon_job_events: dict[str, threading.Event] = {}
        self._beacon_job_lock = threading.Lock()

        # Detecção automática de padrão multi-passo: regista timestamps das
        # últimas chamadas por host. A 1ª chamada a um host nunca tenta
        # bootstrap (paga só o custo de WinRM normal) — só a partir da 2ª
        # chamada dentro da janela é que vale a pena pagar o bootstrap,
        # porque aí já há sinal de investigação a sério, não um pedido único.
        self._recent_calls: dict[str, list] = defaultdict(list)
        self._recent_calls_lock = threading.Lock()

        # Métricas
        self._metrics_lock = threading.Lock()
        self._total_jobs = 0
        self._total_winrm = 0
        self._total_http = 0
        self._total_bootstrap = 0
        self._total_failures = 0
        self._active_winrm = 0

        # URL pela qual o beacon (a correr numa máquina remota) consegue
        # alcançar o Center via outbound. "localhost" NÃO serve aqui —
        # na máquina remota, localhost aponta para ela própria, não para
        # o Center. Usa BROKER_CENTER_URL se definido; senão tenta
        # descobrir o IP real desta máquina na rede do domínio.
        self._center_url = os.getenv("BROKER_CENTER_URL") or self._detect_center_url()

        # Rede de segurança para sessões de investigação (ver
        # agentless_investigation_sessions/_upsert_session em agentless_store.py):
        # o fecho "preguiçoso" só acontece quando a MESMA máquina recebe outra
        # chamada depois da janela de inactividade expirar — uma máquina que
        # nunca mais é tocada ficaria com a sessão "open" para sempre. Este
        # worker corre em segundo plano, periodicamente, a fechar essas.
        threading.Thread(
            target=self._session_sweep_worker,
            daemon=True,
            name="agentless-session-sweep",
        ).start()

    def _session_sweep_worker(self):
        while True:
            time.sleep(_SESSION_SWEEP_INTERVAL_S)
            try:
                closed = self._report_store.close_stale_sessions()
                if closed:
                    log.info("session sweep: %d sessão(ões) de investigação fechada(s) por inactividade", closed)
            except Exception:
                log.exception("falha no sweep de sessões de investigação (ignorado, tenta de novo no próximo ciclo)")

    @staticmethod
    def _detect_center_url() -> str:
        import socket
        port = os.getenv("JARVIS_API_PORT", "8080")
        try:
            # Liga a um IP externo (não envia dados) só para descobrir
            # qual interface/IP local o SO usaria para sair para a rede.
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return f"http://{local_ip}:{port}"
        except Exception:
            log.warning("não foi possível detectar IP local — beacon outbound vai falhar; define BROKER_CENTER_URL")
            return f"http://localhost:{port}"

    # ── Detecção automática de multi-step ───────────────────────────────

    def _detect_multi_step(self, host: str) -> bool:
        """Regista esta chamada e devolve True se for a 2ª (ou mais) chamada
        ao mesmo host dentro da janela — sinal de investigação a sério, não
        um pedido único. A 1ª chamada a um host nunca dispara bootstrap."""
        host_key = host.upper()
        now = time.time()
        with self._recent_calls_lock:
            calls = self._recent_calls[host_key]
            calls[:] = [t for t in calls if (now - t) < _MULTI_STEP_WINDOW_S]
            calls.append(now)
            return len(calls) >= _MULTI_STEP_THRESHOLD

    # ── Submit ───────────────────────────────────────────────────────────

    def submit(
        self,
        host: str,
        script: str,
        machine_type: str = "server",
        timeout_s: int = 60,
        priority: int = P_NORMAL,
        operator: str = "system",
        multi_step: bool = False,
        reason: str = "",
    ) -> str:
        # multi_step explícito (True) é sempre respeitado; caso contrário,
        # decide automaticamente com base no padrão de chamadas recentes.
        effective_multi_step = multi_step or self._detect_multi_step(host)
        job = Job(
            priority=priority,
            created_at=time.time(),
            host=host,
            script=script,
            machine_type=machine_type,
            timeout_s=timeout_s,
            operator=operator,
            multi_step=effective_multi_step,
            reason=reason,
        )
        event = threading.Event()
        with self._results_lock:
            self._results[job.job_id] = {"status": "pending"}
            self._results_events[job.job_id] = event
            self._total_jobs += 1

        self._pool.submit(self._execute_job, job)
        return job.job_id

    def submit_bulk(
        self,
        hosts: list[str],
        script: str,
        machine_type: str = "server",
        timeout_s: int = 120,
        priority: int = P_NORMAL,
        operator: str = "system",
        reason: str = "",
    ) -> str:
        job = Job(
            priority=priority,
            created_at=time.time(),
            hosts=hosts,
            script=script,
            machine_type=machine_type,
            timeout_s=timeout_s,
            operator=operator,
            is_bulk=True,
            reason=reason,
        )
        event = threading.Event()
        with self._results_lock:
            self._results[job.job_id] = {"status": "pending"}
            self._results_events[job.job_id] = event
            self._total_jobs += 1

        self._pool.submit(self._execute_bulk_job, job)
        return job.job_id

    # ── Wait & Get ───────────────────────────────────────────────────────

    def wait_result(self, job_id: str, timeout: int = 120) -> dict | None:
        event = self._results_events.get(job_id)
        if event is None:
            return None
        event.wait(timeout=timeout)
        with self._results_lock:
            result = self._results.pop(job_id, None)
            self._results_events.pop(job_id, None)
        return result

    def get_status(self, job_id: str) -> dict | None:
        with self._results_lock:
            return self._results.get(job_id)

    # ── Single job execution ─────────────────────────────────────────────

    def _execute_job(self, job: Job):
        with self._results_lock:
            self._results[job.job_id] = {"status": "running"}

        try:
            result = self._execute_single(
                host=job.host,
                script=job.script,
                machine_type=job.machine_type,
                timeout_s=job.timeout_s,
                multi_step=job.multi_step,
            )
            result["_transport"] = result.get("_transport", "winrm")
            result["_job_id"] = job.job_id
        except Exception as e:
            log.exception("job %s failed", job.job_id)
            result = {"error": str(e), "ok": False, "_job_id": job.job_id}
            with self._metrics_lock:
                self._total_failures += 1

        with self._results_lock:
            self._results[job.job_id] = {**result, "status": "done"}
        event = self._results_events.get(job.job_id)
        if event:
            event.set()

        # Relatório de auditoria — SEMPRE depois de desbloquear quem espera
        # o resultado. Corre numa thread à parte para nunca atrasar o
        # próximo job deste worker, e nunca deve poder falhar a entrega
        # da resposta já devolvida acima.
        duration_ms = int((time.time() - job.created_at) * 1000)
        threading.Thread(
            target=self._safe_record_execution,
            args=(job.host, job.machine_type, job.script, result, duration_ms, job.operator, job.reason),
            daemon=True,
        ).start()

    def _safe_record_execution(self, host: str, machine_type: str, script: str,
                                result: dict, duration_ms: int, operator: str,
                                reason: str = "") -> None:
        try:
            self._report_store.record_execution(
                host=host,
                machine_type=machine_type,
                script=script,
                success=bool(result.get("ok", False)),
                stdout=result.get("stdout", "") or "",
                exit_code=result.get("exit_code"),
                error_summary=result.get("stderr") or result.get("error"),
                transport=result.get("_transport"),
                duration_ms=duration_ms,
                operator=operator,
                source="chat",
                reason=reason,
            )
        except Exception:
            log.exception("falha ao gravar relatório agentless para %s (ignorado)", host)

    # ── Transport decision ───────────────────────────────────────────────

    def _execute_single(
        self,
        host: str,
        script: str,
        machine_type: str,
        timeout_s: int,
        multi_step: bool = False,
    ) -> dict:
        # 1. Beacon existente e vivo (já a fazer polling)? → empurra job, espera
        beacon = self.registry.get_by_host(host)
        if beacon:
            result = self._exec_via_beacon(beacon, script, timeout_s)
            if result is not None:
                result["_transport"] = "beacon_existing"
                result["_beacon_exec_count"] = beacon.exec_count
                return result
            self.registry.remove(beacon.beacon_id)

        # 2. Multi-step? → Bootstrap novo beacon (1 WinRM curto, outbound depois)
        if multi_step:
            beacon = self._bootstrap(host, machine_type)
            if beacon:
                result = self._exec_via_beacon(beacon, script, timeout_s)
                if result is not None:
                    result["_transport"] = "beacon_bootstrap"
                    return result

        # 3. Fallback → WinRM directo
        return self._exec_winrm_direct(host, script, machine_type, timeout_s)

    # ── Beacon job push-and-wait ─────────────────────────────────────────

    def _exec_via_beacon(self, beacon: Beacon, script: str, timeout_s: int) -> dict | None:
        """Empurra um job para a fila do beacon e espera que ele faça polling,
        execute, e devolva o resultado via POST /agentless/beacon/{id}/result."""
        job_id = uuid.uuid4().hex[:16]
        event = threading.Event()
        with self._beacon_job_lock:
            self._beacon_job_events[job_id] = event

        beacon.push_job(job_id, script)

        got = event.wait(timeout=min(timeout_s, _BEACON_JOB_TIMEOUT))
        with self._beacon_job_lock:
            self._beacon_job_events.pop(job_id, None)

        if not got:
            log.debug("beacon job %s timed out waiting for poll", job_id)
            return None

        result = beacon.pop_result(job_id)
        if result is not None:
            with self._metrics_lock:
                self._total_http += 1
        return result

    # ── Endpoints chamados pelo beacon remoto (polling outbound) ─────────

    def beacon_poll_next(self, beacon_id: str, api_key: str) -> dict | None:
        """Chamado pelo endpoint GET /agentless/beacon/{id}/next.
        Devolve o próximo job pendente, ou None se não houver nada."""
        beacon = self.registry.get_by_id(beacon_id)
        if beacon is None or beacon.api_key != api_key:
            return None
        beacon.touch()
        job = beacon.pop_job()
        return job

    def beacon_submit_result(self, beacon_id: str, api_key: str, payload: dict) -> bool:
        """Chamado pelo endpoint POST /agentless/beacon/{id}/result."""
        beacon = self.registry.get_by_id(beacon_id)
        if beacon is None or beacon.api_key != api_key:
            return False
        job_id = payload.get("job_id")
        if not job_id:
            return False
        beacon.submit_result(job_id, payload)
        with self._beacon_job_lock:
            event = self._beacon_job_events.get(job_id)
        if event:
            event.set()
        return True

    # ── Bootstrap beacon ─────────────────────────────────────────────────

    def _bootstrap(self, host: str, machine_type: str) -> Beacon | None:
        api_key = _generate_api_key()
        beacon = self.registry.register(
            host=host, api_key=api_key, machine_type=machine_type, ttl=_BEACON_TTL,
        )

        inner_script = _BEACON_INNER_SCRIPT.format(
            center_url=self._center_url,
            beacon_id=beacon.beacon_id,
            api_key=api_key,
            ttl=_BEACON_TTL,
            poll_interval=1,
        )
        b64 = base64.b64encode(inner_script.encode("utf-8")).decode("ascii")
        job_id = uuid.uuid4().hex[:8]
        temp_path = f"C:\\Windows\\Temp\\j{job_id}.ps1"

        # Lança o mini-agente via WMI Win32_Process.Create em vez de
        # Start-Process. Processos lançados com Start-Process dentro de uma
        # sessão WinRM ficam presos ao Job Object da sessão e são mortos
        # quando essa sessão fecha — mesmo com -WindowStyle Hidden. A
        # criação via WMI não herda esse Job Object, por isso o processo
        # sobrevive ao fecho da sessão WinRM (igual a um processo lançado
        # pelo Task Scheduler).
        cmdline = (
            f"powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass "
            f"-WindowStyle Hidden -File \"{temp_path}\""
        )
        bootstrap_script = (
            f"$b='{b64}';[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b))"
            f"|Out-File '{temp_path}' -Encoding utf8 -Force;"
            f"$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            f"-Arguments @{{CommandLine='{cmdline}'}};"
            f"@{{pid=$r.ProcessId;rc=$r.ReturnValue}}|ConvertTo-Json -Compress"
        )

        credential = self._credential_for(machine_type)

        acquired = self._acquire_governors(host, credential, "system")
        if not acquired:
            log.warning("bootstrap governors timeout for %s", host)
            self.registry.remove(beacon.beacon_id)
            return None

        try:
            if machine_type == "workstation":
                result = self._run_invoke_command(host, bootstrap_script, _BOOTSTRAP_TIMEOUT)
            else:
                result = self._run_pywinrm(host, bootstrap_script, machine_type)

            with self._metrics_lock:
                self._total_bootstrap += 1
        except Exception as e:
            log.debug("bootstrap failed for %s: %s", host, e)
            self.registry.remove(beacon.beacon_id)
            return None
        finally:
            self._release_governors(host, credential, "system")

        stdout = (result.get("stdout") or "").strip()
        if not stdout:
            log.debug("bootstrap empty stdout for %s, stderr: %s", host, result.get("stderr", ""))
            self.registry.remove(beacon.beacon_id)
            return None

        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            log.debug("bootstrap invalid JSON for %s: %s", host, stdout[:200])
            self.registry.remove(beacon.beacon_id)
            return None

        log.info("beacon %s launched on %s (pid=%s) — waiting for first poll",
                  beacon.beacon_id, host, data.get("pid"))

        # Espera o primeiro poll do mini-agente para confirmar que está vivo
        # (prova de outbound bem sucedida — substitui o antigo health check
        # inbound, que falhava com firewall a bloquear conexões de entrada).
        deadline = time.time() + 8
        while time.time() < deadline:
            if (time.time() - beacon.last_poll) < 8:
                return beacon
            time.sleep(0.5)

        log.debug("beacon %s never polled — bootstrap considered failed", beacon.beacon_id)
        self.registry.remove(beacon.beacon_id)
        return None

    # ── WinRM directo ────────────────────────────────────────────────────

    def _exec_winrm_direct(
        self,
        host: str,
        script: str,
        machine_type: str,
        timeout_s: int,
    ) -> dict:
        credential = self._credential_for(machine_type)

        acquired = self._acquire_governors(host, credential, "system")
        if not acquired:
            return {
                "ok": False,
                "error": "Semáforo WinRM cheio — muitas operações simultâneas. Tenta novamente.",
                "_transport": "winrm_queued_timeout",
            }

        try:
            executor = RemoteExecutor(host=host, machine_type=machine_type)
            result = executor.run(script, timeout=timeout_s)
            result["_transport"] = "winrm_direct"
            with self._metrics_lock:
                self._total_winrm += 1
            return result
        except RemoteExecutorError as e:
            return {"ok": False, "error": str(e), "_transport": "winrm_direct"}
        except Exception as e:
            return {"ok": False, "error": str(e), "_transport": "winrm_direct"}
        finally:
            self._release_governors(host, credential, "system")

    # ── Bulk execution ───────────────────────────────────────────────────

    def _execute_bulk_job(self, job: Job):
        with self._results_lock:
            self._results[job.job_id] = {"status": "running"}

        try:
            executor = RemoteExecutor(
                host=job.hosts[0] if job.hosts else "localhost",
                machine_type=job.machine_type,
            )
            result = executor.run_bulk(job.hosts, job.script, timeout=job.timeout_s)
            result["_job_id"] = job.job_id
            result["_transport"] = "winrm_bulk"
        except Exception as e:
            log.exception("bulk job %s failed", job.job_id)
            result = {
                "error": str(e),
                "results": {},
                "unreachable": job.hosts,
                "_job_id": job.job_id,
            }
            with self._metrics_lock:
                self._total_failures += 1

        with self._results_lock:
            self._results[job.job_id] = {**result, "status": "done"}
        event = self._results_events.get(job.job_id)
        if event:
            event.set()

        duration_ms = int((time.time() - job.created_at) * 1000)
        threading.Thread(
            target=self._safe_record_bulk_execution,
            args=(job.hosts, job.machine_type, job.script, result, duration_ms, job.operator, job.reason),
            daemon=True,
        ).start()

    def _safe_record_bulk_execution(self, hosts: list, machine_type: str, script: str,
                                     result: dict, duration_ms: int, operator: str,
                                     reason: str = "") -> None:
        per_host = result.get("results") or {}
        unreachable = result.get("unreachable") or []
        for host in hosts:
            r = per_host.get(host)
            try:
                if r is not None:
                    self._report_store.record_execution(
                        host=host, machine_type=machine_type, script=script,
                        success=bool(r.get("ok", False)), stdout=r.get("stdout", "") or "",
                        exit_code=r.get("exit_code"), error_summary=r.get("stderr"),
                        transport=result.get("_transport"), duration_ms=duration_ms,
                        operator=operator, source="chat", reason=reason,
                    )
                elif host in unreachable:
                    self._report_store.record_execution(
                        host=host, machine_type=machine_type, script=script,
                        success=False, stdout="", error_summary="unreachable",
                        transport=result.get("_transport"), duration_ms=duration_ms,
                        operator=operator, source="chat", reason=reason,
                    )
            except Exception:
                log.exception("falha ao gravar relatório agentless bulk para %s (ignorado)", host)

    # ── Governors ────────────────────────────────────────────────────────

    @staticmethod
    def _credential_for(machine_type: str) -> str:
        if machine_type == "workstation":
            return os.getenv("AGENTLESS_WS_USER", "sprd_passreset$")
        return os.getenv("AGENTLESS_SERVER_USER", "server_account")

    def _acquire_governors(self, host: str, credential: str, operator: str,
                           timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        remaining = timeout

        if not self._sem_global.acquire(timeout=remaining):
            return False
        with self._metrics_lock:
            self._active_winrm += 1

        remaining = max(0.1, deadline - time.time())
        if not self._sem_credentials[credential].acquire(timeout=remaining):
            self._sem_global.release()
            with self._metrics_lock:
                self._active_winrm -= 1
            return False

        remaining = max(0.1, deadline - time.time())
        host_key = host.upper()
        if not self._sem_hosts[host_key].acquire(timeout=remaining):
            self._sem_credentials[credential].release()
            self._sem_global.release()
            with self._metrics_lock:
                self._active_winrm -= 1
            return False

        return True

    def _release_governors(self, host: str, credential: str, operator: str):
        host_key = host.upper()
        self._sem_hosts[host_key].release()
        self._sem_credentials[credential].release()
        self._sem_global.release()
        with self._metrics_lock:
            self._active_winrm -= 1

    # ── Internal WinRM helpers ───────────────────────────────────────────

    @staticmethod
    def _run_invoke_command(host: str, script: str, timeout: int) -> dict:
        ps_cmd = (
            f"Invoke-Command -ComputerName '{host}' "
            f"-ScriptBlock {{ {script} }}"
        )
        r = subprocess.run(
            ["powershell.exe", "-NonInteractive", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return {
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
            "exit_code": r.returncode,
            "ok": r.returncode == 0,
        }

    @staticmethod
    def _run_pywinrm(host: str, script: str, machine_type: str) -> dict:
        import winrm
        server_user = os.getenv("AGENTLESS_SERVER_USER", "")
        server_pass = os.getenv("AGENTLESS_SERVER_PASS", "")
        session = winrm.Session(
            target=host,
            auth=(server_user, server_pass),
            transport="ntlm",
            server_cert_validation="ignore",
            read_timeout_sec=_BOOTSTRAP_TIMEOUT + 10,
            operation_timeout_sec=_BOOTSTRAP_TIMEOUT,
        )
        result = session.run_ps(script)
        return {
            "stdout": result.std_out.decode("utf-8", errors="replace").strip(),
            "stderr": result.std_err.decode("utf-8", errors="replace").strip(),
            "exit_code": result.status_code,
            "ok": result.status_code == 0,
        }

    # ── Métricas ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._metrics_lock:
            metrics = {
                "active_winrm": self._active_winrm,
                "max_winrm": _MAX_GLOBAL_WINRM,
                "total_jobs": self._total_jobs,
                "total_winrm": self._total_winrm,
                "total_http": self._total_http,
                "total_bootstrap": self._total_bootstrap,
                "total_failures": self._total_failures,
            }
        metrics["beacons"] = self.registry.status()
        metrics["pool_threads"] = self._pool._max_workers
        return metrics


# ── Singleton ────────────────────────────────────────────────────────────────

_broker: ExecutionBroker | None = None
_broker_lock = threading.Lock()


def get_broker() -> ExecutionBroker:
    global _broker
    if _broker is not None:
        return _broker
    with _broker_lock:
        if _broker is None:
            _broker = ExecutionBroker()
            log.info("ExecutionBroker initialized (max_winrm=%d, workers=%d)",
                     _MAX_GLOBAL_WINRM, _WORKER_POOL_SIZE)
        return _broker
