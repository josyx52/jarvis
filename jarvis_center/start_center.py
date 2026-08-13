import subprocess
import time
import sys
import os
import threading
import socket
from datetime import datetime

import redis
import psycopg2
import requests


# --------------------------------
# DEFINIR DIRETÓRIO DO PROJETO
# --------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

# Carregar .env se existir (antes de ler os.getenv)
_env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_file):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        # fallback manual se python-dotenv não estiver instalado
        with open(_env_file, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    os.environ.setdefault(_k.strip(), _v.strip())

# --------------------------------
# CONFIG — lida do ambiente, sem hardcode
# --------------------------------

PARTITIONS     = int(os.getenv("JARVIS_PARTITIONS", "8"))
API_PORT       = int(os.getenv("JARVIS_API_PORT", "8080"))
API_HOST       = os.getenv("JARVIS_API_HOST", "127.0.0.1")
AGENTS_ENABLED = os.getenv("JARVIS_AGENTS_ENABLED", "1").strip() not in ("0", "false", "no")
SCOPE_COLLECTOR_ENABLED = os.getenv("JARVIS_SCOPE_COLLECTOR_ENABLED", "0").strip() not in ("0", "false", "no")
EXPLORER_ENABLED = os.getenv("JARVIS_EXPLORER_ENABLED", "0").strip() not in ("0", "false", "no")

# Para verificar conectividade usamos sempre loopback — API_HOST pode ser "0.0.0.0"
# que nao e um destino valido em Windows
API_CHECK_HOST = "127.0.0.1"

LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

GROUP = "jarvis-processors"
STREAM_PREFIX = "jarvis:stream:"

MONITOR_INTERVAL = 5
PROCESS_CHECK_INTERVAL = 3
POSTGRES_CHECK_INTERVAL = 10
API_CHECK_INTERVAL = 5

POSTGRES_HOST     = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_DB       = os.getenv("POSTGRES_DB", "jarvis")
POSTGRES_USER     = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")

processes = []
shutdown_flag = False

# snapshots anteriores para análise de delta
last_flow_snapshot = {
    "total_length": 0,
    "total_lag": 0,
    "total_pending": 0,
    "events_count": 0,
    "predictions_count": 0,
    "alerts_count": 0,
    "investigations_count": 0,
    "correlations_count": 0,
}

# --------------------------------
# REDIS CLIENT
# --------------------------------

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True
)


# --------------------------------
# HELPERS
# --------------------------------

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode('ascii', errors='replace').decode('ascii'), flush=True)


def is_port_open(host, port, timeout=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)

    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def postgres_query_count(table_name):
    conn = None
    cur = None

    try:
        conn = psycopg2.connect(
            host=POSTGRES_HOST,
            database=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD
        )
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table_name}")
        return cur.fetchone()[0], None

    except Exception as e:
        return 0, str(e)

    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass

        try:
            if conn:
                conn.close()
        except Exception:
            pass


# --------------------------------
# START REDIS
# --------------------------------

def start_redis():

    log("[CENTER] starting Redis...")

    try:
        if redis_client.ping():
            log("[CENTER] Redis already running")
            return
    except Exception:
        pass

    # No Windows o Redis corre como Windows Service — usar sc/net start.
    # Nomes comuns do serviço: Redis, redis, redis-server.
    _REDIS_SERVICE_NAMES = ["Redis", "redis", "redis-server"]

    started_as_service = False
    for svc_name in _REDIS_SERVICE_NAMES:
        try:
            result = subprocess.run(
                ["sc", "query", svc_name],
                capture_output=True, text=True, timeout=5
            )
            if "SERVICE_NAME" in result.stdout:
                # Serviço existe — iniciar
                start_result = subprocess.run(
                    ["net", "start", svc_name],
                    capture_output=True, text=True, timeout=15
                )
                if start_result.returncode == 0 or "already been started" in start_result.stdout.lower():
                    log(f"[CENTER] Redis service '{svc_name}' started")
                    started_as_service = True
                    break
                else:
                    log(f"[CENTER] net start {svc_name}: {start_result.stdout.strip() or start_result.stderr.strip()}")
        except Exception:
            pass

    if not started_as_service:
        # Fallback: tentar lançar redis-server directamente (Linux / dev)
        try:
            p = subprocess.Popen(
                ["redis-server"],
                cwd=BASE_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append({"name": "redis", "process": p, "managed": True})
            log("[CENTER] redis-server launched as process")
        except Exception as e:
            log(f"[CENTER] Redis not started: {e}")
            log("[CENTER] start Redis manually: net start Redis")
            return

    time.sleep(2)

    try:
        if redis_client.ping():
            log("[CENTER] Redis confirmed up")
        else:
            log("[CENTER] Redis started but ping not confirmed")
    except Exception:
        log("[CENTER] Redis ping failed after start attempt")


# --------------------------------
# START API
# --------------------------------

def _spawn_api_process():
    """Lanca o subprocess uvicorn e devolve o objecto Popen."""
    api_log = open(os.path.join(LOG_DIR, "api.log"), "a", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "api.ingestion_api:app",
            "--host", "0.0.0.0",
            "--port", str(API_PORT),
            "--timeout-keep-alive", "120",
        ],
        cwd=BASE_DIR,
        stdout=api_log,
        stderr=api_log,
        env=env,
    )


