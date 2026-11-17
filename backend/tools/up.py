"""Start the whole stack for interactive use / the demo video.

  python -m tools.up                 # simulator :9000, control plane :8000, mock receiver :9100
  python -m tools.up --dashboard     # ... and the React dashboard dev server :5173

Ctrl+C stops everything.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT.parent / "frontend"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "models.yaml"))
    ap.add_argument("--reject-first", type=int, default=2, help="mock receiver rejects first N callback attempts")
    ap.add_argument("--dashboard", action="store_true")
    ap.add_argument("--db", default=str(ROOT.parent / "reports" / "inferq.db"))
    a = ap.parse_args()
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    py = sys.executable
    procs = [
        subprocess.Popen([py, "-m", "simulator.server", "--port", "9000", "--config", a.config], cwd=ROOT, env=env),
        subprocess.Popen([py, "-m", "inferq.api", "--port", "8000", "--config", a.config, "--db", a.db],
                         cwd=ROOT, env=env),
        subprocess.Popen([py, "-m", "tools.mock_receiver", "--port", "9100", "--reject-first",
                          str(a.reject_first)], cwd=ROOT, env=env),
    ]
    if a.dashboard:
        npm = shutil.which("npm") or "npm"
        procs.append(subprocess.Popen([npm, "run", "dev"], cwd=FRONTEND, shell=os.name == "nt"))
    print("simulator      http://127.0.0.1:9000\ncontrol plane  http://127.0.0.1:8000  (docs: /docs)\n"
          "mock receiver  http://127.0.0.1:9100/callback" + ("\ndashboard      http://127.0.0.1:5173" if a.dashboard
                                                             else "") + "\nCtrl+C to stop.")
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
