"""
SetupServer — wizard de configuração web-based do Jarvis Agent.

Fluxo:
  1. Abre browser em http://localhost:9393
  2. Wizard: URL → Scan em tempo real → Descoberta → IA → Credenciais → Guardar → Done
  3. Escreve jarvis_config.json e sinaliza conclusão ao processo pai

Requisitos: apenas stdlib Python (http.server, json, threading, webbrowser)
"""

import json
import os
import queue
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

WIZARD_PORT = 9393

# -------------------------------------------------------
# Estado partilhado entre requests
# -------------------------------------------------------

_state = {
    "scan_result": None,
    "ai_result":   None,
    "config_path": None,
}
_scan_queue  = queue.Queue()
_done_event  = threading.Event()
_center_url  = ""


# -------------------------------------------------------
# SCAN em background (emite eventos para SSE)
# -------------------------------------------------------

def _run_scan(center_url: str):
    global _center_url
    _center_url = center_url

    def emit(evt):
        _scan_queue.put(evt)

    emit({"type": "progress", "message": "A verificar hostname e sistema operativo...", "percent": 2})

    try:
        # Adiciona o path do agente para imports relativos
        agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)

        from setup.server_scanner import ServerScanner, KNOWN_PORTS
        scanner = ServerScanner()

        hostname = scanner._get_hostname()
        os_info  = scanner._get_os_info()

        emit({"type": "progress", "message": f"Host: {hostname} | OS: {os_info.get('system','')} {os_info.get('release','')}", "percent": 5})

        # -- Portas --
        open_ports = []
        ports_list = list(KNOWN_PORTS.items())
        for i, (port, info) in enumerate(ports_list):
            pct = 8 + int((i / len(ports_list)) * 48)
            emit({"type": "progress",
                  "message": f"A verificar {info['name']} (porta {port})...",
                  "percent": pct})
            if scanner._port_open(port):
                open_ports.append({"port": port, **info})
                emit({"type": "found", "service": info["name"], "port": port})
            time.sleep(0.04)

        # -- Processos --
        emit({"type": "progress", "message": "A verificar processos em execução...", "percent": 62})
        processes = scanner._scan_processes()
        for p in processes:
            emit({"type": "found", "service": p.get("name","?"), "port": None})

        # -- Serviços Windows --
        emit({"type": "progress", "message": "A verificar serviços Windows...", "percent": 76})
        services = scanner._scan_services()

        # -- Disco / Memória --
        emit({"type": "progress", "message": "A verificar disco e memória...", "percent": 86})
        disk   = scanner._get_disk_info()
        memory = scanner._get_memory_info()

        # -- Conectividade ao Center --
        center_ok = False
        if center_url:
            emit({"type": "progress", "message": f"A testar conectividade com {center_url}...", "percent": 93})
            try:
                parsed = urlparse(center_url)
                host = parsed.hostname or "localhost"
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                s = socket.create_connection((host, port), timeout=4)
                s.close()
                center_ok = True
                emit({"type": "found", "service": "Jarvis Center", "port": port})
            except Exception:
                emit({"type": "warn", "message": f"Center em {center_url} não respondeu — continua na mesma"})

        emit({"type": "progress", "message": "A consolidar resultados...", "percent": 97})
        detected = scanner._classify(open_ports, processes, services)

        result = {
            "hostname":   hostname,
            "os":         os_info,
            "open_ports": open_ports,
            "processes":  processes,
            "services":   services,
            "disk":       disk,
            "memory":     memory,
            "detected":   detected,
            "center_reachable": center_ok,
        }

        _state["scan_result"] = result
        emit({"type": "complete", "result": result})

    except Exception as e:
        import traceback
        emit({"type": "error", "message": f"Erro no scan: {e}"})
        emit({"type": "complete", "result": {"detected": {}, "error": str(e)}})


# -------------------------------------------------------
# HTTP HANDLER
# -------------------------------------------------------