def start_api():

    log("[CENTER] starting ingestion API...")

    # Verificar sempre em 127.0.0.1 — API_HOST pode ser "0.0.0.0" que
    # nao e valido como destino de ligacao no Windows
    if is_port_open(API_CHECK_HOST, API_PORT):
        log(f"[CENTER] API port {API_PORT} already in use — a assumir que esta activa")
        return

    try:
        p = _spawn_api_process()

        processes.append({"name": "api", "process": p, "managed": True})

        time.sleep(3)

        if is_port_open(API_CHECK_HOST, API_PORT):
            log(f"[CENTER] API running (port {API_PORT})")
        else:
            log(f"[CENTER] API process launched (pid={p.pid}) — porta ainda nao confirmada")

    except Exception as e:
        log(f"[CENTER] API failed to start: {e}")


# --------------------------------
# START SUPERVISOR
# --------------------------------

def start_supervisor():

    log("[CENTER] starting processor supervisor...")

    run_center_script = os.path.join(BASE_DIR, "run_center.py")

    try:
        p = subprocess.Popen([
            sys.executable,
            run_center_script
        ], cwd=BASE_DIR)

        processes.append({
            "name": "supervisor",
            "process": p,
            "managed": True
        })

        time.sleep(2)

        if p.poll() is None:
            log("[CENTER] Supervisor running")
        else:
            log("[CENTER] Supervisor exited right after start")

    except Exception as e:
        log(f"[CENTER] Supervisor failed to start: {e}")


# --------------------------------
# API HEALTH MONITOR
# --------------------------------

def monitor_api():

    _fail_count = 0
    _MAX_FAILS  = 3  # reinicia apos 3 falhas consecutivas

    while not shutdown_flag:

        try:
            response = requests.get(
                f"http://{API_CHECK_HOST}:{API_PORT}/",
                timeout=10
            )

            if response.status_code == 200:
                if _fail_count > 0:
                    log("[HEALTH][API] up (recuperado)")
                else:
                    log("[HEALTH][API] up")
                _fail_count = 0

            else:
                _fail_count += 1
                log(f"[HEALTH][API] unhealthy status={response.status_code} (falha {_fail_count}/{_MAX_FAILS})")

        except Exception as e:
            _fail_count += 1
            log(f"[HEALTH][API] down error={e} (falha {_fail_count}/{_MAX_FAILS})")

        # Auto-restart apos MAX_FAILS falhas consecutivas
        if _fail_count >= _MAX_FAILS:
            log("[HEALTH][API] a reiniciar API...")
            # Matar processo anterior se ainda existir
            for item in processes:
                if item["name"] == "api":
                    try:
                        if item["process"].poll() is None:
                            item["process"].terminate()
                            time.sleep(1)
                            if item["process"].poll() is None:
                                item["process"].kill()
                    except Exception:
                        pass
                    processes.remove(item)
                    break
            try:
                p = _spawn_api_process()
                processes.append({"name": "api", "process": p, "managed": True})
                time.sleep(3)
                if is_port_open(API_CHECK_HOST, API_PORT):
                    log(f"[HEALTH][API] reiniciado com sucesso (pid={p.pid})")
                    _fail_count = 0
                else:
                    log("[HEALTH][API] reinicio lancado — aguardar proximo ciclo")
                    _fail_count = 0
            except Exception as ex:
                log(f"[HEALTH][API] erro ao reiniciar: {ex}")

        time.sleep(API_CHECK_INTERVAL)


# --------------------------------
# PROCESS HEALTH MONITOR
# --------------------------------

def monitor_processes():

    while not shutdown_flag:

        log("\n[HEALTH][PROCESSES]")

        for item in processes:

            name = item["name"]
            proc = item["process"]
            managed = item["managed"]

            rc = proc.poll()

            if rc is None:
                log(f"  {name}: running")
            else:
                log(f"  {name}: exited code={rc} managed={managed}")

        time.sleep(PROCESS_CHECK_INTERVAL)


