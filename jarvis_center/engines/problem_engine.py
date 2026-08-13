"""
ProblemEngine — agrupa alertas relacionados num único Problem com ciclo de vida.

Comportamento semelhante ao Dynatrace Problems:
  - Todos os alertas do mesmo host dentro de GROUP_WINDOW pertencem ao mesmo Problem.
  - A severidade só escala durante a vida do Problem (nunca baixa).
  - Um Problem resolve-se automaticamente se não chegar nenhum alerta novo
    durante OPEN_WINDOW segundos.

Ciclo de vida:
  open  → (novos alertas) → open  (actualizado)
  open  → (silêncio > OPEN_WINDOW) → resolved

Cada processor gere os seus próprios hosts (particionamento por host_key),
por isso o estado local é suficiente — não precisa de Redis.
"""

import time


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}

_OPEN_WINDOW  = 900   # segundos sem novos alertas → auto-resolve
_GROUP_WINDOW = 300   # janela de agrupamento (não usado explicitamente —
                      # qualquer alerta enquanto o Problem está open fica agrupado)


def _top_severity(alerts: list[dict]) -> str:
    return min(
        (a.get("severity", "unknown") for a in alerts),
        key=lambda s: _SEVERITY_ORDER.get(s, 99),
        default="unknown",
    )


class ProblemEngine:
    """Agrupa alertas por host num Problem com lifecycle open/resolved."""

    def __init__(self):
        # host_key → Problem em aberto
        self._active: dict[str, dict] = {}

    def process(
        self,
        host_key:     str,
        alerts:       list[dict],
        events:       list[dict],
        correlations: list[dict],
    ) -> list[dict]:
        """
        Deve ser chamado a cada frame, mesmo sem alertas.
        Retorna lista de Problems emitidos neste frame:
          - {"status": "open"}     → Problem novo ou actualizado
          - {"status": "resolved"} → Problem fechado por timeout
        """
        now     = time.time()
        emitted = []

        # ── 1. Verificar resoluções automáticas por timeout ───────────────────
        if host_key in self._active:
            prob = self._active[host_key]
            if now - prob["last_seen"] >= _OPEN_WINDOW:
                resolved          = dict(prob)
                resolved["status"]      = "resolved"
                resolved["resolved_at"] = now
                resolved["duration_s"]  = round(now - prob["opened_at"], 1)
                emitted.append(resolved)
                del self._active[host_key]

        if not alerts:
            return emitted

        # ── 2. Calcular atributos dos alertas actuais ─────────────────────────
        sev          = _top_severity(alerts)
        alert_titles = list({a.get("title", "") for a in alerts if a.get("title")})

        if host_key in self._active:
            # ── 3a. Actualizar Problem existente ──────────────────────────────
            prob                       = self._active[host_key]
            prob["last_seen"]          = now
            prob["alert_count"]       += len(alerts)
            prob["events_count"]       = len(events)
            prob["correlations_count"] = len(correlations)

            # Severidade só escala — nunca baixa durante a vida do Problem
            if _SEVERITY_ORDER.get(sev, 99) < _SEVERITY_ORDER.get(prob["severity"], 99):
                prob["severity"] = sev

            for title in alert_titles:
                if title and title not in prob["alert_titles"]:
                    prob["alert_titles"].append(title)

            emitted.append(dict(prob))

        else:
            # ── 3b. Criar novo Problem ────────────────────────────────────────
            hostname   = host_key.split("::")[0]
            problem_id = f"prob_{hostname}_{int(now)}"

            prob = {
                "problem_id":          problem_id,
                "host":                host_key,
                "title":               alerts[0].get("title", "Problema detectado"),
                "severity":            sev,
                "status":              "open",
                "alert_count":         len(alerts),
                "alert_titles":        alert_titles,
                "events_count":        len(events),
                "correlations_count":  len(correlations),
                "opened_at":           now,
                "last_seen":           now,
                "resolved_at":         None,
                "duration_s":          None,
            }
            self._active[host_key] = prob
            emitted.append(dict(prob))

        return emitted

    # ── Utilitários ───────────────────────────────────────────────────────────

    def open_problems(self) -> list[dict]:
        """Devolve cópia de todos os Problems actualmente em aberto."""
        return [dict(p) for p in self._active.values()]

    def problem_count(self) -> int:
        return len(self._active)
