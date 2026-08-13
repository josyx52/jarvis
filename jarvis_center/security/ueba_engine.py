"""
UEBAEngine — analisa comportamento de utilizadores com Claude.

Dois modos:
  periodic   — corre de hora a hora para todos os utilizadores activos
  triggered  — corre imediatamente quando eventos de alto risco são detectados

Usa o FoundryClient (Claude Sonnet via Azure AI Foundry) tal como os
outros engines do center.
"""

import json
import time
from datetime import datetime


class UEBAEngine:

    # Intervalo mínimo entre análises do mesmo utilizador (segundos)
    _MIN_ANALYSIS_INTERVAL = 3600  # 1 hora

    # Risk score a partir do qual dispara análise imediata
    _HIGH_RISK_TRIGGER_SCORE = 60

    def __init__(self):
        self._last_analysis: dict[str, float] = {}  # host:user → timestamp

        try:
            from brain.foundry_client import FoundryClient
            self._llm = FoundryClient()
        except Exception as e:
            print(f"[UEBAEngine] FoundryClient indisponível: {e}")
            self._llm = None

    # ─── ENTRY POINTS ────────────────────────────────────────────────────────

    def analyze_if_due(self, host: str, username: str,
                       store, analysis_type: str = "periodic") -> dict | None:
        """
        Analisa comportamento se passou tempo suficiente desde a última análise.
        store = UserActivityStore
        """
        key = f"{host}:{username}"
        now = time.time()
        last = self._last_analysis.get(key, 0)

        if analysis_type != "triggered" and (now - last) < self._MIN_ANALYSIS_INTERVAL:
            return None

        events = store.get_recent_events(host, username, hours=24, limit=300)
        if not events:
            return None

        result = self._run_analysis(host, username, events, analysis_type)
        if result:
            self._last_analysis[key] = now
            analysis_id = store.save_analysis(
                host, username, analysis_type, result,
                events_analyzed=len(events),
                trigger_event=result.get("trigger_event", ""),
            )
            self._maybe_raise_alert(host, username, result, analysis_id, store)

        return result

    def analyze_high_risk(self, host: str, username: str,
                          trigger_event: dict, store) -> dict | None:
        """Análise imediata disparada por evento de alto risco."""
        events = store.get_recent_events(host, username, hours=4, limit=100)
        if not events:
            return None

        result = self._run_analysis(host, username, events, "triggered",
                                    trigger=trigger_event)
        if result:
            result["trigger_event"] = trigger_event.get("event_type", "")
            analysis_id = store.save_analysis(
                host, username, "triggered", result,
                events_analyzed=len(events),
                trigger_event=result.get("trigger_event", ""),
            )
            self._maybe_raise_alert(host, username, result, analysis_id, store)

        return result

    # ─── LLM ANALYSIS ────────────────────────────────────────────────────────

    def _run_analysis(self, host: str, username: str, events: list[dict],
                      analysis_type: str, trigger: dict | None = None) -> dict | None:
        if not self._llm:
            return None

        timeline = self._format_timeline(events)
        trigger_ctx = ""
        if trigger:
            trigger_ctx = (
                f"\nEVENTO DE ALTO RISCO QUE DESPOLETOU ESTA ANÁLISE:\n"
                f"  Tipo: {trigger.get('event_type')}\n"
                f"  Detalhes: {json.dumps(trigger.get('details', {}), ensure_ascii=False)}\n"
            )

        prompt = f"""Analisa o comportamento de segurança do utilizador abaixo e devolve apenas JSON válido.

UTILIZADOR: {username}
HOST: {host}
PERÍODO ANALISADO: últimas {24 if analysis_type != 'triggered' else 4} horas
TIPO DE ANÁLISE: {analysis_type}
{trigger_ctx}
ACTIVIDADE OBSERVADA:
{timeline}

Responde APENAS com JSON (sem texto adicional):
{{
  "compliance_score": <inteiro 0-100, 100=completamente normal>,
  "verdict": "<compliant|suspicious|non_compliant>",
  "summary": "<2-3 frases sobre o comportamento observado>",
  "anomalies": ["<anomalia 1>", "<anomalia 2>"],
  "risk_indicators": ["<indicador 1>"],
  "recommendations": ["<acção recomendada>"]
}}

Critérios:
- 80-100: compliant — actividade normal para o perfil do utilizador
- 50-79: suspicious — actividade incomum que merece atenção
- 0-49:  non_compliant — actividade anómala ou potencialmente maliciosa

Considera como suspeito: processos desconhecidos com privilégios, serviços instalados em runtime,
conexões a IPs externos incomuns, actividade fora de horas normais, ferramentas de segurança conhecidas."""

        try:
            raw = self._llm.generate(prompt, max_tokens=1024, temperature=0.1)
            result = self._parse_json(raw)
            result["raw_analysis"] = raw
            return result
        except Exception as e:
            print(f"[UEBAEngine] Análise falhou para {username}@{host}: {e}")
            return None

    # ─── ALERT ───────────────────────────────────────────────────────────────

    def _maybe_raise_alert(self, host: str, username: str, result: dict,
                           analysis_id: int | None, store):
        verdict = result.get("verdict", "compliant")
        score   = result.get("compliance_score", 100)

        if verdict == "compliant" and score >= 80:
            return

        severity = "critical" if score < 30 else ("high" if score < 50 else "medium")
        alert_type = "behavioral_anomaly" if verdict == "suspicious" else "non_compliant_activity"

        anomalies = result.get("anomalies", [])
        title = f"[UEBA] {username}@{host} — {verdict} (score={score})"

        store.save_security_alert(
            host=host,
            username=username,
            alert_type=alert_type,
            severity=severity,
            title=title,
            details={
                "compliance_score": score,
                "verdict":          verdict,
                "summary":          result.get("summary", ""),
                "anomalies":        anomalies[:10],
                "recommendations":  result.get("recommendations", []),
            },
            analysis_id=analysis_id,
        )

        print(f"[UEBAEngine] ALERTA {severity.upper()} — {title}")

    # ─── HELPERS ─────────────────────────────────────────────────────────────

    @staticmethod
    def _format_timeline(events: list[dict]) -> str:
        lines = []
        for ev in events:
            ts      = ev.get("event_time", "")
            etype   = ev.get("event_type", "")
            details = ev.get("details") or {}

            if isinstance(ts, datetime):
                ts = ts.isoformat()

            detail_str = ""
            if etype == "process_start":
                detail_str = f"{details.get('name','')} — {details.get('cmdline','')[:100]}"
                if details.get("elevated"):
                    detail_str += " [ELEVADO]"
                if details.get("suspicious"):
                    detail_str += " [SUSPEITO]"
            elif etype == "privilege_use":
                privs = details.get("high_risk") or details.get("privileges", [])
                detail_str = ", ".join(privs[:5])
            elif etype == "service_install":
                detail_str = f"{details.get('service_name','')} @ {details.get('image_path','')}"
            elif etype == "scheduled_task":
                detail_str = f"{details.get('action','')} {details.get('task_name','')}"
            elif etype in ("user_logon", "user_logoff"):
                detail_str = details.get("logon_type", "") or details.get("ip_address", "")
            else:
                detail_str = str(details)[:100]

            lines.append(f"[{ts}] [{etype.upper()}] {detail_str}")

        return "\n".join(lines) if lines else "(sem eventos)"

    @staticmethod
    def _parse_json(raw: str) -> dict:
        raw = raw.strip()
        # remover code fences se presentes
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
