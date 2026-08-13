"""
Verifica a cada 15 dias se há uma nova versão do osquery no GitHub.
Se houver, descarrega o MSI para installers/ e actualiza JARVIS_OSQUERY_PATH no .env.
"""

import os
import re
import time
import threading
import urllib.request
import json

# Intervalo de verificação: 15 dias em segundos
_CHECK_INTERVAL = 15 * 24 * 3600

_GITHUB_RELEASES_URL = "https://api.github.com/repos/osquery/osquery/releases/latest"

_BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INSTALLERS_DIR = os.path.join(_BASE_DIR, "installers")
_ENV_PATH      = os.path.join(_BASE_DIR, ".env")

_shutdown = False


def _log(msg: str):
    print(f"[OSQUERY-UPDATER] {msg}", flush=True)


def _get_latest_release() -> tuple[str, str]:
    """Devolve (versao, url_msi) ou lança excepção."""
    req = urllib.request.Request(
        _GITHUB_RELEASES_URL,
        headers={"User-Agent": "JarvisCenter/1.0", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    version = data["tag_name"].lstrip("v")

    msi_asset = next(
        (a for a in data["assets"]
         if a["name"].endswith(".msi") and "debug" not in a["name"].lower()),
        None,
    )
    if not msi_asset:
        raise RuntimeError("Nenhum MSI encontrado na release")

    return version, msi_asset["browser_download_url"]


def _current_version() -> str:
    """Extrai a versão do caminho actual em JARVIS_OSQUERY_PATH."""
    path = os.getenv("JARVIS_OSQUERY_PATH", "")
    m = re.search(r"osquery-([\d.]+)\.msi", path, re.IGNORECASE)
    return m.group(1) if m else ""


def _download_msi(url: str, dest: str):
    _log(f"A descarregar {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "JarvisCenter/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    size_mb = os.path.getsize(dest) / (1024 * 1024)
    _log(f"Download concluido: {size_mb:.1f} MB → {dest}")


def _update_env(new_path: str):
    """Substitui JARVIS_OSQUERY_PATH no .env e actualiza a variável de ambiente do processo."""
    if not os.path.exists(_ENV_PATH):
        return

    with open(_ENV_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    new_content = re.sub(
        r"^JARVIS_OSQUERY_PATH=.*$",
        f"JARVIS_OSQUERY_PATH={new_path}",
        content,
        flags=re.MULTILINE,
    )

    if new_content == content:
        # linha ainda não existe — adicionar
        new_content = content.rstrip() + f"\nJARVIS_OSQUERY_PATH={new_path}\n"

    with open(_ENV_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)

    os.environ["JARVIS_OSQUERY_PATH"] = new_path
    _log(f".env actualizado: JARVIS_OSQUERY_PATH={new_path}")


def _check_and_update():
    """Verifica GitHub e actualiza se houver versão mais recente."""
    try:
        latest_ver, msi_url = _get_latest_release()
        current_ver = _current_version()

        _log(f"Versao actual: {current_ver or '(desconhecida)'}  |  Ultima: {latest_ver}")

        if current_ver == latest_ver:
            _log("Sem actualizacao necessaria.")
            return

        dest = os.path.join(_INSTALLERS_DIR, f"osquery-{latest_ver}.msi")

        if os.path.exists(dest):
            _log(f"MSI {latest_ver} ja existe em disco — a actualizar apenas o .env.")
        else:
            _download_msi(msi_url, dest)

        _update_env(dest)
        _log(f"osquery actualizado para {latest_ver}.")

    except Exception as exc:
        _log(f"Erro ao verificar actualizacao: {exc}")


def _worker():
    # Primeira verificação com 60 s de atraso para o centro terminar o arranque
    time.sleep(60)
    _check_and_update()

    while not _shutdown:
        time.sleep(_CHECK_INTERVAL)
        if not _shutdown:
            _check_and_update()


def start(daemon: bool = True) -> threading.Thread:
    t = threading.Thread(target=_worker, name="osquery-updater", daemon=daemon)
    t.start()
    _log(f"Agendado — verifica actualizacoes a cada 15 dias.")
    return t


def stop():
    global _shutdown
    _shutdown = True
