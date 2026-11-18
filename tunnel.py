#!/usr/bin/env python3
"""User-owned temporary HTTPS tunnel; requires no administrator access."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parent
PID = ROOT / "tunnel.pid"
LOG = ROOT / "tunnel.log"
STATE = ROOT / "data/tunnel.json"


def process_alive(pid):
    path = Path("/proc") / str(pid)
    try:
        return path.exists() and ") Z " not in (path / "stat").read_text()
    except FileNotFoundError:
        return False


def stop():
    if PID.exists():
        pid = int(PID.read_text())
        if process_alive(pid):
            executable = (Path("/proc") / str(pid) / "exe").resolve()
            if executable != (ROOT / "bin/cloudflared").resolve():
                raise SystemExit("PID is not this tunnel; refusing to stop it")
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not process_alive(pid):
                    break
                time.sleep(0.1)
            else:
                raise SystemExit("Tunnel has not stopped")
        PID.unlink(missing_ok=True)
    STATE.unlink(missing_ok=True)


def status():
    if PID.exists() and process_alive(int(PID.read_text())):
        if STATE.exists():
            print(STATE.read_text())
        else:
            print("Tunnel process running; public URL is not ready")
    else:
        raise SystemExit("Tunnel is not running")


def start():
    if PID.exists() and process_alive(int(PID.read_text())):
        status()
        return
    STATE.unlink(missing_ok=True)
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as log, open(os.devnull, "rb") as null:
        child = subprocess.Popen([
            str(ROOT / "bin/cloudflared"), "tunnel", "--no-autoupdate", "--protocol", "http2",
            "--url", "http://127.0.0.1:8765", "--metrics", "127.0.0.1:0",
        ], cwd=ROOT, stdin=null, stdout=log, stderr=log, start_new_session=True)
    PID.write_text(str(child.pid) + "\n")
    for _ in range(100):
        if child.poll() is not None:
            raise SystemExit("Tunnel exited; inspect tunnel.log")
        text = LOG.read_text(errors="replace")
        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
        if match and "Registered tunnel connection" in text:
            info = {"url": match.group(), "pid": child.pid, "temporary": True}
            STATE.write_text(json.dumps(info) + "\n")
            print(json.dumps(info), flush=True)
            return
        time.sleep(0.5)
    raise SystemExit("Tunnel connection not ready; inspect tunnel.log")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "status", "stop"])
    {"start": start, "status": status, "stop": stop}[parser.parse_args().action]()