class _WizardHandler(BaseHTTPRequestHandler):

    # ---------- routing ----------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", ""):
            self._serve_html()
        elif path == "/api/scan-events":
            self._handle_scan_events()
        elif path == "/api/status":
            self._json({"done": _done_event.is_set(),
                        "config_path": _state.get("config_path")})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        handlers = {
            "/api/start-scan":  self._handle_start_scan,
            "/api/analyze":     self._handle_analyze,
            "/api/save-config": self._handle_save_config,
            "/api/done":        self._handle_done,
        }
        fn = handlers.get(path)
        if fn:
            fn()
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    # ---------- helpers ----------

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass  # silencia logs de acesso

    # ---------- GET handlers ----------

    def _serve_html(self):
        query = ""
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
        params   = parse_qs(query)
        c_url    = params.get("center_url", [""])[0]
        html     = WIZARD_HTML.replace("__CENTER_URL__", c_url)
        body     = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_scan_events(self):
        qs      = self.path.split("?", 1)[1] if "?" in self.path else ""
        params  = parse_qs(qs)
        c_url   = params.get("url", [""])[0]

        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection",    "keep-alive")
        self._cors()
        self.end_headers()

        # Inicia scan em thread separada
        threading.Thread(target=_run_scan, args=(c_url,), daemon=True).start()

        try:
            while True:
                try:
                    evt  = _scan_queue.get(timeout=25)
                    data = "data: " + json.dumps(evt, ensure_ascii=False) + "\n\n"
                    self.wfile.write(data.encode("utf-8"))
                    self.wfile.flush()
                    if evt.get("type") == "complete":
                        break
                except queue.Empty:
                    self.wfile.write(b"data: {\"type\":\"ping\"}\n\n")
                    self.wfile.flush()
        except Exception:
            pass

    # ---------- POST handlers ----------

    def _handle_start_scan(self):
        body  = self._read_body()
        c_url = body.get("center_url", "")
        threading.Thread(target=_run_scan, args=(c_url,), daemon=True).start()
        self._json({"status": "started"})

    def _handle_analyze(self):
        body     = self._read_body()
        scan     = body.get("scan") or _state.get("scan_result") or {}
        c_url    = body.get("center_url", _center_url)

        try:
            agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if agent_dir not in sys.path:
                sys.path.insert(0, agent_dir)

            from setup.setup_engine import SetupEngine
            engine = SetupEngine(center_url=c_url, silent=True)
            rec    = engine._ask_ai(scan)
            _state["ai_result"] = rec
            self._json(rec)
        except Exception as e:
            fallback = {
                "summary": "Análise com IA indisponível. Configuração padrão aplicada.",
                "profile": "unknown",
                "collectors": {
                    "db_postgres":    {"enabled": False, "reason": "IA indisponível"},
                    "db_mssql":       {"enabled": False, "reason": "IA indisponível"},
                    "kafka":          {"enabled": False, "reason": "IA indisponível"},
                    "web":            {"enabled": False, "type": None, "reason": "IA indisponível"},
                    "auto_instrument":{"enabled": False, "reason": "IA indisponível"},
                },
                "thresholds": {
                    "slow_query_ms": 2000, "long_transaction_s": 60,
                    "connection_pool_pct": 80, "cpu_high": 90,
                    "memory_high": 90, "disk_high": 95,
                },
                "credentials_needed": [],
            }
            _state["ai_result"] = fallback
            self._json(fallback)

    def _handle_save_config(self):
        body     = self._read_body()
        c_url    = body.get("center_url", _center_url)
        scan     = body.get("scan")     or _state.get("scan_result") or {}
        ai       = body.get("ai_recommendation") or _state.get("ai_result") or {}
        creds    = body.get("credentials", {})

        try:
            agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if agent_dir not in sys.path:
                sys.path.insert(0, agent_dir)

            from setup.setup_engine import SetupEngine
            from agent_core.config import save_config

            engine  = SetupEngine(center_url=c_url, silent=True)
            config  = engine._build_config(scan, ai, creds)
            path    = save_config(config)

            _state["config_path"] = path

            active = [k for k, v in config.get("collectors", {}).items()
                      if isinstance(v, dict) and v.get("enabled") and k not in ("core", "otel_receiver")]

            _done_event.set()

            self._json({
                "success":         True,
                "config_path":     path or r"C:\ProgramData\JarvisAgent\jarvis_config.json",
                "profile":         config.get("profile", "unknown"),
                "version":         config.get("version", "1.0"),
                "active_collectors": active,
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json({"success": False, "error": str(e)}, status=500)

    def _handle_done(self):
        _done_event.set()
        self._json({"ok": True})


# -------------------------------------------------------
# ARRANQUE DO SERVIDOR
# -------------------------------------------------------

def start_wizard(center_url: str = "", wait: bool = True) -> str | None:
    """
    Inicia o servidor do wizard e abre o browser.
    Devolve o caminho para o config gerado (ou None).
    Se wait=True, bloqueia até o wizard terminar.
    """
    server = HTTPServer(("127.0.0.1", WIZARD_PORT), _WizardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://localhost:{WIZARD_PORT}"
    if center_url:
        from urllib.parse import quote
        url += f"?center_url={quote(center_url)}"

    print(f"[Setup] A abrir wizard em {url}")
    try:
        webbrowser.open(url)
    except Exception:
        print(f"[Setup] Abre manualmente: {url}")

    if wait:
        _done_event.wait()
        server.shutdown()
        return _state.get("config_path")

    return None


# -------------------------------------------------------
# HTML WIZARD (template)
# -------------------------------------------------------

WIZARD_HTML = r"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jarvis Agent — Setup</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#080b14;--surface:#111827;--card:#1c2333;--border:#2d3748;
  --primary:#6366f1;--primary-light:#818cf8;--primary-glow:rgba(99,102,241,.15);
  --success:#10b981;--success-bg:rgba(16,185,129,.12);--success-border:rgba(16,185,129,.35);
  --warning:#f59e0b;--danger:#ef4444;
  --text:#f1f5f9;--muted:#94a3b8;--dim:#4b5563;
  --radius:14px;--radius-sm:9px;--shadow:0 8px 32px rgba(0,0,0,.5);
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{
  font-family:'Inter',system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  display:flex;align-items:center;justify-content:center;
  min-height:100vh;
  background-image:
    radial-gradient(ellipse at 15% 5%,rgba(99,102,241,.07) 0%,transparent 55%),
    radial-gradient(ellipse at 85% 95%,rgba(16,185,129,.05) 0%,transparent 55%);
}
.wrap{width:100%;max-width:780px;padding:24px 16px}

/* ---- header ---- */
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px}
.logo{display:flex;align-items:center;gap:12px}
.logo-box{
  width:42px;height:42px;border-radius:11px;
  background:linear-gradient(135deg,#6366f1,#7c3aed);
  display:flex;align-items:center;justify-content:center;font-size:22px;
}
.logo-name{font-size:18px;font-weight:700;letter-spacing:-.4px}
.logo-name span{color:var(--primary-light)}

/* ---- step dots ---- */
.dots{display:flex;align-items:center;gap:0}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);transition:all .3s}
.dot.active{width:26px;border-radius:4px;background:var(--primary);box-shadow:0 0 10px rgba(99,102,241,.5)}
.dot.done{background:var(--success)}
.dline{width:18px;height:2px;background:var(--dim);transition:background .3s}
.dline.done{background:var(--success)}

/* ---- card ---- */
.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:22px;padding:44px 48px;
  box-shadow:var(--shadow);min-height:420px;
  position:relative;overflow:hidden;
}
@media(max-width:600px){.card{padding:28px 20px}}

