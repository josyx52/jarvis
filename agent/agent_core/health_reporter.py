"""
health_reporter.py — Auto-diagnóstico do agente Jarvis.

O agente reporta periodicamente a saúde dos seus próprios componentes ao center,
como um paciente que descreve os seus sintomas ao médico.

Componentes monitorizados:
  otel_receiver    — porta 4318 a escutar?
  etw              — pywintrace disponível? Consumer activo?
  packet_capture   — WinDivert ou rawsocket?
  dll_injection    — JarvisHook_x64.dll compilada?
  python_otel      — lib/ bundled com packages OTel?
  java_otel        — opentelemetry-javaagent.jar presente?
  dotnet_otel      — OTel .NET profiler presente?
  solution_driver  — CommandPoller activo?
  sitecustomize    — injecções falhadas recentes?

Frequência: a cada 5 minutos (configurável).
Destino: POST {center}/agent/health
"""

import logging
import os
import threading
import time
import urllib.request
import json

from agent_core import config as cfg

logger = logging.getLogger(__name__)

_REPORT_INTERVAL = 300   # segundos entre relatórios
_SENTINEL_DIR    = os.path.join(
    os.environ.get("PROGRAMDATA", "C:\\ProgramData"),
    "JarvisAgent", "py_loaded",
)
_CODE_MODULES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "auto_instrument", "code_modules",
)