# --------------------------------
# REDIS STREAM DETAIL
# --------------------------------

def get_stream_metrics(partition):

    stream = f"{STREAM_PREFIX}{partition}"

    metrics = {
        "partition": partition,
        "stream": stream,
        "exists": False,
        "length": 0,
        "radix_tree_keys": 0,
        "radix_tree_nodes": 0,
        "groups": 0,
        "last_id": "0-0",
        "lag": 0,
        "pending": 0,
        "consumers": 0,
        "consumer_details": []
    }

    try:
        if not redis_client.exists(stream):
            return metrics

        info = redis_client.xinfo_stream(stream)

        metrics["exists"] = True
        metrics["length"] = int(info.get("length", 0))
        metrics["radix_tree_keys"] = int(info.get("radix-tree-keys", 0))
        metrics["radix_tree_nodes"] = int(info.get("radix-tree-nodes", 0))
        metrics["groups"] = int(info.get("groups", 0))
        metrics["last_id"] = info.get("last-generated-id", "0-0")

        groups = redis_client.xinfo_groups(stream)

        for group in groups:
            if group.get("name") == GROUP:
                metrics["lag"] = int(group.get("lag") or 0)
                metrics["pending"] = int(group.get("pending") or 0)
                metrics["consumers"] = int(group.get("consumers") or 0)
                break

        try:
            consumers = redis_client.xinfo_consumers(stream, GROUP)

            for consumer in consumers:
                metrics["consumer_details"].append({
                    "name": consumer.get("name"),
                    "pending": int(consumer.get("pending", 0)),
                    "idle": int(consumer.get("idle", 0))
                })
        except Exception:
            pass

    except Exception:
        return metrics

    return metrics


# --------------------------------
# POSTGRES MONITOR
# --------------------------------

def monitor_postgres():

    while not shutdown_flag:

        log("\n[HEALTH][POSTGRES]")

        tables = [
            "events",
            "correlations",
            "predictions",
            "alerts",
            "investigations"
        ]

        table_results = {}

        has_error = False

        for table in tables:
            count, err = postgres_query_count(table)

            if err:
                has_error = True
                table_results[table] = {
                    "count": 0,
                    "error": err
                }
            else:
                table_results[table] = {
                    "count": count,
                    "error": None
                }

        if has_error:
            log("  status: ERROR")
        else:
            log("  status: OK")

        for table, result in table_results.items():
            if result["error"]:
                log(f"  {table}: ERROR -> {result['error']}")
            else:
                log(f"  {table}: {result['count']}")

        time.sleep(POSTGRES_CHECK_INTERVAL)


# --------------------------------
# FLOW MONITOR
# --------------------------------