.card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,#6366f1,#7c3aed,#06b6d4);
  border-radius:22px 22px 0 0;
}

/* ---- steps ---- */
.step{display:none}
.step.on{display:block;animation:fadeUp .3s ease}
@keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}

/* ---- typography ---- */
.ttl{font-size:26px;font-weight:700;letter-spacing:-.5px;margin-bottom:6px}
.sub{color:var(--muted);font-size:14px;line-height:1.7;margin-bottom:28px}

/* ---- input ---- */
.fld{margin-bottom:18px}
.lbl{display:block;font-size:12px;font-weight:600;color:var(--muted);
     letter-spacing:.4px;text-transform:uppercase;margin-bottom:7px}
.inp{
  width:100%;background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:13px 16px;
  color:var(--text);font-family:inherit;font-size:15px;
  transition:border .2s,box-shadow .2s;outline:none;
}
.inp:focus{border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-glow)}
.inp::placeholder{color:var(--dim)}
.err{color:var(--danger);font-size:12px;margin-top:5px;display:none}

/* ---- info box ---- */
.info{
  background:rgba(99,102,241,.07);border:1px solid rgba(99,102,241,.2);
  border-radius:var(--radius-sm);padding:13px 16px;font-size:13px;
  color:var(--muted);line-height:1.6;margin-top:6px
}
.info code{background:rgba(255,255,255,.06);padding:1px 5px;border-radius:4px;font-size:12px}

/* ---- buttons ---- */
.btn-p{
  background:linear-gradient(135deg,#6366f1,#7c3aed);
  color:#fff;border:none;border-radius:var(--radius-sm);
  padding:13px 32px;font-size:15px;font-weight:600;
  cursor:pointer;font-family:inherit;transition:all .2s;
}
.btn-p:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(99,102,241,.4)}
.btn-p:disabled{opacity:.45;cursor:not-allowed;transform:none;box-shadow:none}
.btn-s{
  background:transparent;color:var(--muted);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:13px 24px;font-size:15px;
  font-weight:500;cursor:pointer;font-family:inherit;transition:all .2s;
}
.btn-s:hover{background:var(--card);color:var(--text)}

/* ---- footer ---- */
.foot{display:flex;justify-content:space-between;align-items:center;margin-top:30px}
.step-lbl{font-size:12px;color:var(--dim)}

/* ---- welcome tags ---- */
.tags{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:28px}
.tag{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;
     background:var(--card);border:1px solid var(--border);border-radius:99px;
     font-size:12px;color:var(--muted)}
.tag.ok{background:var(--success-bg);border-color:var(--success-border);color:var(--success)}

