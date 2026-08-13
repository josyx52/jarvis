"""
ProcessInjector — Jarvis Auto-Instrumentation

Tier 1 — PEB env injection (managed runtimes):
  Python  → PYTHONPATH=code_modules/  (sitecustomize.py carrega OTel auto-instrumentation)
  Node.js → NODE_OPTIONS=--require nodejs_loader.js
  Java    → JAVA_TOOL_OPTIONS=-javaagent:opentelemetry-javaagent.jar
  .NET    → CORECLR_ENABLE_PROFILING + OTel .NET auto-instrumentation profiler

Tier 2 — DLL injection (binários nativos: nginx, postgres, redis, etc.):
  JarvisHook.dll via CreateRemoteThread + LoadLibraryA.
  Hooks WSASend/WSARecv/connect para capturar telemetria TCP/HTTP.
  Funciona em qualquer binário Windows compilado, independente da linguagem.

Para compilar JarvisHook.dll: cd native_hook && python build.py
Para baixar artefactos OTel: python setup_modules.py
"""

import ctypes
import ctypes.wintypes
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_CODE_MODULES_DIR = os.path.join(os.path.dirname(__file__), "code_modules")
_OTEL_ENDPOINT    = "http://localhost:4318"
_JARVIS_PID       = str(os.getpid())

# Directório de sentinels escrito pelo sitecustomize.py após OTel carregar com sucesso.
# {pid}      — sucesso (contém service_name)
# {pid}.fail — falha   (contém mensagem de erro)
_SENTINEL_DIR   = os.path.join(
    os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
    "JarvisAgent", "py_loaded",
)
_SENTINEL_DELAY = 30  # segundos a aguardar antes de verificar o sentinel

_k32   = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll",    use_last_error=True)

PROCESS_VM_READ           = 0x0010
PROCESS_VM_WRITE          = 0x0020
PROCESS_VM_OPERATION      = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_ALL_ACCESS        = 0x1F0FFF
MEM_COMMIT                = 0x1000
MEM_RESERVE               = 0x2000
PAGE_READWRITE            = 0x04


