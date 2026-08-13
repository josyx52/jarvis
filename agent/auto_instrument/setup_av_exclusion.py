#!/usr/bin/env python3
"""
setup_av_exclusion.py — Configura exclusões no Windows Defender para o Jarvis.

Fix Bug 4: CreateRemoteThread + LoadLibraryA é a assinatura clássica de injecção
de malware. O Windows Defender bloqueia silenciosamente sem certificado EV.

Adiciona exclusões para:
  - Directório do agente Jarvis (paths e ficheiros)
  - JarvisHook_x64.dll e JarvisHook_x86.dll
  - Processo do agente (agent_daemon.exe)

Requer: execução como Administrador
Uso:    python setup_av_exclusion.py
        python setup_av_exclusion.py --remove   (remove exclusões)
        python setup_av_exclusion.py --status   (mostra exclusões actuais)
"""

import argparse
import os
import subprocess
import sys


def _is_admin() -> bool:
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _run_ps(cmd: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["powershell", "-NonInteractive", "-NoProfile", "-Command", cmd],
        capture_output=True, text=True
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _agent_root() -> str:
    """Raíz do directório do agente."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def get_exclusion_paths() -> list[str]:
    root = _agent_root()
    return [
        root,
        os.path.join(root, "auto_instrument", "code_modules", "JarvisHook_x64.dll"),
        os.path.join(root, "auto_instrument", "code_modules", "JarvisHook_x86.dll"),
        os.path.join(root, "auto_instrument", "code_modules", "JarvisHook.dll"),
    ]


def get_exclusion_processes() -> list[str]:
    root = _agent_root()
    return [
        os.path.join(root, "agent_daemon.exe"),
        os.path.join(root, "agent_daemon", "agent_daemon.exe"),
        "agent_daemon.exe",
    ]


def cmd_add():
    """Adiciona exclusões ao Windows Defender."""
    if not _is_admin():
        print("[ERROR] Este script requer privilégios de Administrador.")
        print("  Executar: python setup_av_exclusion.py (como Administrador)")
        sys.exit(1)

    # Verificar se Windows Defender está activo
    code, out, _ = _run_ps("Get-MpComputerStatus | Select-Object -ExpandProperty AntivirusEnabled")
    if code != 0 or out.strip().lower() != "true":
        print("[INFO] Windows Defender não está activo — exclusões não necessárias")
        return

    print("[AV] Adicionando exclusões ao Windows Defender...\n")

    # Exclusões de paths
    paths = get_exclusion_paths()
    for path in paths:
        ps = f'Add-MpPreference -ExclusionPath "{path}" -ErrorAction SilentlyContinue'
        code, _, err = _run_ps(ps)
        status = "OK" if code == 0 else f"FALHA ({err[:60]})"
        print(f"  Path: {os.path.basename(path):<40} {status}")

    # Exclusões de processos
    processes = get_exclusion_processes()
    for proc in processes:
        if os.path.isfile(proc):
            ps = f'Add-MpPreference -ExclusionProcess "{proc}" -ErrorAction SilentlyContinue'
            code, _, err = _run_ps(ps)
            status = "OK" if code == 0 else f"FALHA ({err[:60]})"
            print(f"  Proc: {os.path.basename(proc):<40} {status}")

    # Exclusão de extensão para as DLLs de injecção
    # (Não adicionamos .dll globalmente — muito permissivo)
    print("\n[AV] Exclusões configuradas.")
    print("     Se o Defender continuar a bloquear, verificar com --status")


def cmd_remove():
    """Remove exclusões do Windows Defender."""
    if not _is_admin():
        print("[ERROR] Requer Administrador.")
        sys.exit(1)

    print("[AV] Removendo exclusões do Windows Defender...\n")

    for path in get_exclusion_paths():
        ps = f'Remove-MpPreference -ExclusionPath "{path}" -ErrorAction SilentlyContinue'
        code, _, _ = _run_ps(ps)
        print(f"  Removido: {os.path.basename(path)}")

    for proc in get_exclusion_processes():
        if os.path.isfile(proc):
            ps = f'Remove-MpPreference -ExclusionProcess "{proc}" -ErrorAction SilentlyContinue'
            _run_ps(ps)
            print(f"  Removido proc: {os.path.basename(proc)}")

    print("\n[AV] Exclusões removidas.")


def cmd_status():
    """Mostra exclusões actuais relevantes para o Jarvis."""
    print("[AV] Exclusões actuais no Windows Defender:\n")

    code, out, _ = _run_ps(
        "(Get-MpPreference).ExclusionPath | Where-Object { $_ -like '*jarvis*' -or $_ -like '*JarvisHook*' }"
    )
    if out:
        print("  Paths:")
        for line in out.splitlines():
            print(f"    {line}")
    else:
        print("  Paths: nenhuma exclusão Jarvis encontrada")

    code, out, _ = _run_ps(
        "(Get-MpPreference).ExclusionProcess | Where-Object { $_ -like '*jarvis*' -or $_ -like '*agent*' }"
    )
    if out:
        print("  Processos:")
        for line in out.splitlines():
            print(f"    {line}")
    else:
        print("  Processos: nenhuma exclusão Jarvis encontrada")

    # Verificar se as DLLs existem
    print("\n[AV] Estado das DLLs de injecção:")
    root = _agent_root()
    for dll in ["JarvisHook_x64.dll", "JarvisHook_x86.dll"]:
        path = os.path.join(root, "auto_instrument", "code_modules", dll)
        exists = os.path.isfile(path)
        size   = f"{os.path.getsize(path) // 1024} KB" if exists else "—"
        print(f"  {dll:<28} {'✓ existe' if exists else '✗ não compilada':<16} {size}")

    # Verificar se Defender está activo
    code, out, _ = _run_ps("Get-MpComputerStatus | Select-Object AntivirusEnabled,RealTimeProtectionEnabled | ConvertTo-Json")
    if code == 0 and out:
        import json
        try:
            info = json.loads(out)
            av  = info.get("AntivirusEnabled", "?")
            rtp = info.get("RealTimeProtectionEnabled", "?")
            print(f"\n  Defender activo: {av}  |  Real-time protection: {rtp}")
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Configura exclusões Windows Defender para Jarvis DLL injection"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--remove", action="store_true", help="Remove exclusões")
    group.add_argument("--status", action="store_true", help="Mostra estado actual")
    args = parser.parse_args()

    if args.remove:
        cmd_remove()
    elif args.status:
        cmd_status()
    else:
        cmd_add()


if __name__ == "__main__":
    main()