/* ---- scan ---- */
.scan-ctr{text-align:center;padding:10px 0 4px}
.orb{
  width:84px;height:84px;border-radius:50%;
  background:linear-gradient(135deg,#6366f1,#7c3aed);
  margin:0 auto 22px;position:relative;
  display:flex;align-items:center;justify-content:center;font-size:34px;
  animation:pulse 2s ease-in-out infinite;
}
.orb::after{
  content:'';position:absolute;inset:-14px;border-radius:50%;
  border:2px solid rgba(99,102,241,.3);
  animation:ripple 2s ease-out infinite;
}
@keyframes pulse{0%,100%{box-shadow:0 0 20px rgba(99,102,241,.4)}50%{box-shadow:0 0 44px rgba(99,102,241,.7)}}
@keyframes ripple{0%{transform:scale(1);opacity:1}100%{transform:scale(1.7);opacity:0}}
.scan-msg{font-size:14px;color:var(--muted);min-height:22px;margin-bottom:14px}
.pbar-wrap{background:var(--card);border-radius:99px;height:5px;overflow:hidden;margin-bottom:16px}
.pbar{height:100%;background:linear-gradient(90deg,#6366f1,#7c3aed);width:0;border-radius:99px;transition:width .4s ease}
.slog{
  background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);
  padding:14px;font-family:'Courier New',monospace;font-size:11.5px;
  max-height:170px;overflow-y:auto;text-align:left;
}
.slog-e{padding:2px 0;color:var(--muted);animation:fi .3s ease}
.slog-e.found{color:var(--success)}
.slog-e.warn{color:var(--warning)}
.slog-e.error{color:var(--danger)}
@keyframes fi{from{opacity:0}to{opacity:1}}

/* ---- services grid ---- */
.svc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:14px;margin-top:4px}
.svc-card{
  background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px 14px;text-align:center;animation:fadeUp .4s ease;
}
.svc-card.det{border-color:var(--success-border);background:linear-gradient(135deg,rgba(16,185,129,.05),var(--card))}
.svc-ico{font-size:32px;margin-bottom:10px}
.svc-nm{font-size:13px;font-weight:600;margin-bottom:4px}
.svc-cat{font-size:11px;color:var(--dim);margin-bottom:8px}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-size:11px;font-weight:600}
.b-ok{background:var(--success-bg);color:var(--success)}
.b-no{background:var(--card);color:var(--dim);border:1px solid var(--border)}

.empty{text-align:center;padding:40px 0;color:var(--muted)}
.empty .eico{font-size:48px;margin-bottom:14px}

/* ---- AI ---- */
.ai-think{
  display:flex;align-items:center;gap:14px;
  background:var(--primary-glow);border:1px solid rgba(99,102,241,.3);
  border-radius:var(--radius-sm);padding:16px 20px;margin-bottom:20px;
}
.ai-think .dtdots span{
  display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--primary-light);margin:0 2px;
  animation:blink 1.2s ease-in-out infinite;
}
.ai-think .dtdots span:nth-child(2){animation-delay:.2s}
.ai-think .dtdots span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.3;transform:scale(.8)}40%{opacity:1;transform:scale(1.2)}}

.ai-box{background:var(--primary-glow);border:1px solid rgba(99,102,241,.3);
         border-radius:var(--radius);padding:18px 20px;margin-bottom:22px}
.ai-lbl{font-size:10px;font-weight:700;letter-spacing:1px;
         color:var(--primary-light);text-transform:uppercase;margin-bottom:8px}
.ai-box p{font-size:13.5px;line-height:1.75;color:var(--text)}

.rec-list{list-style:none}
.rec-item{display:flex;align-items:center;gap:14px;padding:12px 0;border-bottom:1px solid var(--border)}
.rec-item:last-child{border-bottom:none}
.rec-ico{width:36px;height:36px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.rec-ico.on{background:var(--success-bg)}
.rec-ico.off{background:var(--card);border:1px solid var(--border)}
.rec-nm{font-size:14px;font-weight:600;margin-bottom:2px}
.rec-rs{font-size:12px;color:var(--muted)}
.sec-hdr{font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;
          color:var(--muted);margin-bottom:14px;margin-top:4px}

/* ---- credentials ---- */
.cred-sec{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:14px}
.cred-ttl{display:flex;align-items:center;gap:10px;font-size:14px;font-weight:600;
           margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border)}
.cgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.full{grid-column:1/-1}

