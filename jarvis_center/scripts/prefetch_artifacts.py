#!/usr/bin/env python3
"""
prefetch_artifacts.py — Pré-carrega todos os binários no repositório interno do Jarvis.

Corre no servidor center UMA VEZ (ou periodicamente para actualizar versões).
Os agentes descarregam daqui em vez de aceder directamente a sites de terceiros
(GitHub, PyPI, etc.) — funciona em redes isoladas e é mais rápido em deployments
com muitos agentes.

Artefactos descarregados:
  opentelemetry-javaagent.jar        — OTel Java auto-instrumentation (~18 MB)
  otel-dotnet-windows.zip            — OTel .NET profiler (~30 MB)
  WinDivert.dll / WinDivert64.sys    — Captura de pacotes ao nível NDIS (~0.5 MB)
  pywintrace wheel                   — ETW consumer Python
  pydivert wheel                     — Python wrapper para WinDivert

Uso:
  python prefetch_artifacts.py
  python prefetch_artifacts.py --artifacts-dir /data/jarvis/artifacts
  python prefetch_artifacts.py --skip-pip      (não baixar wheels Python)
  python prefetch_artifacts.py --check         (apenas verifica o que falta)
"""

import argparse
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile


# ── Versões ──────────────────────────────────────────────────────────────────

_JAVA_VERSION   = "2.12.0"
_DOTNET_VERSION = "1.9.0"
_WINDIVERT_VER  = "2.2.2"

# ── URLs oficiais ─────────────────────────────────────────────────────────────

ARTIFACTS = [
    {
        "name":        "opentelemetry-javaagent.jar",
        "url":         (
            f"https://github.com/open-telemetry/opentelemetry-java-instrumentation"
            f"/releases/download/v{_JAVA_VERSION}/opentelemetry-javaagent.jar"
        ),
        "description": f"OTel Java auto-instrumentation agent v{_JAVA_VERSION}",
        "required":    True,
    },
    {
        "name":        "otel-dotnet-windows.zip",
        "url":         (
            f"https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation"
            f"/releases/download/v{_DOTNET_VERSION}"
            f"/opentelemetry-dotnet-instrumentation-windows.zip"
        ),
        "description": f"OTel .NET auto-instrumentation profiler v{_DOTNET_VERSION}",
        "required":    True,
    },
    {
        "name":        f"WinDivert-{_WINDIVERT_VER}-A.zip",
        "url":         (
            f"https://github.com/basil00/WinDivert/releases/download"
            f"/v{_WINDIVERT_VER}/WinDivert-{_WINDIVERT_VER}-A.zip"
        ),
        "description": f"WinDivert {_WINDIVERT_VER} — packet capture (NDIS level)",
        "required":    True,
        "post":        "extract_windivert",
    },
]

# Wheels Python — descarregados via pip download para funcionar offline nos agentes
PIP_PACKAGES = [
    "pywintrace",
    "pydivert",
]


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _progress(label):
    def _hook(count, block, total):
        if total > 0:
            pct  = min(int(count * block * 100 / total), 100)
            done = pct // 5
            bar  = "█" * done + "░" * (20 - done)
            print(f"\r  [{bar}] {pct:3d}%  {label}", end="", flush=True)
    return _hook


def _download(url: str, dest: pathlib.Path, label: str) -> bool:
    print(f"\n[DOWNLOAD] {label}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        urllib.request.urlretrieve(url, str(tmp), reporthook=_progress(label))
        print()
        tmp.rename(dest)
        size_mb = dest.stat().st_size / (1024 * 1024)
        sha     = _sha256(dest)
        print(f"  OK — {size_mb:.1f} MB  sha256={sha[:16]}...")
        return True
    except Exception as e:
        print(f"\n  FALHA: {e}")
        if tmp.exists():
            tmp.unlink()
        return False


def _extract_windivert(zip_path: pathlib.Path, artifacts_dir: pathlib.Path):
    """Extrai WinDivert.dll e WinDivert64.sys do zip para artifacts/."""
    print(f"  [WINDIVERT] A extrair DLL + driver de {zip_path.name} ...")
    targets = {"WinDivert.dll", "WinDivert64.sys", "WinDivert32.sys"}
    with zipfile.ZipFile(zip_path, "r") as z:
        for entry in z.infolist():
            fname = pathlib.Path(entry.filename).name
            if fname in targets:
                dest = artifacts_dir / fname
                with z.open(entry) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                print(f"  [WINDIVERT] Extraído: {fname} ({dest.stat().st_size // 1024} KB)")


def _pip_download(packages: list[str], dest_dir: pathlib.Path) -> bool:
    """Usa pip download para guardar wheels offline."""
    wheels_dir = dest_dir / "python_wheels"
    wheels_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[PIP DOWNLOAD] A guardar wheels em {wheels_dir} ...")
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "download",
            "--dest", str(wheels_dir),
            "--platform", "win_amd64",
            "--only-binary=:all:",
            "--quiet",
            "--disable-pip-version-check",
        ] + packages,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        wheels = list(wheels_dir.glob("*.whl"))
        print(f"  OK — {len(wheels)} wheel(s) guardados:")
        for w in wheels:
            print(f"    {w.name}  ({w.stat().st_size // 1024} KB)")
        return True
    else:
        # pip download com --platform pode falhar — tentar sem filtro de plataforma
        result2 = subprocess.run(
            [
                sys.executable, "-m", "pip", "download",
                "--dest", str(wheels_dir),
                "--quiet", "--disable-pip-version-check",
            ] + packages,
            capture_output=True,
            text=True,
        )
        if result2.returncode == 0:
            wheels = list(wheels_dir.glob("*.whl")) + list(wheels_dir.glob("*.tar.gz"))
            print(f"  OK — {len(wheels)} ficheiro(s) guardados")
            return True
        print(f"  AVISO pip download: {result2.stderr[:300]}")
        return False


