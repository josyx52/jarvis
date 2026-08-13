"""
MachineProfileEngine — constrói perfis de máquina a partir dos dados históricos.

Para cada host analisa:
  - Serviços e processos mais frequentes (snapshots raw_json)
  - Padrões de alertas (o que falha, com que frequência)
  - Reasoning histórico (o que o Jarvis já concluiu sobre este host)
  - Métricas baseline (CPU/mem/disco médios e picos)

Usa AI para classificar:
  - role: web_server, database, domain_controller, file_server, workstation, etc.
  - env: production, staging, dev (inferido por nome + actividade)
  - critical_services: lista de serviços cujo paragem = impacto de negócio
  - business_impact: low / medium / high / critical
  - summary: descrição curta em português

Grava em server_profiles.
"""

import json
import os
import sys
import argparse
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

_here = os.path.dirname(os.path.abspath(__file__))
_center = os.path.dirname(_here)   # jarvis_center/ — contains brain/
sys.path.insert(0, _here)
sys.path.insert(0, _center)


def _conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        dbname=os.getenv("POSTGRES_DB", "jarvis"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", "Vermelho555@"),
    )


def _get_hosts(conn, host_filter: str = "") -> list[str]:
    cur = conn.cursor()
    if host_filter:
        cur.execute(
            "SELECT DISTINCT host FROM snapshots WHERE host ILIKE %s ORDER BY host",
            (f"%{host_filter}%",),
        )
    else:
        cur.execute("SELECT DISTINCT host FROM snapshots ORDER BY host")
    return [r[0] for r in cur.fetchall()]


def _collect_host_data(conn, host: str) -> dict:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Métricas baseline
    cur.execute("""
        SELECT
            ROUND(AVG(cpu_percent)::numeric,1)    AS avg_cpu,
            ROUND(MAX(cpu_percent)::numeric,1)    AS max_cpu,
            ROUND(AVG(memory_percent)::numeric,1) AS avg_mem,
            ROUND(AVG(disk_percent)::numeric,1)   AS avg_disk,
            ROUND(MAX(disk_percent)::numeric,1)   AS max_disk,
            COUNT(*)                              AS snapshot_count,
            MIN(snapshot_time)                    AS first_seen,
            MAX(snapshot_time)                    AS last_seen
        FROM snapshots WHERE host = %s
    """, (host,))
    metrics = dict(cur.fetchone() or {})

    # top processes do snapshot mais recente
    cur.execute(
        "SELECT top_cpu_process, top_memory_process FROM snapshots WHERE host = %s ORDER BY snapshot_time DESC LIMIT 1",
        (host,),
    )
    top_row = cur.fetchone()
    if top_row:
        metrics["top_cpu_process"]    = top_row["top_cpu_process"]
        metrics["top_memory_process"] = top_row["top_memory_process"]

    # Serviços e processos do snapshot mais recente
    cur.execute(
        "SELECT raw_json FROM snapshots WHERE host = %s ORDER BY snapshot_time DESC LIMIT 1",
        (host,),
    )
    row = cur.fetchone()
    services, processes, ports = [], [], []
    if row and row["raw_json"]:
        snap = row["raw_json"] if isinstance(row["raw_json"], dict) else json.loads(row["raw_json"])
        services  = snap.get("services",  []) or []
        processes = snap.get("processes", []) or []
        network   = snap.get("network",   []) or []
        ports     = sorted({c.get("laddr", {}).get("port") for c in network
                            if c.get("status") == "LISTEN" and c.get("laddr", {}).get("port")}
                           - {None})[:20]

    # Top serviços em execução
    running_services = [
        s.get("name") for s in services
        if (s.get("state") or s.get("status") or "").lower() in ("running", "started")
        and s.get("name")
    ][:30]

    stopped_services = [
        s.get("name") for s in services
        if (s.get("state") or s.get("status") or "").lower() in ("stopped", "down")
        and s.get("name")
    ][:20]

    top_processes = [
        p.get("name") for p in sorted(
            processes, key=lambda p: float(p.get("cpu_percent") or 0), reverse=True
        )[:20] if p.get("name")
    ]

    # Padrão de alertas (últimos 30 dias)
    cur.execute("""
        SELECT title, severity, COUNT(*) as cnt
        FROM alerts WHERE host = %s
        AND created_at > NOW() - INTERVAL '30 days'
        GROUP BY title, severity ORDER BY cnt DESC LIMIT 15
    """, (host,))
    alert_pattern = [dict(r) for r in cur.fetchall()]

    # Último reasoning
    cur.execute(
        "SELECT summary, details FROM reasoning WHERE host = %s ORDER BY created_at DESC LIMIT 1",
        (host,),
    )
    row = cur.fetchone()
    last_reasoning = (row["details"] or "")[:1500] if row else ""

    # Problemas abertos
    cur.execute(
        "SELECT title, severity, alert_titles, alert_count FROM problems WHERE host = %s AND status='open' ORDER BY alert_count DESC LIMIT 5",
        (host,),
    )
    open_problems = [dict(r) for r in cur.fetchall()]

    return {
        "host":             host,
        "hostname":         host.split("::")[0],
        "metrics":          metrics,
        "running_services": running_services,
        "stopped_services": stopped_services,
        "top_processes":    top_processes,
        "listening_ports":  ports,
        "alert_pattern":    alert_pattern,
        "last_reasoning":   last_reasoning,
        "open_problems":    open_problems,
    }


