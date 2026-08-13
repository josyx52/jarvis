"""
AdaptiveThresholdEngine — ajusta sigma (sensibilidade) por família de métricas por host.

Ciclo de vida de cada série:

  LEARNING (primeiros MIN_ALERTS_LEARNING alertas)
    → usa thresholds fixos globais: σwarn=2.5  σhigh=3.5
    → observa silenciosamente sem alterar nada

  ADAPTED (após aprendizagem)
    → recalcula sigma com base em noise_ratio a cada hora:
        noise_ratio = alertas que NÃO resultaram em Problem / total alertas
        > 0.80 → muito ruído → σ sobe 0.5   (menos sensível)
        > 0.60 → algum ruído → σ sobe 0.25
        < 0.20 → muito preciso → σ desce 0.25 (mais sensível)
        0.20-0.60 → intervalo aceitável → sem alteração

Semelhante ao Davis AI do Dynatrace: automático, sem intervenção humana.
Estado persistido em PostgreSQL para sobreviver a reinicios.
"""

import os
import threading
import time

import psycopg2
import psycopg2.extras

# ── Thresholds globais fixos (período de aprendizagem) ───────────────────────

SIGMA_WARN_DEFAULT = 2.5
SIGMA_HIGH_DEFAULT = 3.5

# ── Limites de adaptação ─────────────────────────────────────────────────────

SIGMA_WARN_MIN = 1.5
SIGMA_WARN_MAX = 6.0
SIGMA_HIGH_MIN = 2.5
SIGMA_HIGH_MAX = 8.0

# ── Período de aprendizagem ───────────────────────────────────────────────────

MIN_ALERTS_LEARNING = 20   # alertas mínimos antes de adaptar

# ── Frequência de adaptação ───────────────────────────────────────────────────

ADAPT_INTERVAL_S = 3600    # 1 hora entre adaptações da mesma série


# ── Estado por série ──────────────────────────────────────────────────────────

class _SeriesState:
    __slots__ = (
        "alerts_fired", "alerts_with_problem",
        "sigma_warn", "sigma_high",
        "learning", "last_adapted",
    )

    def __init__(self):
        self.alerts_fired        = 0
        self.alerts_with_problem = 0
        self.sigma_warn          = SIGMA_WARN_DEFAULT
        self.sigma_high          = SIGMA_HIGH_DEFAULT
        self.learning            = True
        self.last_adapted        = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _key_from_series(series_key: str) -> str:
    """
    Converte series_key do BaselineEngine em tracking_key (host + família de métrica).

    Exemplos:
      "srv1::abc::cpu_percent"               → "srv1::abc::cpu_percent"
      "srv1::abc::trace::svc::op::p95_ms"   → "srv1::abc::trace_p95_ms"
      "srv1::abc::db::mydb::avg_query_ms"   → "srv1::abc::db_avg_query_ms"
    """
    parts = series_key.split("::")
    if len(parts) < 3:
        return series_key
    host_key = "::".join(parts[:2])          # "hostname::boot_id"
    suffix   = parts[2:]                     # tudo após o host_key

    if suffix[0] == "trace":
        family = f"trace_{suffix[-1]}"
    elif suffix[0] == "db":
        family = f"db_{suffix[-1]}"
    else:
        family = suffix[0]                   # cpu_percent, memory_percent, …

    return f"{host_key}::{family}"


def _conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        dbname=os.getenv("POSTGRES_DB", "jarvis"),
        user=os.getenv("POSTGRES_USER", "jarvis"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
    )


# ── Engine principal ──────────────────────────────────────────────────────────