/* ---- done ---- */
.done-ctr{text-align:center;padding:10px 0}
.chk{
  width:82px;height:82px;background:var(--success-bg);
  border:2px solid var(--success);border-radius:50%;
  display:flex;align-items:center;justify-content:center;font-size:38px;
  margin:0 auto 22px;animation:pop .5s cubic-bezier(.175,.885,.32,1.275);
}
@keyframes pop{from{transform:scale(0);opacity:0}to{transform:scale(1);opacity:1}}
.done-tags{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-bottom:22px}
.cfg-sum{
  background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  padding:18px;font-family:'Courier New',monospace;font-size:12px;
  max-height:190px;overflow-y:auto;text-align:left;
}
.cfg-ln{padding:2px 0;color:var(--muted)}
.cfg-k{color:var(--primary-light)}
.cfg-v{color:var(--success)}
</style>
</head>
<body>
<div class="wrap">
  <!-- Header -->
  <div class="hdr">
    <div class="logo">
      <div class="logo-box">⚡</div>
      <div class="logo-name">Jarvis <span>Agent</span></div>
    </div>
    <div class="dots" id="dotsEl"></div>
  </div>

  <!-- Card -->
  <div class="card">

    <!-- S1: Welcome -->
    <div class="step on" id="s1">
      <div style="text-align:center;padding:16px 0 8px">
        <div style="font-size:68px;margin-bottom:18px">🤖</div>
        <div class="ttl">Bem-vindo ao Jarvis Agent</div>
        <div class="sub" style="max-width:480px;margin:8px auto 0">
          O assistente vai analisar este servidor com IA, detectar os serviços em execução
          e gerar uma configuração personalizada de monitorização.
        </div>
        <div class="tags">
          <div class="tag">🔍 Descoberta automática</div>
          <div class="tag">🤖 Claude Sonnet 4.6</div>
          <div class="tag">⚙️ Config inteligente</div>
          <div class="tag">🔐 Dados locais</div>
        </div>
      </div>
    </div>

    <!-- S2: Center URL -->
    <div class="step" id="s2">
      <div class="ttl">Servidor Jarvis Center</div>
      <div class="sub">Introduz o endereço do Jarvis Center. O assistente vai testar a conectividade antes de continuar.</div>
      <div class="fld">
        <label class="lbl">URL do Jarvis Center</label>
        <input id="cUrl" class="inp" type="text" placeholder="http://192.168.1.100:8080" value="__CENTER_URL__">
        <div class="err" id="urlErr"></div>
      </div>
      <div class="info">
        💡 Exemplo: <code>http://10.0.0.1:8080</code> — o Jarvis Center deve estar em execução.
      </div>
    </div>

    <!-- S3: Scanning -->
    <div class="step" id="s3">
      <div class="ttl">A analisar o servidor</div>
      <div class="sub">A verificar portas, processos e serviços em execução neste servidor...</div>
      <div class="scan-ctr">
        <div class="orb">⚡</div>
        <div class="scan-msg" id="scanMsg">A iniciar...</div>
        <div class="pbar-wrap"><div class="pbar" id="pbar"></div></div>
        <div class="slog" id="slog"></div>
      </div>
    </div>

    <!-- S4: Discovery -->
    <div class="step" id="s4">
      <div class="ttl">Serviços Descobertos</div>
      <div class="sub">Serviços detectados neste servidor. Os activos serão monitorizados com collectors dedicados.</div>
      <div class="svc-grid" id="svcGrid"></div>
      <div class="empty" id="noSvc" style="display:none">
        <div class="eico">🔍</div>
        <p>Nenhum serviço especializado detectado.<br><span style="font-size:13px">Apenas métricas de sistema serão coletadas.</span></p>
      </div>
    </div>

    <!-- S5: AI Analysis -->
    <div class="step" id="s5">
      <div class="ttl">Análise por Claude Sonnet</div>
      <div class="sub">A IA analisa o ambiente e determina a configuração ideal para este servidor.</div>
      <div class="ai-think" id="aiThink">
        <div style="font-size:24px">🤖</div>
        <div>
          <div style="font-size:13px;font-weight:600;margin-bottom:3px">Claude Sonnet está a pensar...</div>
          <div style="font-size:12px;color:var(--muted)">A analisar serviços detectados e a gerar recomendação</div>
        </div>
        <div class="dtdots" style="margin-left:auto"><span></span><span></span><span></span></div>
      </div>
      <div id="aiRes" style="display:none">
        <div class="ai-box"><div class="ai-lbl">🤖 Recomendação Claude Sonnet</div><p id="aiTxt"></p></div>
        <div class="sec-hdr">Collectors a activar</div>
        <ul class="rec-list" id="recList"></ul>
      </div>
    </div>

    <!-- S6: Credentials -->
    <div class="step" id="s6">
      <div class="ttl">Configurar Credenciais</div>
      <div class="sub">Introduz as credenciais de acesso para os serviços que serão monitorizados. Guardadas localmente de forma segura.</div>
      <div id="credForms"></div>
      <div class="empty" id="noCreds" style="display:none">
        <div class="eico">✅</div>
        <p>Nenhuma credencial necessária.<br><span style="font-size:13px">O agente usará os acessos padrão para os serviços detectados.</span></p>
      </div>
    </div>

    <!-- S7: Done -->
    <div class="step" id="s7">
      <div class="done-ctr">
        <div class="chk">✓</div>
        <div class="ttl">Configuração Concluída!</div>
        <div class="sub" style="max-width:500px;margin:8px auto 22px">
          O Jarvis Agent foi configurado com sucesso.<br>O serviço será iniciado automaticamente pelo instalador.
        </div>
        <div class="done-tags">
          <div class="tag ok" id="dTagProfile">✓ Perfil detectado</div>
          <div class="tag ok" id="dTagCenter">✓ Center configurado</div>
        </div>
        <div class="cfg-sum" id="cfgSum"></div>
        <div class="info" style="margin-top:14px;text-align:left">
          📁 Config: <code id="cfgPath">C:\ProgramData\JarvisAgent\jarvis_config.json</code><br>
          📊 Collectors: <span id="cfgColls">—</span>
        </div>
      </div>
    </div>

  </div><!-- /card -->

  <!-- Footer -->
  <div class="foot">
    <button class="btn-s" id="btnBack" onclick="back()" style="visibility:hidden">← Voltar</button>
    <div class="step-lbl" id="stepLbl">Passo 1 de 7</div>
    <button class="btn-p" id="btnNext" onclick="next()">Começar →</button>
  </div>
