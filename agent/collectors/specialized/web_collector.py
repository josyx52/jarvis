"""
WebCollector — coleta metricas de qualquer servidor web.

Suporte:
  Nginx   — stub_status endpoint (http://localhost/nginx_status)
  Apache  — mod_status endpoint (http://localhost/server-status?auto)
  IIS     — Windows Performance Counters via WMI
  HAProxy — stats page (http://localhost/haproxy?stats;csv)
  Caddy   — metrics endpoint (http://localhost:2019/metrics)
  Generico— qualquer servidor com endpoint de metricas HTTP

Recolhe:
  - Requests por segundo / total
  - Conexoes ativas
  - Erros (4xx / 5xx)
  - Workers ativos / em espera

Frame gerado: web_telemetry
"""

import logging
import agent_core.config as cfg

logger = logging.getLogger(__name__)


class WebCollector:

    FRAME_TYPE = "web_telemetry"

    def __init__(self):
        self._type       = cfg._get("collectors.web.type", None)
        self._nginx_url  = cfg._get("collectors.web.nginx_status_url",   "http://localhost/nginx_status")
        self._apache_url = cfg._get("collectors.web.apache_status_url",  "http://localhost/server-status?auto")
        self._haproxy_url= cfg._get("collectors.web.haproxy_stats_url",  "http://localhost/haproxy?stats;csv")
        self._caddy_url  = cfg._get("collectors.web.caddy_metrics_url",  "http://localhost:2019/metrics")
        self._prev       = {}

    def collect(self) -> list[dict]:
        try:
            if self._type == "nginx":
                data = self._collect_nginx()
            elif self._type == "iis":
                data = self._collect_iis()
            else:
                data = self._collect_auto()

            if data:
                return [data]
        except Exception as e:
            logger.warning(f"[WEB] Erro na coleta ({self._type}): {e}")
        return []

    # ----------------------------------------
    # AUTO-DETECT
    # ----------------------------------------

    def _collect_auto(self) -> dict | None:
        for fn in (
            self._collect_nginx,
            self._collect_apache,
            self._collect_haproxy,
            self._collect_caddy,
            self._collect_iis,
        ):
            data = fn()
            if data:
                return data
        return None

    # ----------------------------------------
    # NGINX
    # ----------------------------------------

    def _collect_nginx(self) -> dict | None:
        try:
            import requests
            resp = requests.get(self._nginx_url, timeout=3)
            if resp.status_code != 200:
                return None
            return self._parse_nginx_status(resp.text)
        except Exception:
            return None

    def _parse_nginx_status(self, text: str) -> dict:
        """
        Nginx stub_status format:
          Active connections: 291
          server accepts handled requests
           16630948 16630948 31070465
          Reading: 6 Writing: 179 Waiting: 106
        """
        lines  = text.strip().splitlines()
        result = {"web_type": "nginx"}

        try:
            result["active_connections"] = int(lines[0].split(":")[1].strip())
        except Exception:
            pass

        try:
            nums = lines[2].split()
            result["accepts"]  = int(nums[0])
            result["handled"]  = int(nums[1])
            result["requests"] = int(nums[2])
        except Exception:
            pass

        try:
            parts = lines[3].split()
            result["reading"] = int(parts[1])
            result["writing"] = int(parts[3])
            result["waiting"] = int(parts[5])
        except Exception:
            pass

        # Throughput delta
        prev_req = self._prev.get("requests", result.get("requests", 0))
        curr_req = result.get("requests", 0)
        result["requests_delta"] = max(0, curr_req - prev_req)
        self._prev = dict(result)

        return result

    # ----------------------------------------
    # APACHE (mod_status)
    # ----------------------------------------

    def _collect_apache(self) -> dict | None:
        try:
            import requests
            resp = requests.get(self._apache_url, timeout=3)
            if resp.status_code != 200:
                return None
            return self._parse_apache_status(resp.text)
        except Exception:
            return None

    def _parse_apache_status(self, text: str) -> dict:
        """
        Apache mod_status ?auto format:
          Total Accesses: 1234
          Total kBytes: 5678
          BusyWorkers: 10
          IdleWorkers: 90
          ...
        """
        result = {"web_type": "apache"}
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower().replace(" ", "_")
            val = val.strip()
            try:
                result[key] = int(val)
            except ValueError:
                try:
                    result[key] = float(val)
                except ValueError:
                    result[key] = val

        result["active_connections"] = result.get("busyworkers", 0)
        result["requests"] = result.get("total_accesses", 0)
        prev_req = self._prev.get("requests", result.get("requests", 0))
        result["requests_delta"] = max(0, result["requests"] - prev_req)
        self._prev = dict(result)
        return result

    # ----------------------------------------
    # HAPROXY (stats CSV)
    # ----------------------------------------

    def _collect_haproxy(self) -> dict | None:
        try:
            import requests
            resp = requests.get(self._haproxy_url, timeout=3)
            if resp.status_code != 200:
                return None
            return self._parse_haproxy_stats(resp.text)
        except Exception:
            return None

    def _parse_haproxy_stats(self, text: str) -> dict:
        import csv, io
        result = {"web_type": "haproxy", "backends": [], "active_connections": 0}
        try:
            reader = csv.DictReader(io.StringIO(text.lstrip("# ")))
            total_conn = 0
            total_req  = 0
            for row in reader:
                svname = row.get("svname", "")
                if svname in ("FRONTEND", "BACKEND"):
                    conn = int(row.get("scur") or 0)
                    req  = int(row.get("req_tot") or row.get("stot") or 0)
                    total_conn += conn
                    total_req  += req
                    result["backends"].append({
                        "pxname":      row.get("pxname"),
                        "svname":      svname,
                        "connections": conn,
                        "requests":    req,
                        "errors":      int(row.get("econ") or 0),
                        "status":      row.get("status"),
                    })
            result["active_connections"] = total_conn
            result["requests"] = total_req
            prev_req = self._prev.get("requests", total_req)
            result["requests_delta"] = max(0, total_req - prev_req)
            self._prev = dict(result)
        except Exception:
            pass
        return result

    # ----------------------------------------
    # CADDY (Prometheus metrics)
    # ----------------------------------------

    def _collect_caddy(self) -> dict | None:
        try:
            import requests
            resp = requests.get(self._caddy_url, timeout=3)
            if resp.status_code != 200:
                return None
            return self._parse_caddy_metrics(resp.text)
        except Exception:
            return None

    def _parse_caddy_metrics(self, text: str) -> dict:
        result = {"web_type": "caddy"}
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            if "caddy_http_requests_total" in line and "{" not in line:
                try:
                    result["requests"] = int(float(line.split()[-1]))
                except Exception:
                    pass
            if "caddy_http_active_requests" in line and "{" not in line:
                try:
                    result["active_connections"] = int(float(line.split()[-1]))
                except Exception:
                    pass
        prev_req = self._prev.get("requests", result.get("requests", 0))
        result["requests_delta"] = max(0, result.get("requests", 0) - prev_req)
        self._prev = dict(result)
        return result if len(result) > 1 else None

    # ----------------------------------------
    # IIS (Windows Performance Counters)
    # ----------------------------------------

    def _collect_iis(self) -> dict | None:
        try:
            import wmi
            c = wmi.WMI()

            result = {"web_type": "iis", "sites": []}

            for site in c.Win32_PerfFormattedData_W3SVC_WebService():
                if site.Name == "_Total":
                    result.update({
                        "requests_per_sec":  site.TotalGetRequests + site.TotalPostRequests,
                        "current_connections": site.CurrentConnections,
                        "bytes_sent":        site.TotalBytesSent,
                        "bytes_received":    site.TotalBytesReceived,
                        "not_found_errors":  site.TotalNotFoundErrors,
                        "connection_attempts": site.TotalConnectionAttempts,
                    })
                else:
                    result["sites"].append({
                        "name":     site.Name,
                        "requests": site.TotalGetRequests + site.TotalPostRequests,
                        "connections": site.CurrentConnections,
                    })

            return result if len(result) > 2 else None

        except Exception:
            return None