class AdaptiveThresholdEngine:
    """
    Ajusta sigma por família de métricas com base na correlação alerta → Problem.
    Thread-safe. Persiste estado em PostgreSQL.
    """

    def __init__(self):
        self._state: dict[str, _SeriesState] = {}
        self._lock  = threading.Lock()
        self._load_from_db()

    # ── API pública ───────────────────────────────────────────────────────────

    def get_sigmas(self, series_key: str) -> tuple[float, float]:
        """
        Devolve (sigma_warn, sigma_high) para esta série.
        Durante o período de aprendizagem devolve os valores globais fixos.
        """
        key = _key_from_series(series_key)
        with self._lock:
            state = self._state.get(key)
        if state is None or state.learning:
            return SIGMA_WARN_DEFAULT, SIGMA_HIGH_DEFAULT
        return state.sigma_warn, state.sigma_high

    def record_alert(self, series_key: str, became_problem: bool):
        """
        Regista um alerta de baseline e se estava associado a um Problem activo.
        Adapta sigma se passou 1 hora desde a última adaptação desta série.
        """
        key = _key_from_series(series_key)
        now = time.time()

        with self._lock:
            if key not in self._state:
                self._state[key] = _SeriesState()
            s = self._state[key]

            s.alerts_fired += 1
            if became_problem:
                s.alerts_with_problem += 1

            if s.learning and s.alerts_fired >= MIN_ALERTS_LEARNING:
                s.learning = False
                print(f"[ADAPTIVE] {key}: aprendizagem concluída "
                      f"({s.alerts_fired} alertas, "
                      f"{s.alerts_with_problem} com problem)")

            if not s.learning and (now - s.last_adapted) >= ADAPT_INTERVAL_S:
                self._adapt(key, s)
                s.last_adapted = now

    def status(self) -> list[dict]:
        """Lista o estado de todas as séries — útil para /adaptive/status."""
        out = []
        with self._lock:
            for key, s in self._state.items():
                noise = (
                    (s.alerts_fired - s.alerts_with_problem) / s.alerts_fired
                    if s.alerts_fired else 0.0
                )
                out.append({
                    "series":            key,
                    "learning":          s.learning,
                    "alerts_fired":      s.alerts_fired,
                    "alerts_w_problem":  s.alerts_with_problem,
                    "noise_ratio":       round(noise, 3),
                    "sigma_warn":        s.sigma_warn,
                    "sigma_high":        s.sigma_high,
                    "learning_progress": (
                        f"{s.alerts_fired}/{MIN_ALERTS_LEARNING}"
                        if s.learning else "completo"
                    ),
                })
        return sorted(out, key=lambda x: x["series"])

    # ── Adaptação ─────────────────────────────────────────────────────────────

    def _adapt(self, key: str, s: _SeriesState):
        if s.alerts_fired == 0:
            return

        noise_ratio = (s.alerts_fired - s.alerts_with_problem) / s.alerts_fired
        old_warn    = s.sigma_warn
        old_high    = s.sigma_high

        if noise_ratio > 0.80:
            s.sigma_warn = min(s.sigma_warn + 0.50, SIGMA_WARN_MAX)
            s.sigma_high = min(s.sigma_high + 0.50, SIGMA_HIGH_MAX)
        elif noise_ratio > 0.60:
            s.sigma_warn = min(s.sigma_warn + 0.25, SIGMA_WARN_MAX)
            s.sigma_high = min(s.sigma_high + 0.25, SIGMA_HIGH_MAX)
        elif noise_ratio < 0.20:
            s.sigma_warn = max(s.sigma_warn - 0.25, SIGMA_WARN_MIN)
            s.sigma_high = max(s.sigma_high - 0.25, SIGMA_HIGH_MIN)
        # 0.20-0.60 → intervalo aceitável → sem alteração

        changed   = s.sigma_warn != old_warn or s.sigma_high != old_high
        direction = (
            "↑ menos sensível" if s.sigma_warn > old_warn else
            "↓ mais sensível"  if s.sigma_warn < old_warn else
            "→ sem alteração"
        )

        print(
            f"[ADAPTIVE] {key}: noise={noise_ratio:.0%} "
            f"σwarn={old_warn:.2f}→{s.sigma_warn:.2f} "
            f"σhigh={old_high:.2f}→{s.sigma_high:.2f} {direction}"
        )

        if changed:
            self._persist_one(key, s)

    # ── Persistência ──────────────────────────────────────────────────────────

    def _load_from_db(self):
        try:
            with _conn() as cx, cx.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute("SELECT * FROM threshold_overrides")
                rows = cur.fetchall()
            for row in rows:
                s = _SeriesState()
                s.alerts_fired        = row["alerts_fired"]
                s.alerts_with_problem = row["alerts_with_problem"]
                s.sigma_warn          = float(row["sigma_warn"])
                s.sigma_high          = float(row["sigma_high"])
                s.learning            = bool(row["learning"])
                s.last_adapted        = 0.0
                self._state[row["tracking_key"]] = s
            if self._state:
                print(f"[ADAPTIVE] {len(self._state)} séries carregadas da DB")
        except Exception as e:
            print(f"[ADAPTIVE] load_from_db: {e}")

    def _persist_one(self, key: str, s: _SeriesState):
        sql = """
            INSERT INTO threshold_overrides
                (tracking_key, alerts_fired, alerts_with_problem,
                 sigma_warn, sigma_high, learning)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tracking_key) DO UPDATE SET
                alerts_fired        = EXCLUDED.alerts_fired,
                alerts_with_problem = EXCLUDED.alerts_with_problem,
                sigma_warn          = EXCLUDED.sigma_warn,
                sigma_high          = EXCLUDED.sigma_high,
                learning            = EXCLUDED.learning,
                updated_at          = NOW()
        """
        try:
            with _conn() as cx, cx.cursor() as cur:
                cur.execute(sql, (
                    key,
                    s.alerts_fired,
                    s.alerts_with_problem,
                    s.sigma_warn,
                    s.sigma_high,
                    s.learning,
                ))
        except Exception as e:
            print(f"[ADAPTIVE] persist_one erro: {e}")