</div>

<script>
const N=7;
let cur=1,scanData=null,aiData=null;

// build dots
function mkDots(){
  const el=document.getElementById('dotsEl');el.innerHTML='';
  for(let i=1;i<=N;i++){
    const d=document.createElement('div');
    d.className='dot'+(i===cur?' active':i<cur?' done':'');
    el.appendChild(d);
    if(i<N){const l=document.createElement('div');l.className='dline'+(i<cur?' done':'');el.appendChild(l);}
  }
}
function ui(){
  mkDots();
  document.getElementById('stepLbl').textContent=`Passo ${cur} de ${N}`;
  document.getElementById('btnBack').style.visibility=cur>1?'visible':'hidden';
  const bn=document.getElementById('btnNext');
  if(cur===1) bn.textContent='Começar →';
  else if(cur===6) bn.textContent='Aplicar Configuração ✓';
  else if(cur===7) bn.textContent='Fechar';
  else bn.textContent='Continuar →';
}
function show(n){
  document.querySelectorAll('.step').forEach(s=>s.classList.remove('on'));
  document.getElementById('s'+n).classList.add('on');
  cur=n;ui();
}

// center url from query
(function(){
  const u=new URLSearchParams(location.search).get('center_url')||'';
  if(u) document.getElementById('cUrl').value=u;
})();