def monitor_flow():

    global last_flow_snapshot

    while not shutdown_flag:

        total_length = 0
        total_lag = 0
        total_pending = 0

        log("\n================ FLOW =================")
        log(f"[FLOW][TIME] {now_str()}")
        log("\n[FLOW][REDIS STREAMS]")

        stream_metrics = []

        for i in range(PARTITIONS):

            m = get_stream_metrics(i)
            stream_metrics.append(m)

            if not m["exists"]:
                log(
                    f"  partition={i} exists=no "
                    f"len=0 lag=0 pending=0 consumers=0 last_id=0-0"
                )
                continue

            log(
                f"  partition={i} exists=yes "
                f"len={m['length']} lag={m['lag']} pending={m['pending']} "
                f"consumers={m['consumers']} groups={m['groups']} "
                f"last_id={m['last_id']}"
            )

            total_length += m["length"]
            total_lag += m["lag"]
            total_pending += m["pending"]

        log("\n[FLOW][REDIS SUMMARY]")
        log(f"  total_length  = {total_length}")
        log(f"  total_lag     = {total_lag}")
        log(f"  total_pending = {total_pending}")

        event_count, event_err = postgres_query_count("events")
        corr_count, corr_err = postgres_query_count("correlations")
        pred_count, pred_err = postgres_query_count("predictions")
        alert_count, alert_err = postgres_query_count("alerts")
        inv_count, inv_err = postgres_query_count("investigations")

        log("\n[FLOW][DATABASE SUMMARY]")

        if event_err:
            log(f"  events         = ERROR -> {event_err}")
        else:
            log(f"  events         = {event_count}")

        if corr_err:
            log(f"  correlations   = ERROR -> {corr_err}")
        else:
            log(f"  correlations   = {corr_count}")

        if pred_err:
            log(f"  predictions    = ERROR -> {pred_err}")
        else:
            log(f"  predictions    = {pred_count}")

        if alert_err:
            log(f"  alerts         = ERROR -> {alert_err}")
        else:
            log(f"  alerts         = {alert_count}")

        if inv_err:
            log(f"  investigations = ERROR -> {inv_err}")
        else:
            log(f"  investigations = {inv_count}")

        delta_length = total_length - last_flow_snapshot["total_length"]
        delta_lag = total_lag - last_flow_snapshot["total_lag"]
        delta_pending = total_pending - last_flow_snapshot["total_pending"]
        delta_events = event_count - last_flow_snapshot["events_count"]
        delta_corr = corr_count - last_flow_snapshot["correlations_count"]
        delta_pred = pred_count - last_flow_snapshot["predictions_count"]
        delta_alerts = alert_count - last_flow_snapshot["alerts_count"]
        delta_inv = inv_count - last_flow_snapshot["investigations_count"]

        log("\n[FLOW][DELTA] (desde o último ciclo)")
        log(f"  Δlength         = {delta_length}")
        log(f"  Δlag            = {delta_lag}")
        log(f"  Δpending        = {delta_pending}")
        log(f"  Δevents         = {delta_events}")
        log(f"  Δcorrelations   = {delta_corr}")
        log(f"  Δpredictions    = {delta_pred}")
        log(f"  Δalerts         = {delta_alerts}")
        log(f"  Δinvestigations = {delta_inv}")

        log("\n[FLOW][INTERPRETAÇÃO]")

        if total_lag > 0:
            log("  ❌ BACKLOG REAL: os processors não estão a acompanhar o consumo.")
        else:
            log("  ✅ SEM BACKLOG REAL: os processors estão a consumir as mensagens novas.")

        if total_pending > 0:
            log("  ⚠️ HÁ MENSAGENS PENDENTES: houve leitura sem finalização/ACK em alguma etapa.")
        else:
            log("  ✅ SEM PENDING: não há mensagens presas no consumer group.")

        if total_lag == 0 and total_pending > 0:
            log("  ⚠️ O PROBLEMA NÃO ESTÁ NA FILA: está depois da leitura, dentro do processamento.")

        if delta_events > 0 or delta_corr > 0 or delta_pred > 0 or delta_alerts > 0 or delta_inv > 0:
            log("  ✅ O PIPELINE ESTÁ GERANDO VALOR: houve crescimento em tabelas do runtime.")
        else:
            log("  ⚠️ NÃO HOUVE NOVO VALOR PERSISTIDO NESTE CICLO.")

        if event_err or corr_err or pred_err or alert_err or inv_err:
            log("  ❌ HÁ ERRO DE BANCO: persistência falhando em pelo menos uma tabela.")
        else:
            log("  ✅ BANCO ACESSÍVEL: consultas às tabelas principais estão a funcionar.")

        hot_partitions = [m for m in stream_metrics if m["length"] > 0]
        if hot_partitions:
            hottest = sorted(hot_partitions, key=lambda x: x["length"], reverse=True)[0]
            log(
                f"  ℹ️ PARTIÇÃO MAIS CARREGADA: p{hottest['partition']} "
                f"(len={hottest['length']}, pending={hottest['pending']}, consumers={hottest['consumers']})"
            )

        stuck_partitions = [m for m in stream_metrics if m["pending"] > 0]
        if stuck_partitions:
            parts = ", ".join(
                [f"p{m['partition']}({m['pending']})" for m in stuck_partitions]
            )
            log(f"  ⚠️ PARTIÇÕES COM PENDING: {parts}")

        log("\n[FLOW][CONSUMERS DETAIL]")

        for m in stream_metrics:
            if not m["consumer_details"]:
                continue

            for c in m["consumer_details"]:
                log(
                    f"  partition={m['partition']} "
                    f"consumer={c['name']} pending={c['pending']} idle_ms={c['idle']}"
                )

        log("======================================\n")

        last_flow_snapshot = {
            "total_length": total_length,
            "total_lag": total_lag,
            "total_pending": total_pending,
            "events_count": event_count,
            "predictions_count": pred_count,
            "alerts_count": alert_count,
            "investigations_count": inv_count,
            "correlations_count": corr_count,
        }

        time.sleep(MONITOR_INTERVAL)


# --------------------------------
# MAIN
# --------------------------------

