#!/usr/bin/env python3
"""Per-user cron watchdog. A recovered Quick Tunnel gets a new hostname."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shlex
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BEGIN = "# BEGIN memos-aml-adapter watchdog"
END = "# END memos-aml-adapter watchdog"


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    temporary = path.with_suffix(".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, indent=2)
        out.write("\n")
    temporary.replace(path)


def alive(filename, expected):
    path = ROOT / filename
    if not path.exists():
        return False
    pid = int(path.read_text())
    proc = Path("/proc") / str(pid)
    try:
        if not proc.exists() or ") Z " in (proc / "stat").read_text():
            return False
        if (proc / "cwd").resolve() != ROOT or expected not in (proc / "cmdline").read_bytes():
            raise RuntimeError("PID identity mismatch: " + filename)
        return True
    except FileNotFoundError:
        return False


def healthy():
    import manage
    port = manage.environment().get("PORT", "8765")
    try:
        with urllib.request.urlopen("http://127.0.0.1:" + port + "/health", timeout=3) as response:
            return response.status == 200 and json.load(response) == {"status": "ok"}
    except (OSError, ValueError):
        return False


def run(command):
    result = subprocess.run(["/usr/bin/python3", *command], cwd=ROOT,
                            capture_output=True, text=True, timeout=65)
    if result.returncode:
        # Keep diagnostics free of subprocess output/credentials.
        raise RuntimeError(command[0] + " failed; inspect its service log")


def check():
    DATA.mkdir(mode=0o700, exist_ok=True)
    with (DATA / "supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        log = logging.getLogger("watchdog")
        log.setLevel(logging.INFO)
        log.addHandler(RotatingFileHandler(DATA / "supervisor.log", maxBytes=1024*1024, backupCount=2))
        state = {"checked_at": timestamp(), "actions": [], "errors": []}
        try:
            # Once the prepared systemd service is installed, it owns the adapter.
            system_managed = Path("/etc/systemd/system/memos-aml.service").exists()
            if system_managed:
                state["adapter_manager"] = "systemd"
            else:
                state["adapter_manager"] = "user-cron"
                running = alive("adapter.pid", b"server.py")
                ok = healthy()
                failures_path = DATA / "supervisor.json"
                previous = json.loads(failures_path.read_text()) if failures_path.exists() else {}
                failures = 0 if ok else previous.get("health_failures", 0) + 1
                state["health_failures"] = failures
                if not running and ok:
                    raise RuntimeError("Healthy adapter has no matching PID; refusing to take ownership")
                if not running or failures >= 3:
                    run([str(ROOT / "manage.py"), "restart"])
                    state["actions"].append("adapter restarted")
                    state["health_failures"] = 0
            state["adapter_healthy"] = healthy()
        except Exception as error:
            state["errors"].append(str(error))
        try:
            if (DATA / "http-gateway.enabled").exists() and not Path("/etc/systemd/system/memos-aml.service").exists():
                import gateway
                if not gateway.alive():
                    run([str(ROOT / "gateway.py"), "start"])
                    state["actions"].append("fixed HTTP gateway restarted")
                state["http_gateway"] = {"running": gateway.alive(), "url": gateway.LOCAL_URL}
        except Exception as error:
            state["errors"].append(str(error))
        try:
            old = json.loads((DATA / "tunnel.json").read_text()) if (DATA / "tunnel.json").exists() else {}
            if not alive("tunnel.pid", b"cloudflared"):
                run([str(ROOT / "tunnel.py"), "start"])
                new = json.loads((DATA / "tunnel.json").read_text())
                state["actions"].append("temporary tunnel restarted; public URL may have changed")
                if old.get("url") != new["url"]:
                    log.warning("%s URL changed: %s -> %s", timestamp(), old.get("url"), new["url"])
            state["tunnel"] = json.loads((DATA / "tunnel.json").read_text())
        except Exception as error:
            state["errors"].append(str(error))
        atomic_json(DATA / "supervisor.json", state)
        if state["actions"] or state["errors"]:
            log.warning("%s %s", timestamp(), json.dumps(state))
        if state["errors"]:
            raise SystemExit(1)


def crontab_text():
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode and "no crontab" not in result.stderr.lower():
        raise SystemExit("Cannot read crontab; existing configuration preserved")
    return result.stdout


def without_block(text):
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise SystemExit("Watchdog markers are inconsistent; refusing to change crontab")
    if BEGIN in text:
        start, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        return start.rstrip("\n") + "\n" + tail.lstrip("\n")
    return text


def install(remove=False):
    DATA.mkdir(mode=0o700, exist_ok=True)
    old = crontab_text()
    new = without_block(old)
    if not remove:
        command = "/usr/bin/python3 " + shlex.quote(str(ROOT / "supervisor.py")) + " check >/dev/null 2>&1"
        new = new.rstrip("\n") + "\n" + BEGIN + "\n@reboot " + command + "\n* * * * * " + command + "\n" + END + "\n"
    if new == old:
        print("Crontab already configured")
        return
    backup = DATA / ("crontab-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".txt")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(old)
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    print("User watchdog " + ("removed" if remove else "installed: every minute and at reboot"))


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "install", "uninstall", "status"])
    action = parser.parse_args().action
    if action == "check":
        check()
    elif action == "status":
        print((DATA / "supervisor.json").read_text())
    else:
        install(remove=action == "uninstall")
