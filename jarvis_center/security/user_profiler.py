"""
UserProfiler — constrói perfil comportamental de 7 dias por utilizador com Claude.

Corre periodicamente (cada 6 horas) para manter o perfil actualizado.
O perfil serve de baseline para o UEBAEngine detectar desvios.
"""

import json
import time
from collections import Counter
from datetime import datetime


# IPs associados a infra conhecida de anonimização/C2
_SUSPICIOUS_IP_PREFIXES = (
    "185.220.", "51.77.", "195.189.", "45.142.",
    "77.247.", "176.10.", "171.25.", "62.102.",
)

# Hora fora do horário normal de trabalho
_AFTER_HOURS_START = 20  # 20h
_AFTER_HOURS_END   = 7   # 7h


class UserProfiler:

    _PROFILE_INTERVAL = 6 * 3600  # 6 horas

    def __init__(self):
        self._last_profile: dict[str, float] = {}  # host:user → timestamp

        try:
            from brain.foundry_client import FoundryClient
            self._llm = FoundryClient()
        except Exception as e:
            print(f"[UserProfiler] FoundryClient indisponível: {e}")
            self._llm = None

    def profile_if_due(self, host: str, username: str, store) -> dict | None:
        """
        Reconstrói o perfil se passaram 6h desde o último.
        store = UserActivityStore
        """
        key = f"{host}:{username}"
        now = time.time()
        if (now - self._last_profile.get(key, 0)) < self._PROFILE_INTERVAL:
            return None

        events = store.get_events_for_profile(host, username, days=7)
        if len(events) < 10:
            return None

        summary = self._aggregate(events)
        profile = self._run_profiling(host, username, summary)

        if profile:
            self._last_profile[key] = now
            store.save_profile(host, username, profile)
            print(f"[UserProfiler] Perfil actualizado: {username}@{host} "
                  f"risk={profile.get('risk_score',0)} ({profile.get('risk_level','?')})")

        return profile

    # ─── AGGREGATION ─────────────────────────────────────────────────────────

    def _aggregate(self, events: list[dict]) -> dict:
        processes   = Counter()
        remote_ips  = Counter()
        hours_seen  = []
        days_seen   = set()
        after_hours = 0
        weekends    = 0
        privilege_events   = 0
        service_installs   = []
        registry_changes   = []
        event_dist         = Counter()

        for ev in events:
            etype   = ev.get("event_type", "")
            details = ev.get("details") or {}
            ts      = ev.get("event_time")

            event_dist[etype] += 1

            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except Exception:
                    ts = None

            if ts:
                h = ts.hour
                hours_seen.append(h)
                days_seen.add(ts.strftime("%A"))
                if h >= _AFTER_HOURS_START or h < _AFTER_HOURS_END:
                    after_hours += 1
                if ts.weekday() >= 5:
                    weekends += 1

            if etype == "process_start":
                name = details.get("name", "")
                if name:
                    processes[name] += 1

            elif etype == "network_conn":
                remote = details.get("remote_addr", "")
                if remote and not remote.startswith(("127.", "10.", "192.168.", "172.")):
                    remote_ips[remote] += 1

            elif etype == "privilege_use":
                if details.get("high_risk"):
                    privilege_events += 1

            elif etype == "service_install":
                service_installs.append(details.get("service_name", ""))

        suspicious_ips = [
            ip for ip in remote_ips
            if any(ip.startswith(p) for p in _SUSPICIOUS_IP_PREFIXES)
        ]

        min_hour = min(hours_seen) if hours_seen else 9
        max_hour = max(hours_seen) if hours_seen else 18

        return {
            "top_processes":       [{"name": n, "count": c} for n, c in processes.most_common(15)],
            "network_destinations": [{"ip": ip, "count": c} for ip, c in remote_ips.most_common(20)],
            "suspicious_ips":      suspicious_ips,
            "typical_hours":       f"{min_hour:02d}:00-{max_hour:02d}:00",
            "active_days":         sorted(days_seen),
            "after_hours_count":   after_hours,
            "weekend_count":       weekends,
            "privilege_events":    privilege_events,
            "service_installs":    service_installs[:10],
            "event_type_dist":     dict(event_dist),
            "total_events":        len(events),
        }

    # ─── LLM PROFILING ───────────────────────────────────────────────────────

    def _run_profiling(self, host: str, username: str, summary: dict) -> dict | None:
        if not self._llm:
            return self._fallback_profile(summary)

        prompt = f"""Constrói um perfil comportamental de segurança para o utilizador abaixo.
Devolve APENAS JSON válido (sem texto adicional).

UTILIZADOR: {username}
HOST: {host}

RESUMO DE ACTIVIDADE (7 DIAS):
- Total de eventos: {summary['total_events']}
- Processos mais usados: {json.dumps(summary['top_processes'][:10], ensure_ascii=False)}
- Destinos de rede externos: {json.dumps(summary['network_destinations'][:10], ensure_ascii=False)}
- IPs suspeitos detectados: {summary['suspicious_ips']}
- Horário típico de actividade: {summary['typical_hours']}
- Dias activos: {summary['active_days']}
- Eventos fora de horas: {summary['after_hours_count']}
- Eventos ao fim-de-semana: {summary['weekend_count']}
- Eventos de privilégio elevado: {summary['privilege_events']}
- Serviços instalados: {summary['service_installs']}
- Distribuição de eventos: {json.dumps(summary['event_type_dist'], ensure_ascii=False)}

Responde APENAS com JSON:
{{
  "is_human": <true|false>,
  "risk_score": <0-100>,
  "risk_level": "<low|medium|high|critical>",
  "work_type": "<Developer|SysAdmin|Finance|HR|Executive|ServiceAccount|Unknown>",
  "primary_apps": ["<app1>", "<app2>"],
  "typical_hours": "<ex: 09:00-18:00>",
  "active_days": ["Monday", "Tuesday"],
  "after_hours_activity": <true|false>,
  "weekend_activity": <true|false>,
  "frequent_destinations": [{{"host": "<ip_ou_host>", "category": "<Internal|External|Suspicious>"}}],
  "suspicious_destinations": ["<ip1>"],
  "privilege_abuse_detected": <true|false>,
  "behavioral_anomalies": ["<anomalia observada>"],
  "summary": "<resumo em 2-3 frases>"
}}

Critérios de risco:
- 0-20: low — comportamento completamente normal
- 21-50: medium — alguns comportamentos incomuns mas explicáveis
- 51-75: high — comportamento anómalo que requer atenção
- 76-100: critical — actividade potencialmente maliciosa"""

        try:
            raw = self._llm.generate(prompt, max_tokens=1500, temperature=0.1)
            profile = self._parse_json(raw)
            profile["raw_analysis"] = raw
            return profile
        except Exception as e:
            print(f"[UserProfiler] Profiling falhou para {username}@{host}: {e}")
            return self._fallback_profile(summary)

    @staticmethod
    def _fallback_profile(summary: dict) -> dict:
        """Perfil básico sem LLM — usa heurísticas simples."""
        risk = 0
        anomalies = []

        if summary["after_hours_count"] > 20:
            risk += 20
            anomalies.append(f"Actividade fora de horas: {summary['after_hours_count']} eventos")
        if summary["suspicious_ips"]:
            risk += 30
            anomalies.append(f"IPs suspeitos: {', '.join(summary['suspicious_ips'][:3])}")
        if summary["privilege_events"] > 5:
            risk += 15
            anomalies.append(f"Múltiplos eventos de privilégio: {summary['privilege_events']}")
        if summary["service_installs"]:
            risk += 10
            anomalies.append(f"Serviços instalados: {', '.join(summary['service_installs'][:3])}")

        risk = min(risk, 100)
        level = "low" if risk < 21 else ("medium" if risk < 51 else ("high" if risk < 76 else "critical"))

        return {
            "is_human":               True,
            "risk_score":             risk,
            "risk_level":             level,
            "work_type":              "Unknown",
            "primary_apps":           [p["name"] for p in summary["top_processes"][:5]],
            "typical_hours":          summary["typical_hours"],
            "active_days":            summary["active_days"],
            "after_hours_activity":   summary["after_hours_count"] > 5,
            "weekend_activity":       summary["weekend_count"] > 0,
            "frequent_destinations":  [],
            "suspicious_destinations": summary["suspicious_ips"],
            "privilege_abuse_detected": summary["privilege_events"] > 10,
            "behavioral_anomalies":   anomalies,
            "summary":                f"Perfil gerado por heurísticas. {len(anomalies)} anomalia(s) detectada(s).",
            "raw_analysis":           "",
        }

    @staticmethod
    def _parse_json(raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
