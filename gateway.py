#!/usr/bin/env python3
"""User-owned fixed HTTP ingress, separate from the shared system Nginx."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data/http-gateway"
PID = ROOT / "gateway.pid"
LOCAL_URL = "http://127.0.0.1:18794"


def alive():
    if not PID.exists():
        return False
    process = Path("/proc") / str(int(PID.read_text()))
    try:
        if not process.exists() or ") Z " in (process / "stat").read_text():
            return False
        if (process / "exe").resolve() != Path("/usr/sbin/nginx").resolve() or str(DATA).encode() not in (process / "cmdline").read_bytes():
            raise RuntimeError("Gateway PID identity mismatch")
        return True
    except FileNotFoundError:
        return False


def stop():
    if alive():
        pid = int(PID.read_text())
        # Graceful shutdown lets outstanding Add/Search requests finish.
        os.kill(pid, signal.SIGQUIT)
        for _ in range(100):
            if not alive():
                break
            time.sleep(0.1)
        else:
            raise SystemExit("Gateway still draining; leave it running until requests finish")
    PID.unlink(missing_ok=True)


def start():
    if alive():
        print("HTTP gateway already running")
        return
    DATA.mkdir(parents=True, mode=0o700, exist_ok=True)
    snippet = (ROOT / "deploy/memos-aml-18794.http.nginx.conf").read_text()
    config = DATA / "nginx.conf"
    config.write_text("daemon off;\npid " + str(PID) + ";\nerror_log stderr;\nevents {}\nhttp {\n"
                      + "access_log off;\n"
                      + "".join(name + " " + str(DATA / name) + ";\n" for name in (
                          "client_body_temp_path", "proxy_temp_path", "fastcgi_temp_path", "uwsgi_temp_path", "scgi_temp_path"))
                      + snippet + "\n}\n")
    command = ["/usr/sbin/nginx", "-p", str(DATA), "-c", str(config)]
    subprocess.run(command + ["-t"], check=True, capture_output=True)
    with (DATA / "gateway.log").open("ab") as log, open(os.devnull, "rb") as null:
        child = subprocess.Popen(command, cwd=ROOT, stdin=null, stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        if child.poll() is not None:
            raise SystemExit("HTTP gateway exited; inspect data/http-gateway/gateway.log")
        try:
            with urllib.request.urlopen(LOCAL_URL + "/health", timeout=2) as response:
                assert json.load(response) == {"status": "ok"}
            if alive() and int(PID.read_text()) == child.pid:
                print("Fixed HTTP gateway ready on 0.0.0.0:18794")
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise SystemExit("HTTP gateway health check failed")


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    action = parser.parse_args().action
    if action == "status":
        print(json.dumps({"running": alive(), "url": LOCAL_URL}))
    else:
        {"start": start, "stop": stop}[action]()