def cmd_fetch(artifacts_dir: pathlib.Path, skip_pip: bool = False) -> dict:
    """Descarrega todos os artefactos. Retorna {name: ok/skip/fail}."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[PREFETCH] artifacts_dir: {artifacts_dir}\n")

    results = {}

    for art in ARTIFACTS:
        name = art["name"]
        dest = artifacts_dir / name

        if dest.exists():
            size_mb = dest.stat().st_size / (1024 * 1024)
            print(f"[SKIP] {name} já existe ({size_mb:.1f} MB)")
            results[name] = "skip"
            # Mesmo que o zip já exista, garantir que DLLs estão extraídas
            if art.get("post") == "extract_windivert":
                _extract_windivert(dest, artifacts_dir)
            continue

        ok = _download(art["url"], dest, art["description"])
        results[name] = "ok" if ok else "fail"

        if ok and art.get("post") == "extract_windivert":
            _extract_windivert(dest, artifacts_dir)

    # Wheels Python
    if not skip_pip:
        pip_ok = _pip_download(PIP_PACKAGES, artifacts_dir)
        results["python_wheels"] = "ok" if pip_ok else "fail"
    else:
        results["python_wheels"] = "skip"

    return results


def cmd_check(artifacts_dir: pathlib.Path):
    """Verifica o que está presente e o que falta."""
    print(f"\n[CHECK] artifacts_dir: {artifacts_dir}\n")

    critical_files = [
        "opentelemetry-javaagent.jar",
        "otel-dotnet-windows.zip",
        "WinDivert.dll",
        "WinDivert64.sys",
    ]

    ok_count  = 0
    miss_count = 0

    for fname in critical_files:
        path = artifacts_dir / fname
        if path.exists():
            size_mb = path.stat().st_size / (1024 * 1024)
            print(f"  OK {fname:<45} {size_mb:.1f} MB")
            ok_count += 1
        else:
            print(f"  MISS {fname:<45} MISSING")
            miss_count += 1

    wheels_dir = artifacts_dir / "python_wheels"
    if wheels_dir.exists():
        wheels = list(wheels_dir.glob("*.whl")) + list(wheels_dir.glob("*.tar.gz"))
        print(f"  OK python_wheels/                               {len(wheels)} ficheiro(s)")
        ok_count += 1
    else:
        print(f"  MISS python_wheels/                               MISSING")
        miss_count += 1

    print(f"\n  Total: {ok_count} presentes, {miss_count} em falta")
    if miss_count:
        print("  Executar: python prefetch_artifacts.py")


def main():
    parser = argparse.ArgumentParser(
        description="Pré-carrega artefactos OTel + WinDivert no repositório interno Jarvis"
    )
    parser.add_argument(
        "--artifacts-dir",
        default=str(
            pathlib.Path(__file__).parent.parent.parent / "artifacts"
        ),
        help="Directório de artefactos (default: ../../artifacts)",
    )
    parser.add_argument("--skip-pip",  action="store_true", help="Não descarregar wheels Python")
    parser.add_argument("--check",     action="store_true", help="Apenas verificar o que existe")
    args = parser.parse_args()

    dest = pathlib.Path(args.artifacts_dir).resolve()

    if args.check:
        cmd_check(dest)
        return

    results = cmd_fetch(dest, skip_pip=args.skip_pip)

    print("\n── Resultado ──────────────────────────────────────────────────")
    for name, status in results.items():
        icon = "OK" if status in ("ok", "skip") else "MISS"
        print(f"  {icon} {name:<45} {status}")

    failures = [n for n, s in results.items() if s == "fail"]
    if failures:
        print(f"\nFALHAS: {', '.join(failures)}")
        print("Verificar conectividade com GitHub. Tentar novamente mais tarde.")
        sys.exit(1)
    else:
        print(f"\nRepositório pronto em: {dest}")
        print("Os agentes descarregarão daqui via /artifacts/<nome>")


if __name__ == "__main__":
    main()
