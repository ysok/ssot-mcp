"""Run MCP server and admin UI together (e.g. container entrypoint)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main() -> None:
    env = os.environ.copy()
    procs: list[subprocess.Popen[bytes]] = []

    def terminate_all(*_args: object) -> None:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, terminate_all)
    signal.signal(signal.SIGINT, terminate_all)

    procs.append(subprocess.Popen([sys.executable, "-m", "ssot_mcp.mcp.server"], env=env))
    procs.append(subprocess.Popen([sys.executable, "-m", "ssot_mcp.ui.main"], env=env))

    while True:
        for i, p in enumerate(procs):
            code = p.poll()
            if code is not None:
                terminate_all()
                sys.exit(code if code != 0 else 1)
        time.sleep(0.4)


if __name__ == "__main__":
    main()
