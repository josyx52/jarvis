import os, sys, json, time, requests, psycopg2, psycopg2.extras
from datetime import datetime, timezone

def _conn():
    return psycopg2.connect(
        host=os.getenv("JARVIS_DB_HOST", "localhost"),
        database=os.getenv("JARVIS_DB_NAME", "jarvis"),
        user=os.getenv("JARVIS_DB_USER", "postgres"),
        password=os.getenv("JARVIS_DB_PASS", "Vermelho555@"),
    )

def main():
    conn = _conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Webhook activo
    cur.execute("SELECT url FROM notification_webhooks WHERE active=TRUE LIMIT 1")
    wh = cur.fetchone()
    if not wh:
        print("Nenhum webhook activo.")
        return
    webhook_url = wh["url"]

    # Problema mais rico (mais alertas correlacionados, aberto)
    cur.execute("""
        SELECT problem_id, host, title, severity, status, alert_titles,
               opened_at, last_seen, alert_count
        FROM problems
        WHERE status = 'open'
        ORDER BY alert_count DESC, opened_at DESC
        LIMIT 1
    """)
    prob = cur.fetchone()
    if not prob:
        print("Sem problemas abertos.")
        return

    host = prob["host"]

    # Reasoning AI real
    cur.execute("""
        SELECT summary, details FROM reasoning
        WHERE host = %s ORDER BY created_at DESC LIMIT 1
    """, (host,))
    reasoning = cur.fetchone()

    # Snapshot mais recente
    cur.execute("""
        SELECT cpu_percent, memory_percent, disk_percent,
               top_cpu_process, top_cpu_value, top_memory_process, top_memory_value
        FROM snapshots WHERE host = %s ORDER BY snapshot_time DESC LIMIT 1
    """, (host,))
    snap = cur.fetchone()

    # Serviços parados
    cur.execute("""
        SELECT payload FROM alerts
        WHERE host = %s AND title = 'service_stopped'
        ORDER BY created_at DESC LIMIT 1
    """, (host,))
    svc_alert = cur.fetchone()
    services_down = []
    if svc_alert and svc_alert["payload"]:
        p = svc_alert["payload"]
        if isinstance(p, dict):
            services_down = p.get("services_down") or p.get("stopped_services") or []

    conn.close()

    hostname  = host.split("::")[0]
    opened_dt = prob["opened_at"]
    duration  = round((datetime.now(timezone.utc) - opened_dt.replace(tzinfo=timezone.utc)).total_seconds() / 60, 1)

    root_cause = ""
    technical  = ""
    if reasoning:
        details = reasoning["details"] or ""
        # Extrai primeiros 2 parágrafos do markdown
        lines = [l for l in details.split("\n") if l.strip() and not l.startswith("#")]
        root_cause = lines[0] if lines else reasoning["summary"]
        technical  = "\n".join(lines[1:4]) if len(lines) > 1 else ""

    payload = {
        "notification_id": f"notif_{int(time.time())}",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "problem": {
            "id":               prob["problem_id"],
            "title":            prob["title"],
            "severity":         prob["severity"],
            "host":             hostname,
            "opened_at":        opened_dt.isoformat(),
            "duration_minutes": duration,
            "alert_types":      prob["alert_titles"] or [],
            "alert_count":      prob["alert_count"],
        },
        "machine_state": {
            "cpu_percent":    float(snap["cpu_percent"])    if snap else None,
            "memory_percent": float(snap["memory_percent"]) if snap else None,
            "disk_percent":   float(snap["disk_percent"])   if snap else None,
            "top_processes": [
                {"name": snap["top_cpu_process"],    "cpu_pct": float(snap["top_cpu_value"] or 0),    "mem_mb": 0},
                {"name": snap["top_memory_process"], "cpu_pct": 0, "mem_mb": round(float(snap["top_memory_value"] or 0) / 1_048_576, 0)},
            ] if snap else [],
            "services_down": services_down,
        },
        "investigation": {
            "root_cause":       root_cause,
            "technical_detail": technical,
            "impact":           f"{len(prob['alert_titles'])} tipos de alerta correlacionados no host {hostname}",
            "urgency":          "now" if prob["severity"] == "critical" else "soon",
            "ai_analysis":      (reasoning["details"] or "")[:1500] if reasoning else "",
        },
        "proposed_action": {
            "action_type":   "investigate",
            "risk_level":    "low",
            "script":        "",
            "auto_executed": False,
        },
    }

    print(f"Host:      {hostname}")
    print(f"Problema:  {prob['title']} ({prob['severity']})")
    print(f"Alertas:   {prob['alert_titles']}")
    print(f"Duração:   {duration} min")
    print(f"Disco:     {snap['disk_percent']}%" if snap else "")
    print(f"Root cause: {root_cause[:100]}")
    print()

    r = requests.post(webhook_url, json=payload, timeout=10)
    print(f"Resposta: HTTP {r.status_code}")
    if r.status_code in (200, 202):
        print("Flow disparado com sucesso.")
    else:
        print("Resposta:", r.text[:300])

if __name__ == "__main__":
    main()
