#!/usr/bin/env python3
"""
setup_modules.py — Download OTel artefactos para Jarvis auto-instrumentation.

Baixa:
  - opentelemetry-javaagent.jar  (OTel Java auto-instrumentation, ~18 MB)
  - otel-dotnet-auto/            (OTel .NET auto-instrumentation profiler, ~30 MB)

Uso:
  python setup_modules.py
  python setup_modules.py --java-only
  python setup_modules.py --dotnet-only
"""

import argparse
import os
import subprocess
import sys
import urllib.request
import zipfile

_CODE_MODULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "code_modules")

# Python OTel packages bundled into code_modules/lib/ so sitecustomize.py works
# in any target venv without requiring the user to pip install anything.
_PYTHON_OTEL_PACKAGES = [
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-instrumentation-flask",
    "opentelemetry-instrumentation-django",
    "opentelemetry-instrumentation-requests",
    "opentelemetry-instrumentation-sqlalchemy",
    "opentelemetry-instrumentation-psycopg2",
    "opentelemetry-instrumentation-urllib3",
]

_PYTHON_LIB_DEST   = os.path.join(_CODE_MODULES, "lib")
_PYTHON_LIB_MARKER = os.path.join(_PYTHON_LIB_DEST, "opentelemetry", "sdk", "__init__.py")

# OTel Java agent — https://github.com/open-telemetry/opentelemetry-java-instrumentation
_JAVA_VERSION  = "2.12.0"
_JAVA_URL      = (
    f"https://github.com/open-telemetry/opentelemetry-java-instrumentation"
    f"/releases/download/v{_JAVA_VERSION}/opentelemetry-javaagent.jar"
)
_JAVA_DEST     = os.path.join(_CODE_MODULES, "opentelemetry-javaagent.jar")

# OTel .NET auto-instrumentation — https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation
_DOTNET_VERSION = "1.9.0"
_DOTNET_URL     = (
    f"https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation"
    f"/releases/download/v{_DOTNET_VERSION}/opentelemetry-dotnet-instrumentation-windows.zip"
)
_DOTNET_DEST    = os.path.join(_CODE_MODULES, "otel-dotnet-auto")


def _progress(label):
    def _hook(count, block, total):
        if total > 0:
            pct  = min(int(count * block * 100 / total), 100)
            done = pct // 5
            bar  = "█" * done + "░" * (20 - done)
            print(f"\r  [{bar}] {pct:3d}%  {label}", end="", flush=True)
    return _hook


def _center_url() -> str:
    """Lê o URL do center do config do agente."""
    try:
        import json
        for path in [
            os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"),
                         "JarvisAgent", "jarvis_config.json"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "jarvis_config.json"),
        ]:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                url = cfg.get("server", {}).get("center_url", "")
                if url:
                    return url.rstrip("/")
    except Exception:
        pass
    return os.environ.get("JARVIS_CENTER_URL", "").rstrip("/")


def _try_center_download(artifact_name: str, dest: str) -> bool:
    """
    Tenta descarregar um artefacto do repositório interno do center.
    Retorna True se bem-sucedido.
    """
    base = _center_url()
    if not base:
        return False
    url = f"{base}/artifacts/{artifact_name}"
    tmp = dest + ".tmp"
    try:
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, dest)
        size_mb = os.path.getsize(dest) / (1024 * 1024)
        print(f"  [CENTER] {artifact_name} ({size_mb:.1f} MB) — OK")
        return True
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        print(f"  [CENTER] {artifact_name} indisponível no center ({e}) — a tentar URL oficial...")
        return False


