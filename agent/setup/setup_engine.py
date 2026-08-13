"""
SetupEngine — wizard de instalacao assistido por AI.

Fluxo:
  1. ServerScanner faz o discovery do servidor
  2. Claude Sonnet analisa o que encontrou
  3. Claude devolve: perfil, collectors recomendados, thresholds, perguntas
  4. Admin responde as perguntas (credenciais, etc.)
  5. Claude gera o jarvis_config.json final
  6. Config e persistido via agent_core.config.save_config()

Resultado: jarvis_config.json pronto para o agente arrancar.
"""

import json
import os
import sys
import datetime

from setup.server_scanner import ServerScanner
from agent_core.config import save_config


# ---------------------------------------------------
# LLM CLIENT (reutiliza FoundryClient se disponivel)
# ---------------------------------------------------

def _get_llm():
    """Tenta importar o FoundryClient. Fallback: None (modo manual)."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from brain.foundry_client import FoundryClient
        return FoundryClient()
    except Exception:
        return None


# ---------------------------------------------------
# SETUP ENGINE
# ---------------------------------------------------

class SetupEngine:

    def __init__(self, center_url: str = "http://localhost:8080", silent: bool = False,
                 creds_file: str = None, api_key: str = ""):
        self.center_url  = center_url
        self.silent      = silent
        self.creds_file  = creds_file
        self.api_key     = api_key
        self.llm         = _get_llm()
        self.scanner     = ServerScanner()

    def run(self) -> dict:
        """
        Executa o wizard completo e devolve o config gerado.
        Em modo silent (instalador headless) usa apenas defaults da AI.
        """
        self._print("\n" + "=" * 60)
        self._print("  JARVIS AGENT — CONFIGURACAO ASSISTIDA POR AI")
        self._print("=" * 60)
        self._print("\n[1/4] A analisar o servidor...\n")

        # Discovery
        scan = self.scanner.scan()
        detected = scan.get("detected", {})

        self._print_discovery(detected)

        # AI analisa e recomenda
        self._print("\n[2/4] Claude Sonnet a analisar o ambiente...\n")
        ai_recommendation = self._ask_ai(scan)

        # Apresentar recomendacao
        self._print("\n[3/4] Recomendacao:\n")
        self._print(ai_recommendation.get("summary", "Analise indisponivel."))
        self._print("")

        # Recolher credenciais — do ficheiro do wizard ou interactivamente
        credentials = {}
        if self.creds_file and os.path.isfile(self.creds_file):
            try:
                import json as _json
                with open(self.creds_file, "r", encoding="utf-8") as f:
                    credentials = _json.load(f)
                self._print(f"  Credenciais carregadas do instalador.")
            except Exception as e:
                self._print(f"  Aviso: nao foi possivel ler creds_file: {e}")
        elif not self.silent:
            credentials = self._collect_credentials(ai_recommendation, detected)

        # Gerar config final
        self._print("\n[4/4] A gerar configuracao...\n")
        config = self._build_config(scan, ai_recommendation, credentials)

        # Persistir
        path = save_config(config)
        if path:
            self._print(f"Configuracao guardada em: {path}")
        else:
            self._print("Aviso: nao foi possivel guardar a configuracao em disco.")

        self._print("\nSetup concluido. O agente esta pronto.\n")
        return config

    # ----------------------------------------
    # PRINT (respeitando modo silent)
    # ----------------------------------------

    def _print(self, msg: str):
        if not self.silent:
            try:
                print(msg)
            except Exception:
                pass

    def _print_discovery(self, detected: dict):
        dbs  = detected.get("databases", [])
        mqs  = detected.get("message_queues", [])
        webs = detected.get("web_servers", [])
        apis = detected.get("api_runtimes", [])

        for items, label in [(dbs, "Bases de dados"), (mqs, "Message queues"),
                             (webs, "Web servers"), (apis, "Runtimes API")]:
            if items:
                names = ", ".join(set(i.get("name", "?") for i in items))
                self._print(f"  [+] {label}: {names}")
            else:
                self._print(f"  [-] {label}: nao detectado")

    # ----------------------------------------
    # CHAMADA AO CLAUDE
    # ----------------------------------------

    def _ask_ai(self, scan: dict, server_description: str = "") -> dict:
        if not self.llm:
            return self._default_recommendation(scan)

        prompt = self._build_prompt(scan, server_description)

        try:
            response = self.llm.generate(prompt, max_tokens=2048)
            return self._parse_ai_response(response)
        except Exception as e:
            self._print(f"  Aviso: AI indisponivel ({e}). A usar defaults.")
            return self._default_recommendation(scan)

    def _build_prompt(self, scan: dict, server_description: str = "") -> str:
        detected = scan.get("detected", {})
        os_info  = scan.get("os", {})
        disk     = scan.get("disk", [])
        memory   = scan.get("memory", {})

        description_block = ""
        if server_description and server_description.strip():
            description_block = f"""
