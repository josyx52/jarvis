import os
import psycopg2
import psycopg2.extras

_conn = None


# ── DB Proxy (quando não há acesso directo à DB) ──────────────────────────────

class _ProxyCursor:
    """Imita psycopg2 cursor mas envia queries ao center via /db/query."""

    def __init__(self, proxy_conn: "_ProxyConnection"):
        self._proxy      = proxy_conn
        self._rows       = []
        self._pos        = 0
        self.rowcount    = -1
        self.description = None  # populado após execute

    def execute(self, sql, params=None):
        import httpx
        payload = {"sql": sql, "params": list(params) if params else [], "dict_cursor": True}
        resp = httpx.post(
            self._proxy._url,
            json=payload,
            headers={"X-API-Key": self._proxy._key},
            timeout=30.0,
        )
        resp.raise_for_status()
        data          = resp.json()
        self._rows    = data.get("rows", [])
        self._pos     = 0
        self.rowcount = data.get("rowcount", len(self._rows))
        # Popula description a partir das chaves do primeiro row (formato psycopg2)
        if self._rows and isinstance(self._rows[0], dict):
            self.description = [(col, None, None, None, None, None, None) for col in self._rows[0].keys()]
        else:
            self.description = None

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        if isinstance(row, dict):
            return tuple(row.values())
        return tuple(row) if row else None

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]

    def __enter__(self): return self
    def __exit__(self, *_): pass


class _ProxyConnection:
    """Imita psycopg2 connection mas usa HTTP para enviar queries ao center."""

    def __init__(self, url: str, key: str):
        self._url  = url
        self._key  = key
        self.closed = False
        self.autocommit = True

    def cursor(self, cursor_factory=None):
        return _ProxyCursor(self)

    def commit(self):   pass
    def rollback(self): pass
    def close(self):    self.closed = True

_DEFAULTS = [
    ("alert.cpu_threshold",    "90",                   "int",    "thresholds", "CPU % threshold for alerts"),
    ("alert.memory_threshold", "85",                   "int",    "thresholds", "Memory % threshold for alerts"),
    ("alert.disk_threshold",   "80",                   "int",    "thresholds", "Disk % threshold for alerts"),
    ("baseline.sigma_warn",    "2.5",                  "float",  "baseline",   "Z-score threshold for medium severity"),
    ("baseline.sigma_high",    "3.5",                  "float",  "baseline",   "Z-score threshold for high severity"),
    ("baseline.min_seasonal",  "5",                    "int",    "baseline",   "Min samples to activate seasonal mode"),
    ("baseline.window_global", "500",                  "int",    "baseline",   "Global rolling window size"),
    ("baseline.window_slot",   "120",                  "int",    "baseline",   "Seasonal slot rolling window size"),
    ("llm.cooldown_seconds",   "30",                        "int",    "llm",        "Seconds between LLM calls per processor"),
    ("llm.provider",           "foundry",                   "string", "llm",        "LLM provider: foundry | ollama"),
    ("llm.model",              "claude-sonnet-4-6",         "string", "llm",        "Active LLM model ID"),
    ("llm.ollama_url",         "http://localhost:11434",     "string", "llm",        "Ollama base URL"),
    ("llm.max_tokens",         "4096",                      "int",    "llm",        "Max tokens per LLM call"),
    ("llm.temperature",        "0.2",                       "float",  "llm",        "LLM sampling temperature"),
    ("retention.events",       "7 days",               "string", "retention",  "Event retention period"),
    ("retention.snapshots",    "30 days",              "string", "retention",  "Snapshot retention period"),
    ("retention.alerts",       "30 days",              "string", "retention",  "Alert retention period"),
    ("retention.problems",     "90 days",              "string", "retention",  "Problem retention period"),
    ("retention.traces",       "3 days",               "string", "retention",  "Trace retention period"),
    ("problem.open_window",    "900",                  "int",    "problems",   "Seconds of silence before auto-resolve"),
    ("agent.snapshot_interval","60",                   "int",    "agent",      "Min seconds between snapshots per host"),
    ("agent.snapshot_delta",   "5.0",                  "float",  "agent",      "Metric delta % to force snapshot"),
    ("solution_driver.enabled","false",                 "bool",   "solution_driver", "Activar Solution Driver (notificações + conversa com analista)"),
]


def get_db():
    global _conn
    if _conn is not None and not _conn.closed:
        return _conn

    import config as _cfg
    if _cfg.llm_provider() == "center" and _cfg.center_api_url() and _cfg.center_api_key():
        _conn = _ProxyConnection(
            url = _cfg.center_api_url().rstrip("/") + "/db/query",
            key = _cfg.center_api_key(),
        )
    else:
        _conn = psycopg2.connect(
            host     = _cfg.db_host(),
            database = _cfg.db_name(),
            user     = _cfg.db_user(),
            password = _cfg.db_password(),
        )
        _conn.autocommit = False
    return _conn


def close_db():
    global _conn
    if _conn and not _conn.closed:
        _conn.close()
        _conn = None


def db_connected() -> bool:
    try:
        cur = get_db().cursor()
        cur.execute("SELECT 1")
        return True
    except Exception:
        return False


def db_url_str() -> str:
    import config as _cfg
    return f"postgres://{_cfg.db_user()}@{_cfg.db_host()}/{_cfg.db_name()}"


def ensure_config_table():
    """Creates jarvis_config table and inserts defaults if missing."""
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS jarvis_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                type        TEXT DEFAULT 'string',
                category    TEXT DEFAULT 'general',
                description TEXT,
                updated_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        for key, value, typ, cat, desc in _DEFAULTS:
            cur.execute("""
                INSERT INTO jarvis_config (key, value, type, category, description)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (key) DO NOTHING
            """, (key, value, typ, cat, desc))
        conn.commit()
    except Exception:
        try:
            get_db().rollback()
        except Exception:
            pass


def get_config_value(key: str, default=None):
    """Read a single config value from DB."""
    try:
        cur = get_db().cursor()
        cur.execute("SELECT value FROM jarvis_config WHERE key = %s", [key])
        row = cur.fetchone()
        return row[0] if row else default
    except Exception:
        return default


def set_config_value(key: str, value: str):
    """Upsert a config value in DB."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO jarvis_config (key, value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    """, (key, value))
    conn.commit()