class HealthReporter:
    """
    Corre em background e reporta a saúde do agente.
    Aceita referências opcionais para componentes vivos para leitura de estado.
    """

    def __init__(self, host: str,
                 otel_receiver=None,
                 probe_manager=None,
                 process_injector=None,
                 command_poller=None):
        self._host             = host
        self._otel_receiver    = otel_receiver
        self._probe_manager    = probe_manager
        self._process_injector = process_injector
        self._command_poller   = command_poller
        self._running          = False
        self._thread           = None

        # Falhas de injecção reportadas pelo ProcessInjector
        # Lista de dicts: {pid, name, tech, injected_at, reason}
        self._failed_injections: list[dict] = []
        self._ok_injections:     list[dict] = []
        self._inj_lock = threading.Lock()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            daemon=True,
            name="health-reporter",
        )
        self._thread.start()
        logger.info("[Health] Reporter iniciado para host=%s", self._host)

    def stop(self):
        self._running = False

    # ── Registo de eventos de injecção ────────────────────────────────────────

    def record_injection_ok(self, pid: int, name: str, tech: str):
        with self._inj_lock:
            self._ok_injections.append({
                "pid": pid, "name": name, "tech": tech, "ts": time.time()
            })
            # Manter apenas os últimos 100
            if len(self._ok_injections) > 100:
                self._ok_injections = self._ok_injections[-100:]

    def record_injection_failed(self, pid: int, name: str, tech: str, reason: str):
        with self._inj_lock:
            self._failed_injections.append({
                "pid": pid, "name": name, "tech": tech,
                "reason": reason, "ts": time.time()
            })
            if len(self._failed_injections) > 200:
                self._failed_injections = self._failed_injections[-200:]

    # ── Loop principal ─────────────────────────────────────────────────────────

    def _loop(self):
        # Primeiro relatório após 30s (dar tempo ao agente para arrancar)
        time.sleep(30)
        while self._running:
            try:
                self._report()
            except Exception as e:
                logger.debug("[Health] Erro no report: %s", e)
            time.sleep(_REPORT_INTERVAL)

    # ── Colecção de estado ────────────────────────────────────────────────────

    def _collect(self) -> dict:
        components = {}

        # 1. OtelReceiver
        components["otel_receiver"] = self._check_otel_receiver()

        # 2. ETW
        components["etw"] = self._check_etw()

        # 3. PacketCapture
        components["packet_capture"] = self._check_packet_capture()

        # 4. DLL injection
        components["dll_injection"] = self._check_dll_injection()

        # 5. Python OTel (lib/ bundled)
        components["python_otel"] = self._check_python_otel()

        # 6. Java OTel
        components["java_otel"] = self._check_java_otel()

        # 7. .NET OTel
        components["dotnet_otel"] = self._check_dotnet_otel()

        # 8. Solution Driver
        components["solution_driver"] = self._check_solution_driver()

        # 9. Injecções Python (sitecustomize sentinels)
        components["sitecustomize"] = self._check_sitecustomize()

        return components

    def _check_otel_receiver(self) -> dict:
        if self._otel_receiver is not None:
            try:
                pending = len(getattr(self._otel_receiver, "_pending", []))
                return {"status": "ok", "detail": f"porta 4318 activa, {pending} spans pendentes"}
            except Exception:
                pass
        # Fallback: tentar ligar à porta
        import socket
        try:
            s = socket.create_connection(("127.0.0.1", 4318), timeout=1)
            s.close()
            return {"status": "ok", "detail": "porta 4318 a responder"}
        except Exception:
            return {"status": "error", "detail": "porta 4318 não está a responder"}

    def _check_etw(self) -> dict:
        if self._probe_manager is not None:
            etw = getattr(self._probe_manager, "_etw", None)
            if etw is not None:
                available = getattr(etw, "_available", False)
                running   = getattr(etw, "_running", False)
                if available and running:
                    return {"status": "ok", "detail": "ETW activo — Kernel-Process + DNS + TCPIP"}
                if not available:
                    return {
                        "status": "missing",
                        "detail": "pywintrace não instalado — ETW desactivado",
                        "fix":    "pip install pywintrace",
                    }
                return {"status": "degraded", "detail": "pywintrace disponível mas ETW não iniciado"}
        try:
            import etw  # noqa: F401
            return {"status": "warning", "detail": "pywintrace disponível mas ProbeManager não iniciado"}
        except ImportError:
            return {
                "status": "missing",
                "detail": "pywintrace não instalado — sem ETW kernel events",
                "fix":    "pip install pywintrace",
            }

    def _check_packet_capture(self) -> dict:
        if self._probe_manager is not None:
            cap = getattr(self._probe_manager, "_capture", None)
            if cap is not None:
                mode = getattr(cap, "mode", "unknown")
                if mode == "windivert":
                    return {"status": "ok", "detail": "WinDivert activo — captura NDIS completa"}
                if mode == "rawsocket":
                    return {
                        "status": "degraded",
                        "detail": "Modo rawsocket — sem visibilidade em loopback",
                        "fix":    "Instalar WinDivert.dll + WinDivert64.sys na pasta do agente",
                    }
        # Verificar DLL em C:\ProgramData\JarvisAgent\
        _wd_dir = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "JarvisAgent")
        if os.path.isfile(os.path.join(_wd_dir, "WinDivert.dll")):
            return {"status": "warning", "detail": "WinDivert.dll presente mas ProbeManager não iniciado"}
        return {
            "status": "missing",
            "detail": "WinDivert.dll não encontrado — PacketCapture em modo rawsocket",
            "fix":    "python setup_modules.py (descarrega WinDivert do center)",
        }

    def _check_dll_injection(self) -> dict:
        dll_x64 = os.path.join(_CODE_MODULES, "JarvisHook_x64.dll")
        dll_x86 = os.path.join(_CODE_MODULES, "JarvisHook_x86.dll")
        dll_any = os.path.join(_CODE_MODULES, "JarvisHook.dll")

        has64 = os.path.isfile(dll_x64)
        has86 = os.path.isfile(dll_x86)
        has_any = os.path.isfile(dll_any)

        if has64 and has86:
            s64 = os.path.getsize(dll_x64) // 1024
            s86 = os.path.getsize(dll_x86) // 1024
            return {"status": "ok", "detail": f"JarvisHook_x64.dll ({s64} KB) + x86 ({s86} KB) prontos"}
        if has64 or has_any:
            name = "JarvisHook_x64.dll" if has64 else "JarvisHook.dll"
            return {"status": "degraded", "detail": f"{name} presente mas falta versão x86"}
        return {
            "status": "missing",
            "detail": "JarvisHook DLLs não compiladas — sem injecção em binários nativos (nginx, postgres, redis...)",
            "fix":    "cd auto_instrument/native_hook && python build.py (requer Visual Studio + vcpkg)",
        }

    def _check_python_otel(self) -> dict:
        marker = os.path.join(_CODE_MODULES, "lib", "opentelemetry", "sdk", "__init__.py")
        lib_dir = os.path.join(_CODE_MODULES, "lib")
        if os.path.isfile(marker):
            size_mb = sum(
                os.path.getsize(os.path.join(d, f))
                for d, _, files in os.walk(lib_dir)
                for f in files
            ) / (1024 * 1024)
            return {"status": "ok", "detail": f"OTel Python bundled ({size_mb:.1f} MB em code_modules/lib/)"}
        return {
            "status": "missing",
            "detail": "OTel packages não instalados em code_modules/lib/",
            "fix":    "python setup_modules.py --python-only",
        }

    def _check_java_otel(self) -> dict:
        jar = os.path.join(_CODE_MODULES, "opentelemetry-javaagent.jar")
        if os.path.isfile(jar):
            size_mb = os.path.getsize(jar) / (1024 * 1024)
            return {"status": "ok", "detail": f"opentelemetry-javaagent.jar ({size_mb:.1f} MB)"}
        return {
            "status": "missing",
            "detail": "opentelemetry-javaagent.jar não encontrado — sem instrumentação Java",
            "fix":    "python setup_modules.py --java-only",
        }

    def _check_dotnet_otel(self) -> dict:
        native = os.path.join(_CODE_MODULES, "otel-dotnet-auto", "win-x64",
                              "OpenTelemetry.AutoInstrumentation.Native.dll")
        if os.path.isfile(native):
            return {"status": "ok", "detail": "OTel .NET profiler presente (win-x64)"}
        return {
            "status": "missing",
            "detail": "OTel .NET profiler não encontrado — sem instrumentação .NET",
            "fix":    "python setup_modules.py --dotnet-only",
        }

    def _check_solution_driver(self) -> dict:
        if self._command_poller is not None:
            running = getattr(self._command_poller, "_running", False)
            host    = getattr(self._command_poller, "_host", "?")
            if running:
                return {"status": "ok", "detail": f"CommandPoller activo para host={host}"}
            return {"status": "error", "detail": "CommandPoller não está a correr"}
        return {"status": "warning", "detail": "CommandPoller não referenciado"}

    def _check_sitecustomize(self) -> dict:
        with self._inj_lock:
            failed = list(self._failed_injections)
            ok     = list(self._ok_injections)

        # Falhas recentes (últimos 10 min)
        cutoff = time.time() - 600
        recent_failed = [f for f in failed if f["ts"] > cutoff]
        recent_ok     = [o for o in ok     if o["ts"] > cutoff]

        if recent_failed:
            names = ", ".join(f"{f['name']}(pid={f['pid']})" for f in recent_failed[:5])
            return {
                "status":  "error",
                "detail":  f"{len(recent_failed)} injecção(ões) falhada(s): {names}",
                "failed":  recent_failed[:10],
                "ok":      recent_ok[:10],
            }
        if recent_ok:
            return {
                "status": "ok",
                "detail": f"{len(recent_ok)} processo(s) instrumentado(s) com sucesso",
                "ok":     recent_ok[:10],
            }
        return {
            "status": "ok",
            "detail": "Sem injecções recentes (normal se nenhum novo processo Python arrancou)",
        }

    # ── Envio do relatório ────────────────────────────────────────────────────

    def _report(self):
        components = self._collect()

        # Determinar estado geral
        statuses = [c.get("status", "ok") for c in components.values()]
        if "error"   in statuses: overall = "degraded"
        elif "missing" in statuses: overall = "degraded"
        else: overall = "ok"

        payload = {
            "host":        self._host,
            "overall":     overall,
            "components":  components,
            "instrumentation": {
                "failed": [f for f in self._failed_injections
                           if f["ts"] > time.time() - 600],
                "ok":     [o for o in self._ok_injections
                           if o["ts"] > time.time() - 600],
            },
            "timestamp": time.time(),
        }

        # Enviar para center
        url = f"{cfg.CORE_URL.rstrip('/')}/agent/health"
        body = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key":    cfg.API_KEY,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                pass
            logger.debug("[Health] Relatório enviado — overall=%s", overall)
        except Exception as e:
            logger.debug("[Health] Falha ao enviar relatório: %s", e)

        # Log local das componentes com problema
        for name, comp in components.items():
            if comp.get("status") not in ("ok", "warning"):
                logger.warning(
                    "[Health] %s status=%s — %s",
                    name, comp.get("status"), comp.get("detail", "")
                )
                fix = comp.get("fix")
                if fix:
                    logger.warning("[Health]   FIX: %s", fix)
