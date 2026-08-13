"""
InstrumentationEngine — AI-assisted auto-instrumentation.

Compara processos a correr com cobertura OTel actual.
Usa LLM para identificar gaps e gerar recomendações de instrumentação.

Fluxo:
  CentralRuntime.process_frame()
    → instrumentation_engine.analyze_gaps()
      → LLM prioriza quais processos instrumentar
    → recomendações guardadas em DB
    → agent GET /instrumentation/pending
    → ProcessInjector.inject_running(pid, tech)
"""

import json
import time

from brain.foundry_client import FoundryClient

_COOLDOWN_S        = 600   # 10 min entre análises do mesmo host
_MIN_CPU_PCT       = 5.0   # ignorar processos com CPU < 5%
_MIN_MEM_MB        = 50.0  # ignorar processos com memória < 50 MB


class InstrumentationEngine:

    def __init__(self):
        self.llm = FoundryClient()
        self._last: dict[str, float] = {}   # host_key → last analysis ts

    def analyze_gaps(
        self,
        host_key: str,
        snapshot: dict,
        problems: list[dict],
        alerts:   list[dict],
        traces:   list[dict],
    ) -> list[dict]:
        """
        Retorna lista de recomendações de instrumentação.
        Cada item: {host, pid, process_name, technology, priority, reason, source, status}
        """
        now = time.time()
        if now - self._last.get(host_key, 0) < _COOLDOWN_S:
            return []

        processes = snapshot.get("processes") or []
        if not processes:
            return []

        traced_services = _extract_traced_services(traces)
        candidates      = _find_candidates(processes, traced_services)

        if not candidates:
            return []

        recs = self._llm_prioritize(host_key, candidates, problems, alerts)
        if recs:
            self._last[host_key] = now
        return recs

    # ── LLM prioritisation ───────────────────────────────────────────────────

    def _llm_prioritize(
        self,
        host_key:   str,
        candidates: list[dict],
        problems:   list[dict],
        alerts:     list[dict],
    ) -> list[dict]:

        hostname = host_key.split("::")[0]

        active_problems = "\n".join(
            f"  [{p.get('severity')}] {p.get('title')}"
            for p in (problems or [])
            if p.get("status") == "open"
        ) or "  (nenhum)"

        recent_alerts = "\n".join(
            f"  [{a.get('severity')}] {a.get('title', '')}"
            for a in (alerts or [])[:5]
        ) or "  (nenhum)"

        candidates_text = "\n".join(
            f"  pid={c['pid']:6}  name={c['name']:20}  "
            f"cpu={c['cpu']:5.1f}%  mem={c['mem_mb']:6.0f}MB  tech={c['technology']}"
            for c in candidates
        )

        prompt = f"""You are an infrastructure observability expert.

Host: {hostname}

Processos com consumo de recursos significativo mas SEM instrumentação OpenTelemetry:
{candidates_text}

Problems activos no host:
{active_problems}

Alertas recentes no host:
{recent_alerts}

TAREFA:
Selecciona até 3 processos que mais beneficiariam de instrumentação OTel.
Prioriza processos relacionados com os problems activos.

Responde EXCLUSIVAMENTE em JSON (array), sem texto adicional:
[
  {{"process_name": "...", "priority": "urgent|normal", "reason": "frase curta em português"}}
]
"""

        try:
            raw  = self.llm.generate(prompt, max_tokens=512)
            i, j = raw.find("["), raw.rfind("]") + 1
            if i < 0 or j <= i:
                return []
            parsed = json.loads(raw[i:j])
        except Exception as e:
            print("[INSTRUMENTATION ENGINE] LLM error:", e)
            return []

        name_map = {c["name"]: c for c in candidates}
        recs     = []

        for r in parsed[:3]:
            pname = (r.get("process_name") or "").lower().replace(".exe", "")
            cand  = name_map.get(pname)
            if cand is None:
                # fuzzy match — nome pode ter capitalização diferente
                for k, v in name_map.items():
                    if pname in k or k in pname:
                        cand = v
                        break
            if cand is None:
                continue

            recs.append({
                "host":         host_key,
                "pid":          cand["pid"],
                "process_name": cand["name"],
                "technology":   cand["technology"],
                "priority":     r.get("priority", "normal"),
                "reason":       r.get("reason", ""),
                "source":       "ai_instrumentation_engine",
                "status":       "pending",
            })

        return recs


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_traced_services(traces: list[dict]) -> set[str]:
    out = set()
    for span in (traces or []):
        svc = (
            span.get("service")
            or (span.get("attributes") or {}).get("service.name")
            or ""
        )
        if svc:
            out.add(svc.lower().replace(".exe", ""))
    return out


def _find_candidates(processes: list[dict], traced: set[str]) -> list[dict]:
    out = []
    for p in processes:
        name = (p.get("name") or "").lower().replace(".exe", "")
        cpu  = float(p.get("cpu_percent") or p.get("percent_processor_time") or 0)
        mem  = float(p.get("resident_size") or 0) / 1_048_576

        if cpu < _MIN_CPU_PCT and mem < _MIN_MEM_MB:
            continue
        if name in traced:
            continue

        tech = detect_technology(name)
        if not tech:
            continue

        out.append({
            "pid":        p.get("pid"),
            "name":       name,
            "cpu":        round(cpu, 1),
            "mem_mb":     round(mem, 1),
            "technology": tech,
        })

    # ordenar por CPU desc, pegar top 10 para o prompt
    out.sort(key=lambda x: x["cpu"], reverse=True)
    return out[:10]


def detect_technology(name: str) -> str | None:
    n = name.lower().replace(".exe", "")
    if (n.startswith("python")
            or n in ("gunicorn", "uvicorn", "waitress", "hypercorn",
                     "granian", "daphne", "meinheld")):
        return "python"
    if n in ("node", "nodemon", "tsx", "ts-node") or n.startswith("node"):
        return "nodejs"
    if n.startswith("java") and n not in ("javaws", "javadoc"):
        return "java"
    if n in ("w3wp", "dotnet", "iisexpress", "aspnetcore"):
        return "dotnet"
    return None
