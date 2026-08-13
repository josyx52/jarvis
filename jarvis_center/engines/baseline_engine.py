"""
BaselineEngine — detecção de anomalias por baselining estatístico dinâmico.

Duas camadas de baseline:

  1. SAZONAL (prioritária)
     Mantém uma rolling window por slot (hora_do_dia, dia_da_semana).
     168 slots possíveis (24 × 7). Um servidor com CPU sempre a 80% às
     segundas às 9h nunca dispara — porque esse é o seu baseline nesse slot.
     Activa a partir de _MIN_SEASONAL amostras no slot actual.

  2. GLOBAL (fallback cold-start)
     Rolling window única por série. Usada enquanto o slot sazonal ainda
     não tem amostras suficientes (primeiros dias de vida do agente).

Regras de disparo (sem threshold fixo hardcoded):
  |z| > SIGMA_WARNING (2.5σ) → severity=medium
  |z| > SIGMA_ANOMALY (3.5σ) → severity=high
"""

import math
import threading
import time
from collections import deque
from datetime import datetime


# ── Janelas ───────────────────────────────────────────────────────────────────

_WINDOW          = 500   # amostras na rolling window global por série
_WINDOW_SLOT     = 120   # amostras na rolling window por slot sazonal
_MIN_SAMPLES     = 30    # mín. para arrancar o baseline global
_MIN_SEASONAL    = 5     # mín. para preferir o baseline sazonal
                         # (5 ocorrências do mesmo slot → ~5 dias se 1 amostra/dia)

# ── Thresholds de z-score ─────────────────────────────────────────────────────

_SIGMA_WARN      = 2.5   # → medium   (~1.2% em dados normais)
_SIGMA_HIGH      = 3.5   # → high     (~0.05% em dados normais)

# Cooldown entre alertas da mesma série
_ALERT_COOLDOWN_S = 120


