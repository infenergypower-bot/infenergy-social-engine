from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]


def main():
    """Serve HTTP and supervise a separate, durable creative job process."""
    import json
    from production_backup import backup_before_migration
    data_dir=os.environ.get("DATA_DIR",str(ROOT/"data"))
    backup=backup_before_migration(data_dir, version='posting-schedule-v1')
    print(json.dumps({"event":"pre_migration_backup",**backup}),flush=True)
    import worker
    stop = threading.Event()
    children = []
    enabled = os.environ.get("PRODUCTION_WORKER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

    def supervise():
        while not stop.is_set():
            child = subprocess.Popen([sys.executable, "-u", str(ROOT / "scripts/production_worker.py")], cwd=ROOT)
            children.append(child)
            while not stop.wait(2):
                if child.poll() is not None:
                    print("[production] worker exited; restarting after backoff", flush=True)
                    break
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
            children.remove(child)
            if stop.wait(5): break

    supervisor = threading.Thread(target=supervise, daemon=True)
    if enabled: supervisor.start()

    def shutdown(signum, frame):
        stop.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        worker.main()
    finally:
        stop.set()
        if enabled: supervisor.join(timeout=15)


if __name__ == "__main__":
    main()