USER DESCRIPTION OF THIS SERVER:
\"\"\"{server_description.strip()}\"\"\"

Use this description to better understand the server's purpose and prioritize
which collectors and thresholds are most relevant.
"""

        return f"""
SYSTEM:
You are a Jarvis monitoring agent setup AI.
Analyze the server discovery data and the user's description to return
the optimal JSON configuration recommendation for this specific environment.

Respond ONLY with valid JSON. No markdown, no explanation outside the JSON.
{description_block}
SERVER DISCOVERY:
- OS: {os_info.get('system')} {os_info.get('release')}
- Memory: {memory.get('total_gb')}GB total, {memory.get('percent')}% used
- Disk: {json.dumps(disk)}
- Detected databases: {json.dumps(detected.get('databases', []))}
- Detected message queues: {json.dumps(detected.get('message_queues', []))}
- Detected web servers: {json.dumps(detected.get('web_servers', []))}
- Detected API runtimes: {json.dumps(detected.get('api_runtimes', []))}

Return this exact JSON structure:
{{
  "summary": "short description of what you found and recommend, tailored to the user's context (in Portuguese)",
  "profile": "one of: api_server | db_server | mq_server | web_server | batch_server | mixed | unknown",
  "collectors": {{
    "db_postgres": {{"enabled": true/false, "reason": "..."}},
    "db_mssql": {{"enabled": true/false, "reason": "..."}},
    "kafka": {{"enabled": true/false, "reason": "..."}},
    "web": {{"enabled": true/false, "type": "nginx|iis|apache|null", "reason": "..."}},
    "mq": {{"enabled": true/false, "type": "rabbitmq|ibmmq|activemq|null", "reason": "..."}},
    "auto_instrument": {{"enabled": true/false, "reason": "..."}}
  }},
  "thresholds": {{
    "slow_query_ms": 500,
    "long_transaction_s": 30,
    "connection_pool_pct": 80,
    "cpu_high": 90,
    "memory_high": 90,
    "disk_high": 85
  }},
  "credentials_needed": [
    {{"collector": "db_postgres", "fields": ["host", "port", "user", "password", "databases"]}}
  ]
}}
"""

    def _parse_ai_response(self, response: str) -> dict:
        try:
            # Tenta extrair JSON da resposta
            text = response.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except Exception:
            return self._default_recommendation({})

    def _default_recommendation(self, scan: dict) -> dict:
        detected = scan.get("detected", {}) if scan else {}
        return {
            "summary": "Configuracao padrao aplicada (AI indisponivel).",
            "profile": "unknown",
            "collectors": {
                "db_postgres":    {"enabled": detected.get("has_db", False)},
                "db_mssql":       {"enabled": False},
                "kafka":          {"enabled": detected.get("has_mq", False)},
                "web":            {"enabled": detected.get("has_web", False), "type": None},
                "mq":             {"enabled": False, "type": None},
                "auto_instrument": {"enabled": detected.get("has_api", False)},
            },
            "thresholds": {
                "slow_query_ms": 2000, "long_transaction_s": 60,
                "connection_pool_pct": 80, "cpu_high": 90,
                "memory_high": 90, "disk_high": 95,
            },
            "credentials_needed": [],
        }

    # ----------------------------------------
    # RECOLHA DE CREDENCIAIS (modo interativo)
    # ----------------------------------------

    def _collect_credentials(self, recommendation: dict, detected: dict) -> dict:
        credentials = {}
        needed = recommendation.get("credentials_needed", [])
        collectors = recommendation.get("collectors", {})

        for item in needed:
            collector = item.get("collector")
            if not collectors.get(collector, {}).get("enabled", False):
                continue

            self._print(f"\nCredenciais para {collector}:")
            creds = {}
            for field in item.get("fields", []):
                if "password" in field.lower():
                    val = self._input_secret(f"  {field}: ")
                else:
                    val = self._input(f"  {field}: ")
                if val:
                    creds[field] = val

            if creds:
                credentials[collector] = creds

        return credentials

    def _input(self, prompt: str) -> str:
        try:
            return input(prompt).strip()
        except Exception:
            return ""

    def _input_secret(self, prompt: str) -> str:
        try:
            import getpass
            return getpass.getpass(prompt).strip()
        except Exception:
            return self._input(prompt)

    # ----------------------------------------
    # CONSTRUIR CONFIG FINAL
    # ----------------------------------------

    def _build_config(self, scan: dict, recommendation: dict, credentials: dict) -> dict:
        rec_collectors = recommendation.get("collectors", {})
        rec_thresholds = recommendation.get("thresholds", {})

        def cred(collector: str, field: str, default=None):
            return credentials.get(collector, {}).get(field, default)

        config = {
            "version": "1.0",
            "profile": recommendation.get("profile", "unknown"),
            "setup_completed_at": datetime.datetime.utcnow().isoformat() + "Z",
            "setup_by": "claude-sonnet-4-6" if self.llm else "default",
            "server": {
                "center_url": self.center_url,
                "api_key": self.api_key,
                "agent_version": "0.4.0",
                "hostname": scan.get("hostname", ""),
                "os": scan.get("os", {}),
            },
            "collectors": {
                "core": {
                    "enabled": True,
                    "metrics_interval": 1,
                    "inventory_interval": 10,
                    "traces_interval": 5,
                    "logs_interval": 15,
                    "enable_logs": True,
                    "enable_traces": True,
                },
                "otel_receiver": {
                    "enabled": True,
                    "port": 4318,
                    "flush_interval": 5,
                },
                "auto_instrument": {
                    "enabled": rec_collectors.get("auto_instrument", {}).get("enabled", False),
                },
                "db_postgres": {
                    "enabled": rec_collectors.get("db_postgres", {}).get("enabled", False),
                    "connections": [{
                        "host": cred("db_postgres", "host", "localhost"),
                        "port": int(cred("db_postgres", "port", 5432) or 5432),
                        "user": cred("db_postgres", "user", ""),
                        "password": cred("db_postgres", "password", ""),
                        "databases": cred("db_postgres", "databases", "all"),
                    }] if rec_collectors.get("db_postgres", {}).get("enabled") else [],
                },
                "db_mssql": {
                    "enabled": rec_collectors.get("db_mssql", {}).get("enabled", False),
                    "connections": [],
                },
                "kafka": {
                    "enabled": rec_collectors.get("kafka", {}).get("enabled", False),
                    "brokers": [cred("kafka", "broker", "localhost:9092")]
                    if rec_collectors.get("kafka", {}).get("enabled") else [],
                },
                "web": {
                    "enabled": rec_collectors.get("web", {}).get("enabled", False),
                    "type": rec_collectors.get("web", {}).get("type"),
                },
                "mq": {
                    "enabled": rec_collectors.get("mq", {}).get("enabled", False),
                    "type": rec_collectors.get("mq", {}).get("type"),
                },
            },
            "thresholds": {
                "cpu_medium": 75,
                "cpu_high":   rec_thresholds.get("cpu_high", 90),
                "memory_medium": 80,
                "memory_high":   rec_thresholds.get("memory_high", 90),
                "disk_medium": 85,
                "disk_high":   rec_thresholds.get("disk_high", 95),
                "slow_query_ms":       rec_thresholds.get("slow_query_ms", 2000),
                "long_transaction_s":  rec_thresholds.get("long_transaction_s", 60),
                "connection_pool_pct": rec_thresholds.get("connection_pool_pct", 80),
            },
            "autodiscovery": {
                "enabled": True,
                "interval_seconds": 300,
                "ai_on_change": True,
            },
            "discovery_snapshot": scan.get("detected", {}),
        }

        return config
