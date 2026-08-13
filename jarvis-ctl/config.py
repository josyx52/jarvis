"""
jarvis-ctl configuration loader.

Reads C:\\ProgramData\\JarvisCtl\\jarvis-ctl.conf (INI format).
Environment variables override file values.

Lookup order per key:
  1. Environment variable  (ex: JARVIS_DB_PASS)
  2. Config file value
  3. Built-in default (never a real secret)
"""

import configparser
import os
from pathlib import Path

# Localizações candidatas — a primeira que existir é usada
_CONFIG_PATHS = [
    Path(os.environ.get("JARVIS_CTL_CONFIG", "")) if os.environ.get("JARVIS_CTL_CONFIG") else None,
    Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "JarvisCtl" / "jarvis-ctl.conf",
    Path(os.environ.get("APPDATA", "")) / "JarvisCtl" / "jarvis-ctl.conf" if os.environ.get("APPDATA") else None,
    Path(__file__).parent / "jarvis-ctl.conf",
]

_cfg = None


def _load() -> configparser.ConfigParser:
    global _cfg
    if _cfg is not None:
        return _cfg

    _cfg = configparser.ConfigParser()

    for p in _CONFIG_PATHS:
        if p and p.exists():
            _cfg.read(p, encoding="utf-8")
            break

    return _cfg


def _get(section: str, key: str, env_var: str, default: str = "") -> str:
    """Env var overrides file; file overrides default."""
    env_val = os.environ.get(env_var)
    if env_val is not None:
        return env_val
    cfg = _load()
    return cfg.get(section, key, fallback=default)


# ── Database ──────────────────────────────────────────────────────────────────

def db_host()     -> str: return _get("database", "host",     "JARVIS_DB_HOST", "localhost")
def db_name()     -> str: return _get("database", "name",     "JARVIS_DB_NAME", "jarvis")
def db_user()     -> str: return _get("database", "user",     "JARVIS_DB_USER", "postgres")
def db_password() -> str: return _get("database", "password", "JARVIS_DB_PASS", "")


# ── Foundry (Azure AI / Anthropic) ────────────────────────────────────────────

def foundry_endpoint() -> str:
    return _get(
        "foundry", "endpoint", "FOUNDRY_ENDPOINT",
        "",
    )

def foundry_api_key() -> str:
    return _get("foundry", "api_key", "FOUNDRY_API_KEY", "")


# ── Center ────────────────────────────────────────────────────────────────────

def center_log_path() -> str:
    return _get("center", "log_path", "JARVIS_CENTER_LOG", r"C:\JarvisCenter\logs\stdout.log")

def center_api_url() -> str:
    return _get("center", "api_url", "JARVIS_CENTER_URL", "http://localhost:8080")

def center_api_key() -> str:
    return _get("center", "api_key", "JARVIS_CENTER_API_KEY", "")

def llm_provider() -> str:
    """Provider forçado no ficheiro de config ('center', 'foundry', 'ollama') — vazio = usa DB."""
    return _get("foundry", "provider", "JARVIS_LLM_PROVIDER", "center")


# ── Validação de arranque ─────────────────────────────────────────────────────

def validate() -> list[str]:
    """Devolve lista de erros de configuração. Lista vazia = OK."""
    errors = []
    if llm_provider() == "center":
        if not center_api_url():
            errors.append("center.api_url não configurado")
        if not center_api_key():
            errors.append("center.api_key não configurado")
    else:
        if not db_password():
            errors.append("database.password não configurado (JARVIS_DB_PASS)")
        if not foundry_api_key():
            errors.append("foundry.api_key não configurado (FOUNDRY_API_KEY)")
    return errors


def config_path() -> str:
    """Devolve o caminho do ficheiro de config activo, ou string vazia se nenhum existir."""
    for p in _CONFIG_PATHS:
        if p and p.exists():
            return str(p)
    return ""