async function next(){
  if(cur===1){show(2);}
  else if(cur===2){
    const url=document.getElementById('cUrl').value.trim();
    const e=document.getElementById('urlErr');
    if(!url){e.textContent='Introduz o URL do Jarvis Center.';e.style.display='block';return;}
    if(!/^https?:\/\//i.test(url)){e.textContent='URL inválido. Deve começar com http:// ou https://';e.style.display='block';return;}
    e.style.display='none';
    show(3);startScan(url);
  }
  else if(cur===3){/* wait for auto-advance */}
  else if(cur===4){show(5);startAI();}
  else if(cur===5){show(6);renderCreds();}
  else if(cur===6){await doSave();}
  else if(cur===7){fetch('/api/done',{method:'POST'}).catch(()=>{});setTimeout(()=>window.close(),400);}
}
function back(){if(cur>1&&cur!==3)show(cur-1);}

// ---- SCAN ----
function addLog(txt,cls=''){
  const el=document.getElementById('slog');
  const d=document.createElement('div');d.className='slog-e'+(cls?' '+cls:'');
  d.textContent=txt;el.appendChild(d);el.scrollTop=el.scrollHeight;
}
function startScan(url){
  document.getElementById('btnNext').disabled=true;
  const evs=new EventSource('/api/scan-events?url='+encodeURIComponent(url));
  evs.onmessage=e=>{
    try{
      const d=JSON.parse(e.data);
      if(d.type==='ping') return;
      if(d.type==='progress'){
        document.getElementById('scanMsg').textContent=d.message;
        document.getElementById('pbar').style.width=(d.percent||0)+'%';
        addLog('› '+d.message);
      }
      if(d.type==='found') addLog('✓ ENCONTRADO: '+d.service+(d.port?' (porta '+d.port+')':''),'found');
      if(d.type==='warn')  addLog('⚠ '+d.message,'warn');
      if(d.type==='error') addLog('✗ '+d.message,'error');
      if(d.type==='complete'){
        scanData=d.result;evs.close();
        document.getElementById('scanMsg').textContent='✓ Análise concluída!';
        document.getElementById('pbar').style.width='100%';
        document.getElementById('btnNext').disabled=false;
        setTimeout(()=>{show(4);renderSvcs();},900);
      }
    }catch(err){}
  };
  evs.onerror=()=>{evs.close();document.getElementById('scanMsg').textContent='Erro no scan.';document.getElementById('btnNext').disabled=false;};
}

// ---- SERVICES ----
const SVC_META={
  'PostgreSQL':{ico:'🐘',cat:'Base de dados'},
  'SQL Server':{ico:'🗃️',cat:'Base de dados'},
  'Oracle':{ico:'🏛️',cat:'Base de dados'},
  'MySQL':{ico:'🐬',cat:'Base de dados'},
  'MongoDB':{ico:'🍃',cat:'Base de dados'},
  'Redis':{ico:'⚡',cat:'Cache'},
  'Kafka':{ico:'📨',cat:'Message Queue'},
  'RabbitMQ':{ico:'🐇',cat:'Message Queue'},
  'IBM MQ':{ico:'📦',cat:'Message Queue'},
  'ActiveMQ':{ico:'🔄',cat:'Message Queue'},
  'Nginx':{ico:'🌐',cat:'Web Server'},
  'Apache':{ico:'🪶',cat:'Web Server'},
  'IIS':{ico:'🏢',cat:'Web Server'},
  'HAProxy':{ico:'⚖️',cat:'Web Server'},
  'Caddy':{ico:'🚀',cat:'Web Server'},
  'Python':{ico:'🐍',cat:'Runtime API'},
  'FastAPI/Uvicorn':{ico:'⚡',cat:'Runtime API'},
  'Java':{ico:'☕',cat:'Runtime API'},
  'Node.js':{ico:'🟢',cat:'Runtime API'},
  '.NET':{ico:'🔷',cat:'Runtime API'},
  'Jarvis Center':{ico:'🎯',cat:'Monitoring'},
};
function renderSvcs(){
  const g=document.getElementById('svcGrid'),ns=document.getElementById('noSvc');
  g.innerHTML='';
  if(!scanData||!scanData.detected){ns.style.display='block';return;}
  const det=scanData.detected;
  const all=[...(det.databases||[]),...(det.message_queues||[]),...(det.web_servers||[]),...(det.api_runtimes||[])];
  const seen=new Set(),uniq=all.filter(s=>{if(seen.has(s.name))return false;seen.add(s.name);return true;});
  if(!uniq.length){ns.style.display='block';return;}
  ns.style.display='none';
  uniq.forEach(svc=>{
    const m=SVC_META[svc.name]||{ico:'⚙️',cat:'Serviço'};
    const c=document.createElement('div');c.className='svc-card det';
    c.innerHTML=`<div class="svc-ico">${m.ico}</div><div class="svc-nm">${svc.name}</div><div class="svc-cat">${m.cat}</div>${svc.port?`<div style="font-size:11px;color:var(--muted);margin-bottom:8px">Porta ${svc.port}</div>`:''}<span class="badge b-ok">● Activo</span>`;
    g.appendChild(c);
  });
}

// ---- AI ----
async function startAI(){
  document.getElementById('btnNext').disabled=true;
  document.getElementById('aiThink').style.display='flex';
  document.getElementById('aiRes').style.display='none';
  try{
    const r=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({scan:scanData,center_url:document.getElementById('cUrl').value.trim()})});
    aiData=await r.json();
    document.getElementById('aiThink').style.display='none';
    document.getElementById('aiRes').style.display='block';
    document.getElementById('aiTxt').textContent=aiData.summary||'Análise concluída.';
    const list=document.getElementById('recList');list.innerHTML='';
    const CMETA={
      db_postgres:{ico:'🐘',nm:'PostgreSQL Monitoring'},
      db_mssql:{ico:'🗃️',nm:'SQL Server Monitoring'},
      kafka:{ico:'📨',nm:'Kafka Consumer Lag'},
      web:{ico:'🌐',nm:'Web Server Metrics'},
      mq:{ico:'🐇',nm:'Message Queue Metrics'},
      auto_instrument:{ico:'🔍',nm:'Auto-instrumentação OTel'},
    };
    // core always on
    const li0=document.createElement('li');li0.className='rec-item';
    li0.innerHTML=`<div class="rec-ico on">⚙️</div><div><div class="rec-nm">Sistema (CPU, RAM, Disco, Rede)</div><div class="rec-rs">Sempre activo — métricas base do servidor</div></div><span class="badge b-ok">Activo</span>`;
    list.appendChild(li0);
    Object.entries(aiData.collectors||{}).forEach(([k,v])=>{
      const m=CMETA[k]||{ico:'⚙️',nm:k};const on=v.enabled;
      const li=document.createElement('li');li.className='rec-item';
      li.innerHTML=`<div class="rec-ico ${on?'on':'off'}">${m.ico}</div><div><div class="rec-nm">${m.nm}</div><div class="rec-rs">${v.reason||''}</div></div><span class="badge ${on?'b-ok':'b-no'}">${on?'Activo':'Inactivo'}</span>`;
      list.appendChild(li);
    });
    document.getElementById('btnNext').disabled=false;
  }catch(e){
    document.getElementById('aiThink').style.display='none';
    document.getElementById('aiRes').style.display='block';
    document.getElementById('aiTxt').textContent='IA indisponível. Configuração padrão aplicada.';
    document.getElementById('btnNext').disabled=false;
  }
}