class _PBI(ctypes.Structure):
    _fields_ = [
        ("ExitStatus",                   ctypes.c_long),
        ("PebBaseAddress",               ctypes.c_void_p),
        ("AffinityMask",                 ctypes.c_void_p),
        ("BasePriority",                 ctypes.c_long),
        ("UniqueProcessId",              ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
    ]


class ProcessInjector:
    """
    Managed runtime PEB injection (Tier 1) + native binary DLL injection (Tier 2).
    """

    # Processos Jarvis — nunca instrumentar
    _SKIP = frozenset({"jarvisagent", "agent_daemon", "jarvis_center",
                       "jarvis_setup", "jarvis"})

    # Processos de sistema Windows — ignorar
    _SYSTEM_SKIP = frozenset({
        "svchost", "lsass", "csrss", "wininit", "winlogon", "services",
        "smss", "explorer", "dwm", "conhost", "taskmgr", "system",
        "idle", "registry", "mmc", "spoolsv", "wuauclt", "audiodg",
        "searchindexer", "wmiprvse", "dllhost", "sihost", "fontdrvhost",
        "runtimebroker", "applicationframehost", "startmenuexperiencehost",
        "ctfmon", "securityhealthservice", "msmpeng", "nissrv", "msdtc",
        "wlanext", "taskhostw", "userinit", "logonui", "winsta",
    })

    # Binários nativos que vale instrumentar via DLL injection
    _NATIVE_TARGETS = frozenset({
        # Web servers
        "nginx", "apache", "httpd", "lighttpd", "caddy", "haproxy",
        # Databases
        "postgres", "mysqld", "mongod", "redis-server", "memcached",
        "mariadb", "sqlservr",
        # Message brokers / streaming
        "rabbitmq", "kafka", "activemq",
        # Service mesh / proxy
        "envoy", "traefik", "consul", "vault", "etcd",
        # Observability
        "elasticsearch", "opensearch",
        # IIS
        "inetinfo", "w3wp",
    })

    def __init__(self, health_reporter=None):
        self._injected: set[int] = set()
        self._lock = threading.Lock()
        self._health_reporter = health_reporter

    # ── ETW callback ──────────────────────────────────────────────────────────

    def on_process_start(self, pid: int, name: str, cmdline: str):
        """Chamado pelo EtwConsumer em cada Process_Start. Non-blocking."""
        threading.Thread(
            target=self._try_inject,
            args=(pid, name, cmdline),
            daemon=True,
            name=f"injector-{pid}",
        ).start()

    # ── Scan de processos já em execução ─────────────────────────────────────

    def scan_existing_processes(self):
        """
        Varre todos os processos já em execução no startup e injeta os elegíveis.
        Chamado uma vez pelo ProbeManager após start() para cobrir apps que
        arrancaram antes do agente Jarvis.
        """
        try:
            import psutil
        except ImportError:
            logger.debug(
                "[INJECTOR] psutil não disponível — scan de processos existentes ignorado "
                "(pip install psutil para activar)"
            )
            return

        count = 0
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                pid     = proc.info["pid"]
                name    = proc.info["name"] or ""
                cmdline = " ".join(proc.info["cmdline"] or [])

                if self._is_jarvis(name) or self._is_system_process(name):
                    continue
                tech = self._technology(name, cmdline)
                if not tech and not self._is_native_target(name):
                    continue

                threading.Thread(
                    target=self._try_inject,
                    args=(pid, name, cmdline),
                    daemon=True,
                    name=f"scan-{pid}",
                ).start()
                count += 1
            except Exception:
                pass

        if count:
            logger.info(
                "[INJECTOR] scan_existing_processes: %d processo(s) elegível(eis) encontrado(s)", count
            )

    # ── Verificação de sentinel Python (30s pós-injecção) ────────────────────

    def _schedule_sentinel_check(self, pid: int, name: str):
        """Agenda verificação de sentinel 30s após injecção Python."""
        threading.Thread(
            target=self._check_sentinel,
            args=(pid, name),
            daemon=True,
            name=f"sentinel-{pid}",
        ).start()

    def _check_sentinel(self, pid: int, name: str):
        """
        Aguarda _SENTINEL_DELAY segundos e verifica se sitecustomize.py escreveu
        um sentinel de sucesso ou falha.
        Chama health_reporter para registar o resultado.
        """
        time.sleep(_SENTINEL_DELAY)

        if self._health_reporter is None:
            return

        ok_path   = os.path.join(_SENTINEL_DIR, str(pid))
        fail_path = os.path.join(_SENTINEL_DIR, str(pid) + ".fail")

        if os.path.isfile(ok_path):
            try:
                with open(ok_path, "r", encoding="utf-8") as f:
                    service = f.read().strip() or name
            except Exception:
                service = name
            self._health_reporter.record_injection_ok(pid, service, "python")
            logger.debug("[INJECTOR] Sentinel OK — pid=%d service=%s", pid, service)

        elif os.path.isfile(fail_path):
            try:
                with open(fail_path, "r", encoding="utf-8") as f:
                    reason = f.read().strip()
            except Exception:
                reason = "erro desconhecido"
            self._health_reporter.record_injection_failed(pid, name, "python", reason)
            logger.warning("[INJECTOR] Sentinel FAIL — pid=%d name=%s reason=%s", pid, name, reason)

        else:
            # Sentinel ausente — sitecustomize não carregou ou processo terminou
            if self._is_process_alive(pid):
                reason = (
                    "sitecustomize.py não registou sentinel — "
                    "OTel packages em falta em code_modules/lib/ ou erro silencioso"
                )
            else:
                reason = "processo terminou antes de sitecustomize carregar"
            self._health_reporter.record_injection_failed(pid, name, "python", reason)
            logger.debug("[INJECTOR] Sentinel ausente — pid=%d name=%s", pid, name)

    # ── Detecção de tecnologia ────────────────────────────────────────────────

    def _technology(self, name: str, cmdline: str) -> str | None:
        n   = name.lower().replace(".exe", "")
        cmd = (cmdline or "").lower()

        if n.startswith("python") or ("python" in cmd and "jarvis" not in cmd):
            return "python"
        if n in ("node",) or "node " in cmd or cmd.endswith("node"):
            return "nodejs"
        if n.startswith("java") and n not in ("javaw", "javaws"):
            return "java"
        if n in ("w3wp", "dotnet", "iisexpress") or n.startswith("dotnet"):
            return "dotnet"
        return None

    def _is_jarvis(self, name: str) -> bool:
        n = name.lower().replace(".exe", "")
        return any(s in n for s in self._SKIP)

    def _is_system_process(self, name: str) -> bool:
        n = name.lower().replace(".exe", "")
        return n in self._SYSTEM_SKIP

    def _is_native_target(self, name: str) -> bool:
        n = name.lower().replace(".exe", "")
        return n in self._NATIVE_TARGETS

    # ── Env vars por tecnologia ───────────────────────────────────────────────

    def _env_vars(self, tech: str, process_name: str = "unknown") -> dict[str, str]:
        svc_name = process_name.lower().replace(".exe", "")

        base = {
            "JARVIS_AGENT":                "1",
            "JARVIS_AGENT_PID":            _JARVIS_PID,
            "JARVIS_OTEL_ENDPOINT":        _OTEL_ENDPOINT,
            "OTEL_EXPORTER_OTLP_ENDPOINT": _OTEL_ENDPOINT,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_SERVICE_NAME":           svc_name,
            "OTEL_TRACES_EXPORTER":        "otlp",
            "OTEL_METRICS_EXPORTER":       "otlp",
            "OTEL_LOGS_EXPORTER":          "otlp",
        }

        if tech == "python":
            return {**base, "PYTHONPATH": _CODE_MODULES_DIR}

        if tech == "nodejs":
            loader = os.path.join(_CODE_MODULES_DIR, "nodejs_loader.js").replace("\\", "/")
            return {**base, "NODE_OPTIONS": f'--require "{loader}"'}

        if tech == "java":
            jar = os.path.join(_CODE_MODULES_DIR, "opentelemetry-javaagent.jar")
            opts = {**base}
            if os.path.isfile(jar):
                opts["JAVA_TOOL_OPTIONS"] = f'-javaagent:"{jar}"'
            else:
                logger.warning(
                    "[INJECTOR] opentelemetry-javaagent.jar não encontrado — "
                    "executar: python setup_modules.py"
                )
            return opts

        if tech == "dotnet":
            home     = os.path.join(_CODE_MODULES_DIR, "otel-dotnet-auto")
            native64 = os.path.join(home, "win-x64", "OpenTelemetry.AutoInstrumentation.Native.dll")
            startup  = os.path.join(home, "OpenTelemetry.AutoInstrumentation.dll")
            opts = {**base}
            if os.path.isfile(native64) and os.path.isfile(startup):
                opts.update({
                    "CORECLR_ENABLE_PROFILING": "1",
                    # GUID oficial do OTel .NET auto-instrumentation profiler
                    "CORECLR_PROFILER":         "{918728DD-259F-4A6A-AC2B-B85E1B658318}",
                    "CORECLR_PROFILER_PATH_64": native64,
                    "DOTNET_STARTUP_HOOKS":     startup,
                    "OTEL_DOTNET_AUTO_HOME":    home,
                })
            else:
                logger.warning(
                    "[INJECTOR] OTel .NET profiler não encontrado em %s — "
                    "executar: python setup_modules.py", home
                )
            return opts

        return base

    # ── Verificação de vida do processo ──────────────────────────────────────

    def _is_process_alive(self, pid: int) -> bool:
        """Retorna True se o processo ainda está a correr."""
        handle = _k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong(0)
        _k32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        _k32.CloseHandle(handle)
        return exit_code.value == 259  # STILL_ACTIVE

    def _wait_for_module(self, pid: int, dll_name: bytes, max_wait_ms: int = 2000) -> bool:
        """
        Aguarda até que uma DLL específica esteja carregada no processo alvo.
        Fix Bug 5: garante que ws2_32.dll está carregada antes de injectar os hooks.
        """
        _k32.EnumProcessModules.restype  = ctypes.c_bool
        interval_ms = 50
        elapsed     = 0
        hmodules    = (ctypes.c_void_p * 1024)()
        needed      = ctypes.c_ulong(0)

        handle = _k32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            return False

        try:
            while elapsed < max_wait_ms:
                if not self._is_process_alive(pid):
                    return False
                ok = _k32.EnumProcessModules(
                    handle, hmodules, ctypes.sizeof(hmodules), ctypes.byref(needed))
                if ok:
                    count = needed.value // ctypes.sizeof(ctypes.c_void_p)
                    name_buf = ctypes.create_string_buffer(256)
                    for i in range(count):
                        if hmodules[i]:
                            _k32.GetModuleBaseNameA(
                                handle, hmodules[i], name_buf, ctypes.sizeof(name_buf))
                            if dll_name.lower() in name_buf.value.lower():
                                return True
                time.sleep(interval_ms / 1000)
                elapsed += interval_ms
        except Exception:
            pass
        finally:
            _k32.CloseHandle(handle)

        return False

    # ── Retry logic ───────────────────────────────────────────────────────────

    _MAX_RETRIES  = 3
    _RETRY_DELAY  = 0.15   # 150ms entre tentativas

    def _inject_peb_with_retry(self, pid: int, name: str, tech: str):
        """PEB injection com retry — fix Bug 5 (race condition no timing)."""
        for attempt in range(self._MAX_RETRIES):
            if not self._is_process_alive(pid):
                logger.debug("[INJECTOR] PEB: processo %d já terminou (%s)", pid, name)
                return

            try:
                self._inject_peb(pid, self._env_vars(tech, name))
                logger.info("[INJECTOR] PEB tech=%s pid=%d name=%s attempt=%d",
                            tech, pid, name, attempt + 1)
                # Agendar verificação de sentinel para processos Python
                if tech == "python":
                    self._schedule_sentinel_check(pid, name)
                return
            except PermissionError:
                # Sem permissão não resolve com retry
                logger.debug("[INJECTOR] PEB sem permissão pid=%d (%s)", pid, name)
                return
            except Exception as e:
                logger.debug("[INJECTOR] PEB tentativa=%d falha pid=%d (%s): %s",
                             attempt + 1, pid, name, e)
                if attempt < self._MAX_RETRIES - 1:
                    time.sleep(self._RETRY_DELAY)

    def _inject_dll_with_retry(self, pid: int, name: str, hook_dll: str):
        """DLL injection com retry — fix Bug 5 (ws2_32.dll pode não estar carregada ainda)."""
        for attempt in range(self._MAX_RETRIES):
            if not self._is_process_alive(pid):
                logger.debug("[INJECTOR] DLL: processo %d já terminou (%s)", pid, name)
                return

            try:
                ok = self._inject_dll(pid, hook_dll)
                if ok:
                    logger.info("[INJECTOR] DLL native=%s pid=%d attempt=%d", name, pid, attempt + 1)
                    return
                logger.debug("[INJECTOR] DLL tentativa=%d retornou False pid=%d (%s)",
                             attempt + 1, pid, name)
            except PermissionError:
                logger.debug("[INJECTOR] DLL sem permissão pid=%d (%s)", pid, name)
                return
            except Exception as e:
                logger.debug("[INJECTOR] DLL tentativa=%d erro pid=%d (%s): %s",
                             attempt + 1, pid, name, e)

            if attempt < self._MAX_RETRIES - 1:
                time.sleep(self._RETRY_DELAY)

    # ── Ciclo de injecção ─────────────────────────────────────────────────────

    def _try_inject(self, pid: int, name: str, cmdline: str):
        with self._lock:
            if pid in self._injected:
                return
            self._injected.add(pid)

        if self._is_jarvis(name) or self._is_system_process(name):
            return

        tech = self._technology(name, cmdline)

        if tech:
            # Tier 1: managed runtime — aguardar PEB inicializar
            # Bug 5 fix: para Java e .NET aguardar mais (JVM/CLR demoram mais a arrancar)
            wait = 0.2 if tech in ("java", "dotnet") else 0.05
            time.sleep(wait)
            self._inject_peb_with_retry(pid, name, tech)

        elif self._is_native_target(name):
            # Tier 2: binário nativo — aguardar ws2_32.dll carregada
            # Bug 5 fix: servidores como nginx carregam Winsock tarde no startup
            hook_dll = self._pick_hook_dll()
            if hook_dll:
                loaded = self._wait_for_module(pid, b"ws2_32.dll", max_wait_ms=2000)
                if not loaded:
                    # ws2_32 não carregou em 2s — tentar mesmo assim (pode usar WinHTTP)
                    time.sleep(0.2)
                self._inject_dll_with_retry(pid, name, hook_dll)
            else:
                logger.debug(
                    "[INJECTOR] JarvisHook_x64/x86.dll não encontrado para %s pid=%d "
                    "— compilar: cd native_hook && python build.py", name, pid
                )

    # ── Injecção por recomendação AI ──────────────────────────────────────────

    def inject_running(self, pid: int, technology: str) -> bool:
        """Injeta num processo já a correr, por recomendação AI."""
        with self._lock:
            if pid in self._injected:
                return False
            self._injected.add(pid)

        tech = technology or "python"
        try:
            self._inject_peb(pid, self._env_vars(tech))
            logger.info("[INJECTOR] AI-targeted tech=%s pid=%d", tech, pid)
            return True
        except PermissionError:
            logger.warning("[INJECTOR] sem permissão para pid=%d", pid)
            self._injected.discard(pid)
            return False
        except Exception as e:
            logger.warning("[INJECTOR] falha pid=%d: %s", pid, e)
            self._injected.discard(pid)
            return False

    # ── PEB injection (Tier 1) ────────────────────────────────────────────────

    def _inject_peb(self, pid: int, new_vars: dict[str, str]):
        """Modifica o bloco de ambiente do processo via PEB (64-bit Windows)."""
        acc = (PROCESS_VM_READ | PROCESS_VM_WRITE |
               PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION)
        handle = _k32.OpenProcess(acc, False, pid)
        if not handle:
            raise PermissionError(ctypes.get_last_error())

        try:
            pbi = _PBI()
            _ntdll.NtQueryInformationProcess(
                handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None)
            peb = pbi.PebBaseAddress
            if not peb:
                return

            br = ctypes.c_size_t(0)

            params_ptr = ctypes.c_ulonglong(0)
            _k32.ReadProcessMemory(
                handle, peb + 0x20,
                ctypes.byref(params_ptr), 8, ctypes.byref(br))
            if not params_ptr.value:
                return

            env_ptr = ctypes.c_ulonglong(0)
            _k32.ReadProcessMemory(
                handle, params_ptr.value + 0x80,
                ctypes.byref(env_ptr), 8, ctypes.byref(br))

            env_size = ctypes.c_ulong(0)
            _k32.ReadProcessMemory(
                handle, params_ptr.value + 0x3F0,
                ctypes.byref(env_size), 4, ctypes.byref(br))

            if not env_ptr.value or env_size.value == 0:
                return

            buf = (ctypes.c_byte * env_size.value)()
            _k32.ReadProcessMemory(
                handle, env_ptr.value, buf, env_size.value, ctypes.byref(br))

            new_block = self._merge_env_block(bytes(buf), new_vars)

            new_ptr = _k32.VirtualAllocEx(
                handle, None, len(new_block),
                MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
            if not new_ptr:
                return
            _k32.WriteProcessMemory(
                handle, new_ptr, new_block, len(new_block), ctypes.byref(br))

            new_ptr_u64 = ctypes.c_ulonglong(new_ptr)
            _k32.WriteProcessMemory(
                handle, params_ptr.value + 0x80,
                ctypes.byref(new_ptr_u64), 8, ctypes.byref(br))
            new_size_u32 = ctypes.c_ulong(len(new_block))
            _k32.WriteProcessMemory(
                handle, params_ptr.value + 0x3F0,
                ctypes.byref(new_size_u32), 4, ctypes.byref(br))
        finally:
            _k32.CloseHandle(handle)

    # ── DLL injection (Tier 2) ────────────────────────────────────────────────

    def _process_is_64bit(self, pid: int) -> bool:
        """Verifica se o processo alvo é 64-bit."""
        try:
            handle = _k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
            if not handle:
                return True  # assumir 64-bit por omissão
            is_wow64 = ctypes.c_bool(False)
            _k32.IsWow64Process(handle, ctypes.byref(is_wow64))
            _k32.CloseHandle(handle)
            # IsWow64Process retorna True se é um processo 32-bit a correr em Windows 64-bit
            return not is_wow64.value
        except Exception:
            return True

    def _pick_hook_dll(self) -> str | None:
        """Escolhe a DLL correcta (x64 ou x86) conforme a arquitectura do processo."""
        # Tentar detectar arquitectura do processo alvo não é possível antes de injectar
        # — usamos a arquitectura do próprio agente como heurística
        # O método _inject_dll verifica e escolhe no momento da injecção
        dll_x64 = os.path.join(_CODE_MODULES_DIR, "JarvisHook_x64.dll")
        dll_x86 = os.path.join(_CODE_MODULES_DIR, "JarvisHook_x86.dll")
        dll_any = os.path.join(_CODE_MODULES_DIR, "JarvisHook.dll")
        if os.path.isfile(dll_x64):
            return dll_x64
        if os.path.isfile(dll_any):
            return dll_any
        return None

    def _inject_dll(self, pid: int, dll_path: str) -> bool:
        """
        Injeta JarvisHook_x64.dll ou JarvisHook_x86.dll num processo nativo
        via CreateRemoteThread + LoadLibraryA.
        Escolhe automaticamente a DLL certa conforme a arquitectura do processo.
        """
        # Seleccionar DLL correcta conforme arquitectura do processo
        is64 = self._process_is_64bit(pid)
        dll_arch = "x64" if is64 else "x86"
        dll_specific = os.path.join(_CODE_MODULES_DIR, f"JarvisHook_{dll_arch}.dll")
        if os.path.isfile(dll_specific):
            dll_path = dll_specific
        # else: usa o dll_path passado como argumento (fallback)

        dll_abs  = os.path.abspath(dll_path)
        dll_bytes = (dll_abs + "\x00").encode("utf-8")

        handle = _k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not handle:
            raise PermissionError(ctypes.get_last_error())

        try:
            # Alocar memória no processo remoto para o path da DLL
            remote_mem = _k32.VirtualAllocEx(
                handle, None, len(dll_bytes),
                MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
            if not remote_mem:
                return False

            written = ctypes.c_size_t(0)
            _k32.WriteProcessMemory(
                handle, remote_mem, dll_bytes, len(dll_bytes), ctypes.byref(written))

            # LoadLibraryA — mesmo endereço em todos processos 64-bit
            k32_handle = _k32.GetModuleHandleA(b"kernel32.dll")
            loadlib    = _k32.GetProcAddress(k32_handle, b"LoadLibraryA")
            if not loadlib:
                return False

            tid    = ctypes.c_ulong(0)
            thread = _k32.CreateRemoteThread(
                handle, None, 0,
                ctypes.c_void_p(loadlib),
                ctypes.c_void_p(remote_mem),
                0, ctypes.byref(tid)
            )
            if not thread:
                return False

            # Aguardar a DLL carregar (max 5s)
            _k32.WaitForSingleObject(thread, 5000)
            _k32.CloseHandle(thread)
            return True

        finally:
            _k32.CloseHandle(handle)

    # ── Utilitários ───────────────────────────────────────────────────────────

    def _find(self, collectors, class_name):
        for c in collectors:
            if type(c).__name__ == class_name:
                return c
        return None

    def _merge_env_block(self, current: bytes, new_vars: dict[str, str]) -> bytes:
        """
        Parseia bloco de ambiente Windows (UTF-16LE, terminado em \\0\\0),
        adiciona/funde as novas vars sem sobrescrever variáveis críticas.
        """
        text = current.decode("utf-16-le", errors="replace").rstrip("\x00")
        entries: dict[str, str] = {}

        for entry in text.split("\x00"):
            if "=" in entry:
                k = entry.split("=", 1)[0]
                entries[k.upper()] = entry

        for key, value in new_vars.items():
            ku = key.upper()
            if ku not in entries:
                entries[ku] = f"{key}={value}"
            elif ku == "PYTHONPATH":
                old_val = entries[ku].split("=", 1)[1]
                entries[ku] = f"PYTHONPATH={value};{old_val}"
            elif ku in ("NODE_OPTIONS", "JAVA_TOOL_OPTIONS"):
                old_val = entries[ku].split("=", 1)[1]
                entries[ku] = f"{key}={value} {old_val}"
            # CORECLR_*, JARVIS_*, OTEL_* só adiciona se não existir

        block = "\x00".join(entries.values()) + "\x00\x00"
        return block.encode("utf-16-le")
