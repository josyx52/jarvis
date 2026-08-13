"""
Build jarvis-ctl.exe com PyInstaller.

Uso:
    python build_ctl.py

Output: dist\jarvis-ctl.exe  (executavel unico, sem instalacao de Python)

Requisito: pip install pyinstaller
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--name", "jarvis-ctl",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build"),
        "--specpath", ROOT,
        "--add-data", f"{ROOT}\\ui;ui",
        "--hidden-import", "anthropic",
        "--hidden-import", "psycopg2",
        "--hidden-import", "rich",
        "--hidden-import", "typer",
        "--hidden-import", "prompt_toolkit",
        "--hidden-import", "httpx",
        "--hidden-import", "pyotp",
        os.path.join(ROOT, "__main__.py"),
    ]

    print("A construir jarvis-ctl.exe...")
    result = subprocess.run(cmd, cwd=ROOT)

    if result.returncode == 0:
        exe = os.path.join(ROOT, "dist", "jarvis-ctl.exe")
        size_mb = os.path.getsize(exe) / 1_048_576
        print(f"\nOK — {exe}  ({size_mb:.1f} MB)")
        print("\nPara distribuir:")
        print("  1. Copiar dist\\jarvis-ctl.exe para a maquina do colega")
        print("  2. Copiar jarvis-ctl.conf.dist para C:\\ProgramData\\JarvisCtl\\jarvis-ctl.conf")
        print("  3. Adicionar jarvis-ctl.exe ao PATH ou correr directamente")
    else:
        print(f"\nErro no build (exit code {result.returncode})")
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