class BaselineEngine:
    """Motor de baselining dinâmico com sazonalidade hora×dia_semana."""

    def __init__(self):
        # Baseline global: {series_key → deque[float]}
        self._series: dict[str, deque] = {}
        # Baseline sazonal: {series_key → {(hour, weekday) → deque[float]}}
        self._seasonal: dict[str, dict[tuple, deque]] = {}
        # Cooldown: {series_key → last_alert_ts}
        self._last_alert: dict[str, float] = {}
        self._lock = threading.Lock()
        # AdaptiveThresholdEngine — injectado após init (evita dependência circular)
        self._threshold_engine = None

    def set_threshold_engine(self, engine) -> None:
        """Liga o AdaptiveThresholdEngine para sigma adaptativo por série."""
        self._threshold_engine = engine

    # ── Alimentar o engine ────────────────────────────────────────────────────

    def ingest(self,
               host_key:    str,
               snapshot:    dict,
               trace_stats: list[dict] | None = None,
               db_stats:    list[dict] | None = None):
        """Regista amostras actuais para aprendizagem do baseline."""
        self._ingest_infra(host_key, snapshot)
        for stat in (trace_stats or []):
            self._ingest_trace(host_key, stat)
        for stat in (db_stats or []):
            self._ingest_db(host_key, stat)

    def _ingest_infra(self, host_key: str, snapshot: dict):
        metrics = snapshot.get("metrics") or {}
        for key in ("cpu_percent", "memory_percent", "disk_percent"):
            v = _to_float(metrics.get(key))
            if v is not None:
                self._add(f"{host_key}::{key}", v)

        for key in ("net_bytes_recv", "net_bytes_sent"):
            v = _to_float(metrics.get(key))
            if v is not None:
                self._add(f"{host_key}::{key}", v)

    def _ingest_trace(self, host_key: str, stat: dict):
        svc    = stat.get("service", "unknown")
        op     = stat.get("operation", "unknown")
        prefix = f"{host_key}::trace::{svc}::{op}"
        for sub, field in (("p95_ms", "p95_ms"), ("error_rate", "error_rate")):
            v = _to_float(stat.get(field))
            if v is not None:
                self._add(f"{prefix}::{sub}", v)

    def _ingest_db(self, host_key: str, stat: dict):
        db     = stat.get("db", "unknown")
        prefix = f"{host_key}::db::{db}"
        for sub, field in (("avg_query_ms", "avg_query_ms"), ("connections_pct", "connections_pct")):
            v = _to_float(stat.get(field))
            if v is not None:
                self._add(f"{prefix}::{sub}", v)

    # ── Detecção de anomalias ─────────────────────────────────────────────────

    def detect(self,
               host_key:    str,
               snapshot:    dict,
               trace_stats: list[dict] | None = None,
               db_stats:    list[dict] | None = None) -> list[dict]:
        """Retorna eventos de anomalia para o frame actual."""
        events = []
        events.extend(self._detect_infra(host_key, snapshot))
        for stat in (trace_stats or []):
            events.extend(self._detect_trace(host_key, stat))
        for stat in (db_stats or []):
            events.extend(self._detect_db(host_key, stat))
        return events

    def _get_sigmas(self, series_key: str) -> tuple[float, float]:
        """Devolve (sigma_warn, sigma_high) — adaptativo se engine disponível."""
        if self._threshold_engine is not None:
            return self._threshold_engine.get_sigmas(series_key)
        return _SIGMA_WARN, _SIGMA_HIGH

    def _detect_infra(self, host_key: str, snapshot: dict) -> list[dict]:
        metrics = snapshot.get("metrics") or {}
        events  = []
        labels  = {
            "cpu_percent":    "CPU",
            "memory_percent": "Memória",
            "disk_percent":   "Disco",
        }
        for key, label in labels.items():
            v = _to_float(metrics.get(key))
            if v is None:
                continue
            sk = f"{host_key}::{key}"
            sw, sh = self._get_sigmas(sk)
            ev = self._anomaly_event(
                series_key    = sk,
                value         = v,
                event_type    = f"{key}_anomaly",
                entity_type   = "host",
                entity_name   = key.replace("_percent", ""),
                summary_tpl   = f"Anomalia em {label}: {{value:.1f}}% (baseline={{mean:.1f}}%, z={{z:.1f}}σ, modo={{mode}})",
                payload_extra = {"metric": key},
                sigma_warn    = sw,
                sigma_high    = sh,
            )
            if ev:
                events.append(ev)
        return events

    def _detect_trace(self, host_key: str, stat: dict) -> list[dict]:
        svc    = stat.get("service", "unknown")
        op     = stat.get("operation", "unknown")
        prefix = f"{host_key}::trace::{svc}::{op}"
        events = []

        p95 = _to_float(stat.get("p95_ms"))
        if p95 is not None:
            sk = f"{prefix}::p95_ms"
            sw, sh = self._get_sigmas(sk)
            ev = self._anomaly_event(
                series_key    = sk,
                value         = p95,
                event_type    = "trace_latency_anomaly",
                entity_type   = "service",
                entity_name   = svc,
                summary_tpl   = f"Latência anómala em '{svc}' '{op}': {{value:.0f}}ms (baseline={{mean:.0f}}ms, z={{z:.1f}}σ, modo={{mode}})",
                payload_extra = {"service": svc, "operation": op},
                sigma_warn    = sw,
                sigma_high    = sh,
            )
            if ev:
                events.append(ev)

        err = _to_float(stat.get("error_rate"))
        if err is not None:
            sk = f"{prefix}::error_rate"
            sw, sh = self._get_sigmas(sk)
            ev = self._anomaly_event(
                series_key    = sk,
                value         = err,
                event_type    = "trace_error_rate_anomaly",
                entity_type   = "service",
                entity_name   = svc,
                summary_tpl   = f"Taxa de erros anómala em '{svc}' '{op}': {{value:.1%}} (baseline={{mean:.1%}}, z={{z:.1f}}σ, modo={{mode}})",
                payload_extra = {"service": svc, "operation": op},
                sigma_warn    = sw,
                sigma_high    = sh,
            )
            if ev:
                events.append(ev)

        return events

    def _detect_db(self, host_key: str, stat: dict) -> list[dict]:
        db     = stat.get("db", "unknown")
        prefix = f"{host_key}::db::{db}"
        events = []

        avg = _to_float(stat.get("avg_query_ms"))
        if avg is not None:
            sk = f"{prefix}::avg_query_ms"
            sw, sh = self._get_sigmas(sk)
            ev = self._anomaly_event(
                series_key    = sk,
                value         = avg,
                event_type    = "db_query_latency_anomaly",
                entity_type   = "database",
                entity_name   = db,
                summary_tpl   = f"Latência de queries anómala em '{db}': {{value:.0f}}ms (baseline={{mean:.0f}}ms, z={{z:.1f}}σ, modo={{mode}})",
                payload_extra = {"db": db},
                sigma_warn    = sw,
                sigma_high    = sh,
            )
            if ev:
                events.append(ev)

        return events

    # ── Kernel estatístico ────────────────────────────────────────────────────

    def _anomaly_event(self,
                       series_key:    str,
                       value:         float,
                       event_type:    str,
                       entity_type:   str,
                       entity_name:   str,
                       summary_tpl:   str,
                       payload_extra: dict,
                       sigma_warn:    float = _SIGMA_WARN,
                       sigma_high:    float = _SIGMA_HIGH) -> dict | None:
        """
        sigma_warn / sigma_high: valores fixos durante aprendizagem,
        adaptativos após aprendizagem (fornecidos pelo AdaptiveThresholdEngine).
        """
        stats = self._stats(series_key)
        if stats is None:
            return None  # cold start

        mean, stddev, n, mode = stats

        if stddev < 1e-6:
            return None  # série sem variância — baseline não útil

        z     = (value - mean) / stddev
        abs_z = abs(z)

        if abs_z < sigma_warn:
            return None

        now = time.time()
        with self._lock:
            last = self._last_alert.get(series_key, 0)
            if now - last < _ALERT_COOLDOWN_S:
                return None
            self._last_alert[series_key] = now

        severity = "high" if abs_z >= sigma_high else "medium"
        summary  = summary_tpl.format(value=value, mean=mean, z=z, mode=mode)

        return {
            "event_type":  event_type,
            "severity":    severity,
            "entity_type": entity_type,
            "entity_name": entity_name,
            "summary":     summary,
            "series_key":  series_key,   # usado pelo AdaptiveThresholdEngine
            "payload": {
                **payload_extra,
                "value":           round(value, 3),
                "baseline_mean":   round(mean, 3),
                "baseline_stddev": round(stddev, 3),
                "zscore":          round(z, 2),
                "samples":         n,
                "baseline_mode":   mode,   # "seasonal" ou "global"
                "sigma_warn":      round(sigma_warn, 2),
                "sigma_high":      round(sigma_high, 2),
                "detection":       "baseline_anomaly",
            },
        }

    # ── Armazenamento de séries ───────────────────────────────────────────────

    def _add(self, key: str, value: float):
        now  = datetime.now()
        slot = (now.hour, now.weekday())   # (0-23, 0-6)
        with self._lock:
            # Global
            if key not in self._series:
                self._series[key] = deque(maxlen=_WINDOW)
            self._series[key].append(value)
            # Sazonal
            if key not in self._seasonal:
                self._seasonal[key] = {}
            if slot not in self._seasonal[key]:
                self._seasonal[key][slot] = deque(maxlen=_WINDOW_SLOT)
            self._seasonal[key][slot].append(value)

    def _stats(self, key: str) -> tuple[float, float, int, str] | None:
        """Devolve (mean, stddev, n, mode) onde mode é 'seasonal' ou 'global'."""
        now  = datetime.now()
        slot = (now.hour, now.weekday())
        with self._lock:
            slot_d = self._seasonal.get(key, {}).get(slot)
            if slot_d and len(slot_d) >= _MIN_SEASONAL:
                samples = list(slot_d)
                mode    = "seasonal"
            else:
                d = self._series.get(key)
                if d is None or len(d) < _MIN_SAMPLES:
                    return None
                samples = list(d)
                mode    = "global"

        n    = len(samples)
        mean = sum(samples) / n
        var  = sum((x - mean) ** 2 for x in samples) / n
        return mean, math.sqrt(var), n, mode


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_float(v) -> float | None:
    try:
        return float(v)
    except Exception:
        return None