// ---- CREDENTIALS ----
const CRED_FIELDS={
  db_postgres:{ico:'🐘',nm:'PostgreSQL',fields:['host','port','user','password','databases']},
  db_mssql:{ico:'🗃️',nm:'SQL Server',fields:['host','port','user','password']},
  kafka:{ico:'📨',nm:'Kafka',fields:['broker']},
};
const PH={host:'localhost',port:'5432',user:'jarvis_monitor',password:'••••••••',databases:'all',broker:'localhost:9092'};
function portFor(c,f){return f==='port'?(c==='db_postgres'?'5432':c==='db_mssql'?'1433':'9092'):PH[f]||'';}
function renderCreds(){
  const ctr=document.getElementById('credForms'),nc=document.getElementById('noCreds');
  ctr.innerHTML='';
  if(!aiData){nc.style.display='block';return;}
  const needed=(aiData.credentials_needed||[]).filter(n=>aiData.collectors?.[n.collector]?.enabled);
  if(!needed.length){nc.style.display='block';return;}
  nc.style.display='none';
  needed.forEach(item=>{
    const m=CRED_FIELDS[item.collector]||{ico:'⚙️',nm:item.collector,fields:item.fields};
    const sec=document.createElement('div');sec.className='cred-sec';
    let rows='';
    (item.fields||m.fields||[]).forEach(f=>{
      const isP=f.toLowerCase().includes('pass');
      const isFull=f==='databases'||(item.fields||[]).length===1;
      rows+=`<div class="fld${isFull?' full':''}"><label class="lbl">${f.charAt(0).toUpperCase()+f.slice(1).replace('_',' ')}</label><input type="${isP?'password':'text'}" id="cr-${item.collector}-${f}" class="inp" placeholder="${portFor(item.collector,f)}" autocomplete="off"></div>`;
    });
    sec.innerHTML=`<div class="cred-ttl"><span style="font-size:20px">${m.ico}</span> Credenciais ${m.nm}</div><div class="cgrid">${rows}</div>`;
    ctr.appendChild(sec);
  });
}
function collectCreds(){
  const needed=(aiData?.credentials_needed||[]).filter(n=>aiData?.collectors?.[n.collector]?.enabled);
  const out={};
  needed.forEach(item=>{
    const c={};
    (item.fields||[]).forEach(f=>{
      const el=document.getElementById(`cr-${item.collector}-${f}`);
      if(el&&el.value.trim()) c[f]=el.value.trim();
    });
    if(Object.keys(c).length) out[item.collector]=c;
  });
  return out;
}

// ---- SAVE ----
async function doSave(){
  document.getElementById('btnNext').disabled=true;
  try{
    const r=await fetch('/api/save-config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        center_url:document.getElementById('cUrl').value.trim(),
        scan:scanData,ai_recommendation:aiData,credentials:collectCreds()
      })});
    const d=await r.json();
    if(d.success){show(7);renderDone(d);}
    else{alert('Erro: '+(d.error||'desconhecido'));document.getElementById('btnNext').disabled=false;}
  }catch(e){alert('Erro: '+e.message);document.getElementById('btnNext').disabled=false;}
}
function renderDone(d){
  document.getElementById('dTagProfile').textContent='✓ Perfil: '+(d.profile||'unknown');
  document.getElementById('dTagCenter').textContent='✓ Center: '+document.getElementById('cUrl').value.trim();
  document.getElementById('cfgPath').textContent=d.config_path||'C:\\ProgramData\\JarvisAgent\\jarvis_config.json';
  document.getElementById('cfgColls').textContent=(d.active_collectors||['core']).join(', ');
  const lines=[['version',d.version||'1.0'],['profile',d.profile||'?'],['center_url',document.getElementById('cUrl').value.trim()],['agent_version','0.3.0'],...(d.active_collectors||[]).map(s=>['collector.'+s,'enabled'])];
  document.getElementById('cfgSum').innerHTML=lines.map(([k,v])=>`<div class="cfg-ln"><span class="cfg-k">${k}</span>: <span class="cfg-v">"${v}"</span></div>`).join('');
}
ui();
</script>
</body>
</html>
"""