def init_schema():
    """Aplica o schema completo na base de dados (idempotente)."""
    log("[CENTER] a inicializar schema da base de dados...")
    schema_path = os.path.join(BASE_DIR, "storage", "schema_init.sql")
    if not os.path.exists(schema_path):
        log("[CENTER] schema_init.sql não encontrado — a ignorar")
        return
    conn = None
    try:
        conn = psycopg2.connect(
            host=POSTGRES_HOST,
            database=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD
        )
        conn.autocommit = True
        cur = conn.cursor()
        with open(schema_path, "r", encoding="utf-8") as f:
            sql = f.read()
        # Executa statement a statement para ignorar erros pontuais
        import re
        statements = [s.strip() for s in re.split(r';', sql) if s.strip()]
        ok = 0
        for stmt in statements:
            try:
                cur.execute(stmt)
                ok += 1
            except Exception as e:
                log(f"[SCHEMA] aviso: {e}")
        cur.close()
        log(f"[CENTER] schema aplicado ({ok}/{len(statements)} statements OK)")
    except Exception as e:
        log(f"[CENTER] erro a inicializar schema: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _retention_worker():
    """Apaga dados antigos a cada 6 horas para manter o disco controlado."""
    import psycopg2 as _pg
    RETENTION = {
        "connections":   "7 days",
        "events":        "7 days",
        "investigations":"7 days",
        "reasoning":     "7 days",
        "alerts":        "30 days",
        "snapshots":     "7 days",
    }
    while not shutdown_flag:
        time.sleep(6 * 3600)
        try:
            conn = _pg.connect(
                host=POSTGRES_HOST, database=POSTGRES_DB,
                user=POSTGRES_USER, password=POSTGRES_PASSWORD
            )
            conn.autocommit = True
            cur = conn.cursor()
            for table, interval in RETENTION.items():
                try:
                    cur.execute(
                        f"DELETE FROM {table} WHERE created_at < NOW() - INTERVAL %s",
                        (interval,)
                    )
                    log(f"[RETENTION] {table}: {cur.rowcount} linhas apagadas")
                except Exception as e:
                    log(f"[RETENTION] {table}: erro {e}")
            cur.close()
            conn.close()
        except Exception as e:
            log(f"[RETENTION] erro de ligação: {e}")


def main():

    global shutdown_flag

    print("\n==============================")
    print("      JARVIS CENTER START")
    print("==============================\n")

    start_redis()
    init_schema()
    start_api()

    if AGENTS_ENABLED:
        start_supervisor()
        from engines import osquery_updater
        osquery_updater.start(daemon=True)
    else:
        log("[CENTER] AGENTS_ENABLED=0 — supervisor e osquery-updater desativados")

    log("\n[CENTER] system ready")
    if AGENTS_ENABLED:
        log("[CENTER] full control mode enabled")
    else:
        log("[CENTER] API-only mode (agentless + chat proxy activos; reasoning engine desativado)")
    log("[CENTER] press CTRL+C to stop everything\n")

    from lachesis.lachesis_scheduler import lachesis_scheduler_worker
    from rootreset.scheduler import rootreset_scheduler_worker

    monitor_threads = [
        threading.Thread(target=monitor_api, daemon=True),
        threading.Thread(target=monitor_processes, daemon=True),
        threading.Thread(target=monitor_postgres, daemon=True),
        threading.Thread(target=_retention_worker, daemon=True),
        threading.Thread(target=lachesis_scheduler_worker, daemon=True),
        threading.Thread(target=rootreset_scheduler_worker, daemon=True),
    ]
    if AGENTS_ENABLED:
        monitor_threads.append(threading.Thread(target=monitor_flow, daemon=True))
    if SCOPE_COLLECTOR_ENABLED:
        from scope_collector.collector import scope_collector_worker
        monitor_threads.append(threading.Thread(target=scope_collector_worker, daemon=True))
        log("[CENTER] Scope Collector activo (JARVIS_SCOPE_COLLECTOR_ENABLED=1)")
    if EXPLORER_ENABLED:
        from explorer.explorer_scheduler import explorer_scheduler_worker
        monitor_threads.append(threading.Thread(target=explorer_scheduler_worker, daemon=True))
        log("[CENTER] Explorer Engine activo (JARVIS_EXPLORER_ENABLED=1)")

    for t in monitor_threads:
        t.start()

    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:

        shutdown_flag = True
        osquery_updater.stop()

        log("\n[CENTER] shutting down")

        for item in processes:
            p = item["process"]

            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass

        time.sleep(2)

        for item in processes:
            p = item["process"]

            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass

        log("[CENTER] stopped")


if __name__ == "__main__":
    main()