"""
agent_watchdog.py — Watchdog para o agent_daemon.

Lança o agent_daemon como subprocesso e reinicia-o automaticamente
se este terminar inesperadamente. Comportamento:

  - Backoff exponencial entre reinicios (1s → 2s → 4s → … max 60s)
  - Reinicia sem limite de tentativas
  - Se o daemon correr mais de STABLE_UPTIME_SECONDS considera-se
    estável e repõe o backoff a zero
  - Ctrl+C / SIGTERM encerra o watchdog e o daemon filho
"""

import subprocess
import sys
import os
import time
import signal

_HERE               = os.path.dirname(os.path.abspath(__file__))
_DAEMON_SCRIPT      = os.path.join(_HERE, "agent_daemon.py")
_PYTHON             = sys.executable
STABLE_UPTIME_SECS  = 30   # segundos para considerar o daemon estável
MAX_BACKOFF_SECS    = 60   # máximo de espera entre reinicios


def _run():
    backoff     = 1
    attempt     = 0
    _child      = None

    def _terminate(signum, frame):
        if _child and _child.poll() is None:
            _child.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT,  _terminate)

    print(f"[Watchdog] a monitorizar {_DAEMON_SCRIPT}")

    while True:
        attempt += 1
        print(f"[Watchdog] a iniciar daemon (tentativa={attempt} backoff={backoff}s)")

        start_ts = time.time()
        _child   = subprocess.Popen(
            [_PYTHON, _DAEMON_SCRIPT],
            cwd=_HERE,
        )

        _child.wait()
        uptime   = time.time() - start_ts
        exitcode = _child.returncode

        print(f"[Watchdog] daemon terminou | exitcode={exitcode} uptime={uptime:.1f}s")

        if exitcode == 0:
            # Saída limpa (ex: --setup-silent concluído) — não reiniciar
            print("[Watchdog] saída limpa (exitcode=0). A encerrar watchdog.")
            break

        if uptime >= STABLE_UPTIME_SECS:
            # Daemon correu tempo suficiente — repõe backoff
            backoff = 1
        else:
            backoff = min(backoff * 2, MAX_BACKOFF_SECS)

        print(f"[Watchdog] a reiniciar em {backoff}s ...")
        time.sleep(backoff)


if __name__ == "__main__":
    _run()
