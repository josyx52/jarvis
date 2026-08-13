"""
SplunkClient — cliente mínimo da REST API do Splunk (porta de gestão, 8089
por omissão), usado para validar acesso e, na Fase 3, para o coletor em si
(saved searches / SPL ad-hoc).

Autenticação: token de autenticação do Splunk (Settings -> Tokens), enviado
como "Authorization: Splunk <token>" — é o esquema documentado pelo Splunk
para tokens (distinto de "Bearer"). Suporta também user/password (Basic)
como alternativa, para instalações sem tokens activados.
"""

import requests

_TIMEOUT_S = 15


class SplunkError(Exception):
    pass


class SplunkClient:

    def __init__(self, base_url: str, token: str | None = None,
                 user: str | None = None, password: str | None = None,
                 verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._user = user
        self._password = password
        self._verify_ssl = verify_ssl

    def _headers(self) -> dict:
        if self._token:
            return {"Authorization": f"Splunk {self._token}"}
        return {}

    def _auth(self):
        if self._token:
            return None
        if self._user:
            return (self._user, self._password or "")
        raise SplunkError("sem token nem user/password configurados")

    def list_indexes(self) -> list[str]:
        resp = requests.get(
            f"{self.base_url}/services/data/indexes",
            params={"output_mode": "json", "count": 0},
            headers=self._headers(),
            auth=self._auth(),
            verify=self._verify_ssl,
            timeout=_TIMEOUT_S,
        )
        if resp.status_code != 200:
            raise SplunkError(f"list_indexes falhou: HTTP {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        return [entry["name"] for entry in body.get("entry", [])]

    def run_oneshot_search(self, spl: str, earliest: str = "-15m", latest: str = "now") -> list[dict]:
        """Corre uma pesquisa SPL em modo 'oneshot' (síncrono) e devolve os resultados."""
        search = spl if spl.strip().startswith("search") or spl.strip().startswith("|") else f"search {spl}"
        resp = requests.post(
            f"{self.base_url}/services/search/jobs",
            data={
                "search": search,
                "earliest_time": earliest,
                "latest_time": latest,
                "exec_mode": "oneshot",
                "output_mode": "json",
            },
            headers=self._headers(),
            auth=self._auth(),
            verify=self._verify_ssl,
            timeout=_TIMEOUT_S,
        )
        if resp.status_code != 200:
            raise SplunkError(f"run_oneshot_search falhou: HTTP {resp.status_code} {resp.text[:300]}")
        return resp.json().get("results", [])
