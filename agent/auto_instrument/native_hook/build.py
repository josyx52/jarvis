#!/usr/bin/env python3
"""
build.py — Compila JarvisHook_x64.dll e JarvisHook_x86.dll

Pré-requisitos:
  1. Visual Studio 2019/2022 com C++ workload
  2. vcpkg com VCPKG_ROOT definido (ou em C:\\vcpkg)
  3. vcpkg install detours:x64-windows detours:x86-windows

Uso:
  python build.py               # compila x64 + x86
  python build.py --x64-only
  python build.py --x86-only
  python build.py --vcpkg C:\\vcpkg
"""

import argparse
import os
import shutil
import subprocess
import sys


def find_vcpkg(hint=None):
    candidates = []
    if hint:        candidates.append(hint)
    env = os.environ.get("VCPKG_ROOT")
    if env:         candidates.append(env)
    candidates += [r"C:\vcpkg", r"C:\tools\vcpkg", r"D:\vcpkg"]
    for p in candidates:
        tc = os.path.join(p, "scripts", "buildsystems", "vcpkg.cmake")
        if os.path.isfile(tc):
            return p
    return None


def find_cmake():
    cmake = shutil.which("cmake")
    if cmake:
        return cmake
    vs_paths = [
        r"C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
        r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
        r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
    ]
    for p in vs_paths:
        if os.path.isfile(p):
            return p
    return None


def ensure_detours(vcpkg_root, triplet):
    check = os.path.join(vcpkg_root, "installed", triplet, "include", "detours.h")
    if os.path.isfile(check):
        return
    print(f"\n[VCPKG] Instalando detours:{triplet} ...")
    vcpkg_exe = os.path.join(vcpkg_root, "vcpkg.exe")
    subprocess.run([vcpkg_exe, "install", f"detours:{triplet}"], check=True)


def build_arch(cmake, vcpkg_root, here, arch):
    """Compila para uma arquitectura (x64 ou x86)."""
    triplet   = f"{arch}-windows"
    platform  = "x64" if arch == "x64" else "Win32"
    build_dir = os.path.join(here, f"build_{arch}")
    toolchain = os.path.join(vcpkg_root, "scripts", "buildsystems", "vcpkg.cmake")

    ensure_detours(vcpkg_root, triplet)

    print(f"\n[BUILD] Configurando CMake para {arch} ...")
    subprocess.run([
        cmake,
        "-S", here,
        "-B", build_dir,
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
        f"-DVCPKG_TARGET_TRIPLET={triplet}",
        "-DCMAKE_BUILD_TYPE=Release",
        "-A", platform,
    ], check=True)

    print(f"[BUILD] Compilando JarvisHook_{arch}.dll ...")
    subprocess.run([
        cmake, "--build", build_dir,
        "--config", "Release",
        "--parallel",
    ], check=True)

    dest = os.path.join(here, "..", "code_modules", f"JarvisHook_{arch}.dll")
    if os.path.isfile(dest):
        size_kb = os.path.getsize(dest) // 1024
        print(f"[BUILD] OK — JarvisHook_{arch}.dll ({size_kb} KB)")
        return True
    else:
        print(f"[BUILD] FALHA: JarvisHook_{arch}.dll não gerada")
        return False


def main():
    parser = argparse.ArgumentParser(description="Compila JarvisHook DLLs")
    parser.add_argument("--vcpkg",    help="Caminho para o directório raíz do vcpkg")
    parser.add_argument("--x64-only", action="store_true")
    parser.add_argument("--x86-only", action="store_true")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))

    cmake = find_cmake()
    if not cmake:
        print("[ERROR] cmake não encontrado.")
        print("  Instalar Visual Studio com C++ workload ou cmake.org")
        sys.exit(1)

    vcpkg_root = find_vcpkg(args.vcpkg)
    if not vcpkg_root:
        print("[ERROR] vcpkg não encontrado.")
        print("  git clone https://github.com/microsoft/vcpkg C:\\vcpkg")
        print("  C:\\vcpkg\\bootstrap-vcpkg.bat")
        print("  Definir VCPKG_ROOT=C:\\vcpkg")
        sys.exit(1)

    print(f"[BUILD] vcpkg : {vcpkg_root}")
    print(f"[BUILD] cmake : {cmake}")
    os.makedirs(os.path.join(here, "..", "code_modules"), exist_ok=True)

    archs = []
    if args.x64_only:
        archs = ["x64"]
    elif args.x86_only:
        archs = ["x86"]
    else:
        archs = ["x64", "x86"]

    results = {}
    for arch in archs:
        try:
            results[arch] = build_arch(cmake, vcpkg_root, here, arch)
        except subprocess.CalledProcessError:
            results[arch] = False

    print("\n── Resultado ──────────────────────────────────")
    for arch, ok in results.items():
        status = "OK" if ok else "FALHA"
        print(f"  JarvisHook_{arch}.dll : {status}")

    if not all(results.values()):
        sys.exit(1)

    print("\nAs DLLs serão injectadas automaticamente:")
    print("  JarvisHook_x64.dll → processos 64-bit (nginx, postgres, redis, ...)")
    print("  JarvisHook_x86.dll → processos 32-bit (apps legacy, IIS 32-bit)")
    print("\nCobertura de APIs:")
    print("  Tier A — Winsock TCP : connect, send, recv, WSASend, WSARecv")
    print("  Tier B — Winsock UDP : WSASendTo, WSARecvFrom")
    print("  Tier C — WinHTTP     : .NET HttpClient, PowerShell, apps modernas")
    print("  Tier D — WinInet     : apps Win32 legacy, IE-based")


if __name__ == "__main__":
    main()