def _build_prompt(data: dict) -> str:
    hostname = data["hostname"]
    m = data["metrics"]

    return f"""Analisa os dados recolhidos pelo sistema de monitorização Jarvis sobre o host Windows Server abaixo e constrói o perfil desta máquina.

## Host
Nome: {hostname}
Snapshots recolhidos: {m.get('snapshot_count', 0)}

## Métricas baseline
CPU média: {m.get('avg_cpu')}% | CPU máx: {m.get('max_cpu')}%
Memória média: {m.get('avg_mem')}%
Disco médio: {m.get('avg_disk')}% | Disco máx: {m.get('max_disk')}%
Processo topo CPU: {m.get('top_cpu_process')}
Processo topo memória: {m.get('top_memory_process')}

## Serviços em execução (amostra)
{json.dumps(data['running_services'][:20], ensure_ascii=False)}

## Portas em escuta
{data['listening_ports']}

## Processos mais activos
{json.dumps(data['top_processes'][:15], ensure_ascii=False)}

## Padrão de alertas (últimos 30 dias)
{json.dumps(data['alert_pattern'], ensure_ascii=False)}

## Problemas abertos
{json.dumps(data['open_problems'], ensure_ascii=False)}

## Última análise AI sobre este host
{data['last_reasoning'][:800] if data['last_reasoning'] else 'Sem análise anterior.'}

---

Com base nestes dados, responde APENAS com um JSON válido (sem markdown, sem texto antes ou depois) com a estrutura:

{{
  "role": "web_server | database_server | domain_controller | file_server | application_server | workstation | monitoring | mixed | unknown",
  "role_detail": "descrição curta do que esta máquina faz (1 linha)",
  "env": "production | staging | dev | unknown",
  "env_confidence": "high | medium | low",
  "critical_services": ["lista de nomes de serviços cuja paragem causa impacto directo no negócio"],
  "important_services": ["serviços importantes mas não críticos"],
  "business_impact": "critical | high | medium | low",
  "business_impact_reason": "razão de 1 linha para o nível de impacto",
  "tech_stack": ["tecnologias identificadas: IIS, SQL Server, Active Directory, etc."],
  "risk_flags": ["problemas estruturais detectados, ex: disco crónico acima de 85%, CPU frequentemente a 100%"],
  "summary": "resumo de 2-3 linhas em português sobre o que esta máquina é e qual o seu papel no negócio"
}}"""


def _call_ai(prompt: str) -> dict | None:
    provider = os.getenv("LLM_PROVIDER", "foundry")

    if provider == "foundry":
        try:
            from brain.foundry_client import FoundryClient
            client = FoundryClient()
            full_prompt = (
                "És um especialista em infraestrutura Windows. Respondes APENAS com JSON válido, sem texto adicional.\n\n"
                + prompt
            )
            text = client.generate(full_prompt, max_tokens=1024, temperature=0.1).strip()
            # Remover markdown se presente
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text)
        except Exception as e:
            print(f"  [AI] Foundry falhou: {e}")

    # Fallback: Ollama
    try:
        import requests
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        r = requests.post(
            f"{ollama_url}/api/generate",
            json={"model": "llama3.2", "prompt": prompt, "stream": False, "format": "json"},
            timeout=60,
        )
        return json.loads(r.json().get("response", "{}"))
    except Exception as e:
        print(f"  [AI] Ollama falhou: {e}")
        return None


def _save_profile(conn, host: str, profile: dict):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO server_profiles (host, profile, setup_by, setup_at, updated_at)
        VALUES (%s, %s, 'machine_profile_engine', NOW(), NOW())
        ON CONFLICT (host) DO UPDATE
          SET profile    = EXCLUDED.profile,
              updated_at = NOW()
    """, (host, json.dumps(profile, ensure_ascii=False)))
    conn.commit()


def run(host_filter: str = ""):
    conn = _conn()
    hosts = _get_hosts(conn, host_filter)
    print(f"[MachineProfileEngine] {len(hosts)} host(s) a processar")

    for host in hosts:
        hostname = host.split("::")[0]
        print(f"\n  [{hostname}] a recolher dados...")
        try:
            data    = _collect_host_data(conn, host)
            prompt  = _build_prompt(data)
            print(f"  [{hostname}] a chamar AI...")
            profile = _call_ai(prompt)
            if not profile:
                print(f"  [{hostname}] AI não respondeu — a ignorar")
                continue
            _save_profile(conn, host, profile)
            print(f"  [{hostname}] perfil guardado: role={profile.get('role')} env={profile.get('env')} impact={profile.get('business_impact')}")
            print(f"    {profile.get('summary', '')[:120]}")
        except Exception as e:
            print(f"  [{hostname}] ERRO: {e}")
            conn.rollback()

    conn.close()
    print("\n[MachineProfileEngine] concluído")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="", help="Filtro de host (parcial)")
    args = parser.parse_args()

    # Garantir que o .env é lido
    env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

    run(host_filter=args.host)
