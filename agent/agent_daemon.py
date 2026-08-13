import multiprocessing
multiprocessing.freeze_support()   # DEVE ser a primeira chamada — impede fork bomb no PyInstaller/Windows

import sys
import os
import time
import ctypes
import threading
import traceback


# ---------------------------------------------------
# INSTANCIA UNICA — Named Mutex do Windows
# Impede que multiplas copias do exe corram em simultaneo.
# ---------------------------------------------------

def _acquire_single_instance_lock():
    """
    Cria um named mutex global. Se outro processo ja o detiver
    (ERROR_ALREADY_EXISTS = 183), termina imediatamente.
    O mutex e libertado automaticamente quando o processo termina.
    """
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\JarvisAgentSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:
        print("[Jarvis] Ja existe uma instancia em execucao. A terminar.")
        sys.exit(0)
    return mutex  # manter referencia para o mutex nao ser garbage collected


if __name__ == '__main__':

    _mutex = _acquire_single_instance_lock()

    import agent_core.config as cfg

    # ---------------------------------------------------
    # ARGUMENTOS
    # ---------------------------------------------------

    _args         = sys.argv[1:]
    _silent_setup = "--setup-silent" in _args
    _force_setup  = "--setup" in _args
    _web_setup    = "--setup-web" in _args
    _center_url   = next((a for a in _args if a.startswith("http")), cfg.CORE_URL)

    # Credentials file passed from installer wizard
    _creds_idx  = _args.index("--setup-creds") if "--setup-creds" in _args else -1
    _creds_file = _args[_creds_idx + 1] if _creds_idx >= 0 and _creds_idx + 1 < len(_args) else None

    _key_idx  = _args.index("--api-key") if "--api-key" in _args else -1
    _api_key  = _args[_key_idx + 1] if _key_idx >= 0 and _key_idx + 1 < len(_args) else ""


    # ---------------------------------------------------
    # SETUP WEB (janela nativa pywebview)
    # ---------------------------------------------------

    if _web_setup:
        try:
            import subprocess, webbrowser
            setup_exe = os.path.join(os.path.dirname(sys.executable), "jarvis_setup.exe")
            if os.path.isfile(setup_exe):
                proc = subprocess.run([setup_exe], check=False)
                sys.exit(proc.returncode)
            installer_py = os.path.join(os.path.dirname(__file__), "..", "agent_release", "setup_app", "installer.py")
            if os.path.isfile(installer_py):
                proc = subprocess.run([sys.executable, installer_py], check=False)
                sys.exit(proc.returncode)
            print("[Jarvis] Setup web indisponivel. A usar setup silencioso...")
            _silent_setup = True
            _web_setup    = False
        except Exception as e:
            print(f"[Jarvis] Erro ao lancar setup web: {e}")
            sys.exit(1)


    # ---------------------------------------------------
    # SETUP SILENCIOSO / INTERATIVO (sem GUI)
    # ---------------------------------------------------

    if not cfg.SETUP_COMPLETED or _force_setup or _silent_setup:
        try:
            print("[Jarvis] A iniciar setup assistido por AI...")
            from setup.setup_engine import SetupEngine
            SetupEngine(center_url=_center_url, silent=_silent_setup, creds_file=_creds_file, api_key=_api_key).run()
            cfg.reload()
        except Exception as e:
            print(f"[Jarvis] Setup falhou ({e}). A usar configuracao padrao.")
            traceback.print_exc()

    if _force_setup or _silent_setup:
        sys.exit(0)


    # ---------------------------------------------------
    # INICIALIZACAO
    # ---------------------------------------------------

    # ── Auto-setup OTel artifacts + dependências nativas ─────────────────────
    # Garante que code_modules/lib/, pywintrace, pydivert e WinDivert estão
    # prontos antes de qualquer processo ser instrumentado.
    # Só corre quando não estamos frozen (exe bundle) — no bundle os artefactos
    # são incluídos no build.
    if not getattr(sys, "frozen", False):
        try:
            from auto_instrument.setup_modules import (
                setup_python_otel, setup_pip_deps, setup_windivert,
            )
            setup_python_otel()
            setup_pip_deps()
            if cfg.WINDIVERT_ENABLED:
                setup_windivert()
            else:
                print("[Agent] WinDivert desactivado (network_probe.windivert_enabled=false) — PacketCapture usará rawsocket")
        except Exception as _setup_err:
            print(f"[Agent] auto-setup ignorado: {_setup_err}")

    from agent_core.runtime import RuntimeAgent
    from agent_core.sender_worker import SenderWorker
    from agent_core.command_poller import CommandPoller
    from agent_core.health_reporter import HealthReporter
    from utils.system_identity import build_host_identity

    runtime = RuntimeAgent()
    sender  = SenderWorker(runtime.store)

    # Solution Driver — executa comandos enviados pelo chat da IA
    _poller = None
    try:
        _host_id = build_host_identity(cfg.AGENT_VERSION).get("hostname", "unknown")
        _poller  = CommandPoller(host=_host_id)
        _poller.start()
        print(f"[Agent] Solution Driver activo para host={_host_id}")
    except Exception as _e:
        _host_id = "unknown"
        print(f"[Agent] Solution Driver não iniciado: {_e}")

    # HealthReporter — auto-diagnóstico do agente, reporta ao center a cada 5min
    try:
        _injector = getattr(runtime.probe, "_injector", None) if runtime.probe else None
        _health   = HealthReporter(
            host           =_host_id,
            otel_receiver  =runtime._otel,
            probe_manager  =runtime.probe,
            process_injector=_injector,
            command_poller =_poller,
        )
        # Injectar referência inversa no ProcessInjector para callbacks de sentinel
        if _injector is not None:
            _injector._health_reporter = _health
        _health.start()
        print(f"[Agent] HealthReporter activo — relatórios a cada 5min para {cfg.CORE_URL}")
    except Exception as _e:
        print(f"[Agent] HealthReporter não iniciado: {_e}")

    print(f"[Agent] Iniciado | perfil={cfg.SERVER_PROFILE} | version={cfg.AGENT_VERSION}")
    print(f"[Agent] Collectors: {runtime.registry.report()}")


    # ---------------------------------------------------
    # SENDER THREAD
    # ---------------------------------------------------

    def start_sender():
        print("[Sender] thread iniciada")
        while True:
            try:
                sender.run()
            except Exception:
                traceback.print_exc()
                time.sleep(2)


    threading.Thread(target=start_sender, daemon=True, name="sender").start()


    # ---------------------------------------------------
    # MAIN COLLECTION LOOP
    # ---------------------------------------------------

    while True:
        try:
            runtime.run_cycle()

            pending_count = runtime.store.count_pending()
            if pending_count > 0:
                print(f"[Runtime] queue pendente: {pending_count} frames")

        except Exception:
            print("[Runtime] erro no ciclo")
            traceback.print_exc()

        time.sleep(runtime.collection_interval_seconds)
