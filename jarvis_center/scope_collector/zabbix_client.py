"""
ZabbixClient — cliente mínimo da API JSON-RPC do Zabbix.

Usado pelo Scope Collector para ler os valores mais recentes dos items de um
host Zabbix (via item.get, que já devolve lastvalue/lastclock sem precisar de
uma segunda chamada a history.get) e para consultar séries históricas quando
necessário.

Autenticação: suporta tanto o token de API estático (Zabbix >= 5.4, enviado
via header Authorization: Bearer) como o fluxo clássico user.login (versões
mais antigas), consoante o que estiver configurado na integração Clotho
(clotho_integrations.config: {"auth_token": "..."} ou {"user":..., "password":...}).
"""

import time

import requests

_TIMEOUT_S = 15


class ZabbixError(Exception):
    pass


class ZabbixClient:

    def __init__(self, base_url: str, auth_token: str | None = None,
                 user: str | None = None, password: str | None = None):
        self.url = base_url.rstrip("/") + "/api_jsonrpc.php"
        self._auth_token = auth_token
        self._user = user
        self._password = password
        self._id = 0

    # ── JSON-RPC core ─────────────────────────────────────────────────────────

    def _call(self, method: str, params: dict, use_auth: bool = True) -> dict:
        self._id += 1
        headers = {"Content-Type": "application/json-rpc"}
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._id,
        }

        if use_auth and self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        elif use_auth and not self._auth_token and self._user:
            self._login()
            headers["Authorization"] = f"Bearer {self._auth_token}"

        resp = requests.post(self.url, json=payload, headers=headers, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        body = resp.json()

        if "error" in body:
            raise ZabbixError(f"{method} falhou: {body['error']}")

        return body.get("result")

    def _login(self):
        if not self._user:
            raise ZabbixError("sem auth_token nem user/password configurados")
        result = self._call(
            "user.login",
            {"username": self._user, "password": self._password},
            use_auth=False,
        )
        self._auth_token = result

    # ── API pública ───────────────────────────────────────────────────────────

    def get_api_version(self) -> str:
        return self._call("apiinfo.version", {}, use_auth=False)

    def has_credentials(self) -> bool:
        return bool(self._auth_token or self._user)

    def ensure_authenticated(self) -> None:
        """Força login (se ainda não houver auth_token) para validar as credenciais.
        Lança ZabbixError se as credenciais estiverem erradas."""
        if not self._auth_token:
            self._login()

    def get_host_id(self, host_name: str) -> str | None:
        hosts = self._call("host.get", {"filter": {"host": [host_name]}, "output": ["hostid"]})
        return hosts[0]["hostid"] if hosts else None

    def get_latest_values(self, host_id: str, item_keys: list[str]) -> dict[str, dict]:
        """
        Devolve {item_key: {"value": float|None, "clock": epoch, "name": str}}
        para os items do host cuja key_ está em item_keys.
        """
        items = self._call(
            "item.get",
            {
                "hostids": [host_id],
                "filter": {"key_": item_keys},
                "output": ["itemid", "key_", "name", "lastvalue", "lastclock"],
            },
        )
        out = {}
        for item in items:
            key = item.get("key_")
            value = item.get("lastvalue")
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
            out[key] = {
                "value": value,
                "clock": int(item.get("lastclock") or 0),
                "name": item.get("name"),
            }
        return out

    def get_active_problems(self, host_ids: list[str], min_severity: int = 0) -> list[dict]:
        """
        Devolve os problemas activos (problem.get) para os hosts indicados,
        já com o nome do host resolvido — usado pelo Atropos para saber quais
        servidores o Zabbix considera em baixo/com problema, distinto de
        "consegui falar com a API do Zabbix" (que é o que scope_targets.last_status
        já mede). min_severity segue a escala do Zabbix: 0=not classified,
        1=information, 2=warning, 3=average, 4=high, 5=disaster.
        """
        params = {
            "hostids": host_ids,
            "recent": False,
            "sortfield": ["eventid"],
            "sortorder": "DESC",
            "output": ["eventid", "objectid", "name", "severity", "clock"],
            "selectHosts": ["hostid", "host", "name"],
        }
        if min_severity:
            params["severities"] = list(range(min_severity, 6))
        problems = self._call("problem.get", params)
        out = []
        for p in problems or []:
            hosts = p.get("hosts") or []
            out.append({
                "event_id": p.get("eventid"),
                "trigger_id": p.get("objectid"),
                "name": p.get("name"),
                "severity": int(p.get("severity") or 0),
                "clock": int(p.get("clock") or 0),
                "host_id": hosts[0]["hostid"] if hosts else None,
                "host": hosts[0]["host"] if hosts else None,
            })
        return out

    def get_history(self, item_id: str, history_type: int = 0,
                     since_seconds: int = 3600, limit: int = 100) -> list[dict]:
        """history_type: 0=float, 1=character, 2=log, 3=unsigned, 4=text (ver docs Zabbix)."""
        time_from = int(time.time()) - since_seconds
        return self._call(
            "history.get",
            {
                "itemids": [item_id],
                "history": history_type,
                "time_from": time_from,
                "output": "extend",
                "sortfield": "clock",
                "sortorder": "DESC",
                "limit": limit,
            },
        )