def _download(url: str, dest: str, label: str, artifact_name: str = ""):
    """
    Descarrega um artefacto.
    Tenta o repositório interno do center primeiro; fallback para o URL oficial.
    """
    print(f"\n[DOWNLOAD] {label}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # 1. Tentar center interno
    if artifact_name and _try_center_download(artifact_name, dest):
        return

    # 2. Fallback: URL oficial (terceiro)
    tmp = dest + ".tmp"
    try:
        urllib.request.urlretrieve(url, tmp, reporthook=_progress(label))
        print()
        os.replace(tmp, dest)
        size_mb = os.path.getsize(dest) / (1024 * 1024)
        print(f"  OK — {size_mb:.1f} MB → {dest}")
    except Exception as e:
        print(f"\n  FALHA: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def setup_python_otel(force: bool = False):
    if not force and os.path.isfile(_PYTHON_LIB_MARKER):
        size_mb = sum(
            os.path.getsize(os.path.join(d, f))
            for d, _, files in os.walk(_PYTHON_LIB_DEST)
            for f in files
        ) / (1024 * 1024)
        print(f"[PYTHON] OTel packages já instalados ({size_mb:.1f} MB em {_PYTHON_LIB_DEST}) — a saltar")
        return

    print(f"\n[PYTHON] A instalar OTel packages em {_PYTHON_LIB_DEST} ...")
    os.makedirs(_PYTHON_LIB_DEST, exist_ok=True)

    # Usar o Python que está a correr este script para garantir compatibilidade
    python_exe = sys.executable
    result = subprocess.run(
        [
            python_exe, "-m", "pip", "install",
            "--target", _PYTHON_LIB_DEST,
            "--quiet",
            "--disable-pip-version-check",
            "--no-warn-script-location",
        ] + _PYTHON_OTEL_PACKAGES,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        size_mb = sum(
            os.path.getsize(os.path.join(d, f))
            for d, _, files in os.walk(_PYTHON_LIB_DEST)
            for f in files
        ) / (1024 * 1024)
        print(f"  OK — {size_mb:.1f} MB instalados em {_PYTHON_LIB_DEST}")
    else:
        err = (result.stderr or result.stdout or "")[:600]
        print(f"  FALHA: {err}")
        raise RuntimeError("pip install OTel packages falhou")


def setup_java():
    if os.path.isfile(_JAVA_DEST):
        size_mb = os.path.getsize(_JAVA_DEST) / (1024 * 1024)
        print(f"[JAVA] opentelemetry-javaagent.jar já existe ({size_mb:.1f} MB) — a saltar")
        return
    _download(_JAVA_URL, _JAVA_DEST, f"OTel Java agent v{_JAVA_VERSION}",
              artifact_name="opentelemetry-javaagent.jar")


def setup_dotnet():
    native = os.path.join(_DOTNET_DEST, "win-x64", "OpenTelemetry.AutoInstrumentation.Native.dll")
    if os.path.isfile(native):
        print(f"[.NET] OTel .NET profiler já existe — a saltar")
        return

    zip_path = os.path.join(_CODE_MODULES, "_otel-dotnet.zip")
    _download(_DOTNET_URL, zip_path, f"OTel .NET auto-instrumentation v{_DOTNET_VERSION}",
              artifact_name="otel-dotnet-windows.zip")

    print(f"[.NET] Extraindo para {_DOTNET_DEST} ...")
    os.makedirs(_DOTNET_DEST, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(_DOTNET_DEST)
    os.remove(zip_path)
    print(f"[.NET] OK")


_WINDIVERT_DIR = os.path.join(
    os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "JarvisAgent"
)


def setup_windivert():
    """
    Descarrega WinDivert.dll + WinDivert64.sys necessários para o PacketCapture.
    Coloca em C:\\ProgramData\\JarvisAgent\\ (não polui o directório do Python).
    """
    import pathlib
    agent_dir = pathlib.Path(_WINDIVERT_DIR)
    agent_dir.mkdir(parents=True, exist_ok=True)
    dll  = agent_dir / "WinDivert.dll"
    sys_ = agent_dir / "WinDivert64.sys"

    if dll.is_file() and sys_.is_file():
        print(f"[WINDIVERT] WinDivert.dll + WinDivert64.sys já existem — a saltar")
        return

    print(f"\n[WINDIVERT] A configurar WinDivert para PacketCapture ...")

    # 1. Tentar obter directamente do center (já extraídos pelo prefetch_artifacts.py)
    got_dll = got_sys = False
    base = _center_url()
    if base:
        for fname, target, flag in [
            ("WinDivert.dll",    dll,  "got_dll"),
            ("WinDivert64.sys",  sys_, "got_sys"),
        ]:
            tmp = str(target) + ".tmp"
            try:
                urllib.request.urlretrieve(f"{base}/artifacts/{fname}", tmp)
                os.replace(tmp, str(target))
                print(f"  [CENTER] {fname} OK")
                if flag == "got_dll": got_dll = True
                else:                got_sys = True
            except Exception:
                if os.path.exists(tmp): os.remove(tmp)

    if got_dll and got_sys:
        print(f"[WINDIVERT] Instalado em {agent_dir}")
        return

    # 2. Fallback: baixar o ZIP oficial e extrair
    _WINDIVERT_VER = "2.2.2"
    _WINDIVERT_URL = (
        f"https://github.com/basil00/WinDivert/releases/download"
        f"/v{_WINDIVERT_VER}/WinDivert-{_WINDIVERT_VER}-A.zip"
    )
    zip_path = os.path.join(_CODE_MODULES, "_windivert.zip")
    try:
        _download(_WINDIVERT_URL, zip_path, f"WinDivert {_WINDIVERT_VER}",
                  artifact_name=f"WinDivert-{_WINDIVERT_VER}-A.zip")
        import shutil
        targets = {"WinDivert.dll", "WinDivert64.sys", "WinDivert32.sys"}
        with zipfile.ZipFile(zip_path, "r") as z:
            for entry in z.infolist():
                fname = pathlib.Path(entry.filename).name
                if fname in targets:
                    dest = agent_dir / fname
                    with z.open(entry) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    print(f"  Extraído: {fname}")
        os.remove(zip_path)
        print(f"[WINDIVERT] Instalado em {agent_dir}")
    except Exception as e:
        print(f"[WINDIVERT] FALHA: {e}")


def setup_pip_deps():
    """
    Instala pywintrace e pydivert para activar ETW e PacketCapture WinDivert.
    Tenta wheels locais do center primeiro (offline-friendly).
    """
    import pathlib, shutil
    deps_to_install = []

    for pkg, import_name in [("pywintrace", "etw"), ("pydivert", "pydivert")]:
        try:
            __import__(import_name)
            print(f"[DEPS] {pkg} já instalado — OK")
        except ImportError:
            deps_to_install.append(pkg)

    if not deps_to_install:
        return

    print(f"\n[DEPS] A instalar: {deps_to_install}")

    # 1. Tentar wheels locais do center (via prefetch_artifacts.py)
    wheels_dir = None
    base = _center_url()
    if base:
        # Os wheels estão em artifacts/python_wheels/ no center — não temos acesso
        # directo à pasta do center daqui. Mas o agente pode ter baixado antes.
        candidate = pathlib.Path(_CODE_MODULES) / "python_wheels"
        if candidate.is_dir() and any(candidate.glob("*.whl")):
            wheels_dir = str(candidate)

    for pkg in deps_to_install:
        installed = False

        # 2. Tentar instalar do directório de wheels local
        if wheels_dir:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install",
                 "--find-links", wheels_dir, "--no-index",
                 "--quiet", pkg],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"  [WHEELS] {pkg} instalado OK")
                installed = True

        # 3. Fallback: pip install normal (requer internet)
        if not installed:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "--disable-pip-version-check", pkg],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"  [PIP] {pkg} instalado OK")
            else:
                print(f"  [PIP] {pkg} FALHOU: {result.stderr[:200]}")


def main():
    parser = argparse.ArgumentParser(description="Download OTel artefactos para Jarvis")
    parser.add_argument("--java-only",   action="store_true")
    parser.add_argument("--dotnet-only", action="store_true")
    parser.add_argument("--python-only", action="store_true")
    parser.add_argument("--force",       action="store_true", help="Reinstalar mesmo se já existir")
    args = parser.parse_args()

    os.makedirs(_CODE_MODULES, exist_ok=True)
    print(f"[SETUP] code_modules: {_CODE_MODULES}")

    if args.python_only:
        setup_python_otel(force=args.force)
    elif args.dotnet_only:
        setup_dotnet()
    elif args.java_only:
        setup_java()
    else:
        setup_python_otel(force=args.force)
        setup_java()
        setup_dotnet()
        setup_windivert()
        setup_pip_deps()

    print("\n[SETUP] Concluído.")
    print("\nPara binários nativos (nginx, postgres, etc.):")
    print("  cd native_hook")
    print("  vcpkg install detours:x64-windows")
    print("  python build.py")


if __name__ == "__main__":
    main()
