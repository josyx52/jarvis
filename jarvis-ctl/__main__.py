import sys
import os

# Forçar UTF-8 no terminal Windows para Rich renderizar correctamente
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
# Activar código de página UTF-8 no Windows (chcp 65001)
os.system("chcp 65001 > nul 2>&1")

from cli import app

if __name__ == "__main__":
    app()
