"""
Jarvis Center — Instalador
Compilar em .exe com: build_installer.bat
"""

import sys
import os
import re
import shutil
import subprocess
import ctypes


INSTALL_DIR = r"C:\JarvisCenter"
LOG_DIR     = os.path.join(INSTALL_DIR, "logs")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {label}{suffix}: ").strip()
    return val if val else default


def get_source_path() -> str:
    """Devolve o caminho para os ficheiros do center (bundle ou script)."""
    if hasattr(sys, "_MEIPASS"):
        # exe compilado: ficheiros em sys._MEIPASS/jarvis_center/
        return os.path.join(sys._MEIPASS, "jarvis_center")
    # execução directa: ficheiros no mesmo directório do script
    return os.path.dirname(os.path.abspath(__file__))


def patch_file(path: str, replacements: list[tuple[str, str]]):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    for old, new in replacements:
        content = content.replace(old, new)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


def main():
    print()
    print("=" * 60)
    print("         JARVIS CENTER  |  Instalador")
    print("=" * 60)
    print()

    # ── 0. Admin ──────────────────────────────────────────────
    if not is_admin():
        print("[ERRO] Execute como Administrador.")
        input("\nPressione Enter para sair...")
        sys.exit(1)

    # ── 1. Python ─────────────────────────────────────────────
    print("[1/6] Verificar Python...")
    try:
        r = run([sys.executable, "--version"], capture_output=True, text=True)
        print(f"       {r.stdout.strip() or r.stderr.strip()}")
    except Exception as e:
        print(f"[ERRO] {e}")
        input("\nPressione Enter para sair...")
        sys.exit(1)
    print()

    # ── 2. Configuração ────────────────────────────────────────
    print("[2/6] Configuração")
    print("-" * 55)
    print()

    pg_host     = ask("Host PostgreSQL       ", "localhost")
    pg_port     = ask("Porta PostgreSQL       ", "5432")
    pg_db       = ask("Base de dados          ", "jarvis")
    pg_user     = ask("Utilizador PostgreSQL  ", "postgres")
    pg_pass     = ask("Password PostgreSQL    ")
    redis_host  = ask("Host Redis             ", "localhost")
    redis_port  = ask("Porta Redis            ", "6379")
    api_port    = ask("Porta API ingestão     ", "8080")
    foundry_key = ask("Azure Foundry API Key  (Enter=manter) ")
    print()

    # ── 3. Copiar ficheiros ────────────────────────────────────
    print(f"[3/6] A copiar ficheiros para {INSTALL_DIR} ...")
    os.makedirs(INSTALL_DIR, exist_ok=True)
    os.makedirs(LOG_DIR,     exist_ok=True)

    src = get_source_path()
    shutil.copytree(src, INSTALL_DIR, dirs_exist_ok=True)
    print("       OK")

    # patch start_center.py (cópia instalada)
    sc = os.path.join(INSTALL_DIR, "start_center.py")
    if os.path.exists(sc):
        patch_file(sc, [
            ('POSTGRES_HOST = "localhost"',        f'POSTGRES_HOST = "{pg_host}"'),
            ('POSTGRES_PORT = "5432"',             f'POSTGRES_PORT = "{pg_port}"'),
            ('POSTGRES_DB = "jarvis"',             f'POSTGRES_DB = "{pg_db}"'),
            ('POSTGRES_USER = "postgres"',         f'POSTGRES_USER = "{pg_user}"'),
            ('POSTGRES_PASSWORD = "Vermelho555@"', f'POSTGRES_PASSWORD = "{pg_pass}"'),
            ('REDIS_HOST = "localhost"',           f'REDIS_HOST = "{redis_host}"'),
            ('REDIS_PORT = 6379',                  f'REDIS_PORT = {redis_port}'),
            ('API_PORT = 8080',                    f'API_PORT = {api_port}'),
        ])
        print("       start_center.py configurado")

    # patch foundry_client.py se chave fornecida
    if foundry_key:
        fc = os.path.join(INSTALL_DIR, "brain", "foundry_client.py")
        if os.path.exists(fc):
            with open(fc, encoding="utf-8") as f:
                content = f.read()
            content = re.sub(
                r'FOUNDRY_API_KEY\s*=\s*"[^"]*"',
                f'FOUNDRY_API_KEY     = "{foundry_key}"',
                content,
            )
            with open(fc, "w", encoding="utf-8") as f:
                f.write(content)
            print("       foundry_client.py configurado")

    # ── 4. Dependências Python ─────────────────────────────────
    print()
    print("[4/6] A instalar dependências Python...")
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "-q"])

    req = os.path.join(INSTALL_DIR, "requirements.txt")
    if os.path.exists(req):
        result = run([sys.executable, "-m", "pip", "install", "-r", req, "-q"])
    else:
        result = run([sys.executable, "-m", "pip", "install",
                      "fastapi", "uvicorn[standard]", "psycopg2-binary",
                      "redis", "requests", "anthropic", "-q"])

    if result.returncode != 0:
        print("[ERRO] Falha ao instalar dependências.")
        input("\nPressione Enter para sair...")
        sys.exit(1)
    print("       OK")

    # ── 5. Base de dados ───────────────────────────────────────
    print()
    print("[5/6] A inicializar base de dados...")
    env = os.environ.copy()
    env["PGPASSWORD"] = pg_pass

    psql = shutil.which("psql")
    if not psql:
        print("[AVISO] psql não encontrado — a saltar criação do schema.")
        print("         Crie a base de dados manualmente e execute:")
        print(f"         psql -U {pg_user} -d {pg_db} -f {INSTALL_DIR}\\storage\\schema_init.sql")
    else:
        # criar DB se não existir
        r = run([psql, "-h", pg_host, "-p", pg_port, "-U", pg_user, "-tc",
                 f"SELECT 1 FROM pg_database WHERE datname='{pg_db}'"],
                capture_output=True, text=True, env=env)
        if "1" not in r.stdout:
            print(f"       A criar base de dados '{pg_db}'...")
            run([psql, "-h", pg_host, "-p", pg_port, "-U", pg_user, "-c",
                 f'CREATE DATABASE "{pg_db}" ENCODING \'UTF8\';'],
                capture_output=True, env=env)

        schema = os.path.join(INSTALL_DIR, "storage", "schema_init.sql")
        if os.path.exists(schema):
            r = run([psql, "-h", pg_host, "-p", pg_port, "-U", pg_user,
                     "-d", pg_db, "-f", schema],
                    capture_output=True, env=env)
            if r.returncode == 0:
                print("       Schema aplicado.")
            else:
                print("[AVISO] Schema pode ter falhado. Verifique a ligação ao PostgreSQL.")

    # ── 6. Serviço Windows ─────────────────────────────────────
    print()
    print("[6/6] Instalar como serviço Windows?")
    svc_choice = input("       (s/n): ").strip().lower()

    if svc_choice == "s":
        svc_name = "JarvisCenter"
        nssm = shutil.which("nssm")

        if not nssm:
            print("       NSSM não encontrado. A tentar instalar via winget...")
            run(["winget", "install", "NSSM.NSSM", "-e", "--silent"],
                capture_output=True)
            # refrescar PATH
            r = run(["reg", "query",
                     r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                     "/v", "Path"],
                    capture_output=True, text=True)
            for line in r.stdout.splitlines():
                if "Path" in line:
                    sys_path = line.split("    ")[-1].strip()
                    os.environ["PATH"] = sys_path + ";" + os.environ.get("PATH", "")
                    break
            nssm = shutil.which("nssm")

        if not nssm:
            print("[AVISO] NSSM não disponível. Serviço não instalado.")
            print(f"         Inicie manualmente: cd {INSTALL_DIR} & python start_center.py")
        else:
            # remover serviço existente
            r = run(["sc", "query", svc_name], capture_output=True)
            if r.returncode == 0:
                run([nssm, "stop",   svc_name],         capture_output=True)
                run([nssm, "remove", svc_name, "confirm"], capture_output=True)

            py_exe = shutil.which("python") or sys.executable
            daemon = os.path.join(INSTALL_DIR, "start_center.py")

            run([nssm, "install", svc_name, py_exe, daemon])
            run([nssm, "set", svc_name, "AppDirectory",   INSTALL_DIR])
            run([nssm, "set", svc_name, "AppStdout",      os.path.join(LOG_DIR, "stdout.log")])
            run([nssm, "set", svc_name, "AppStderr",      os.path.join(LOG_DIR, "stderr.log")])
            run([nssm, "set", svc_name, "AppRotateFiles", "1"])
            run([nssm, "set", svc_name, "AppRotateBytes", "10485760"])
            run([nssm, "set", svc_name, "Start",          "SERVICE_AUTO_START"])
            run([nssm, "set", svc_name, "DisplayName",    "Jarvis Center"])
            run([nssm, "set", svc_name, "Description",
                 "Jarvis Center - observabilidade e analise AI"])

            r = run(["net", "start", svc_name], capture_output=True)
            if r.returncode == 0:
                print(f"       Serviço '{svc_name}' instalado e iniciado.")
            else:
                print(f"       Serviço instalado. Inicie com: net start {svc_name}")

    # ── Fim ────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  INSTALAÇÃO CONCLUÍDA")
    print("=" * 60)
    print()
    print(f"  Directório : {INSTALL_DIR}")
    print(f"  Logs       : {LOG_DIR}")
    print()
    print("  Iniciar manualmente:")
    print(f"    cd {INSTALL_DIR}")
    print("    python start_center.py")
    print()
    print(f"  API de ingestão : http://<host>:{api_port}/telemetry")
    print(f"  Documentação    : http://<host>:{api_port}/docs")
    print()
    input("Pressione Enter para sair...")


if __name__ == "__main__":
    main()
