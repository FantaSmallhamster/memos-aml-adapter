#!/usr/bin/env python3
"""Local service management; never print API keys."""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid

ROOT = Path(__file__).resolve().parent


def environment():
    env = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            env[name] = value
    return env


def configure():
    key = getpass.getpass("MemOS API Key (hidden): ").strip()
    if not key or any(c in key for c in "\r\n\x00"):
        raise SystemExit("Invalid key")
    env = environment()
    env["MEMOS_API_KEY"] = key
    env["AML_API_KEY"] = env.get("AML_API_KEY") or secrets.token_urlsafe(32)
    lines = []
    for line in (ROOT / ".env").read_text().splitlines():
        name = line.split("=", 1)[0]
        lines.append(name + "=" + env[name] if name in ("MEMOS_API_KEY", "AML_API_KEY") else line)
    temp = ROOT / ".env.new"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write("\n".join(lines) + "\n")
    temp.replace(ROOT / ".env")
    print("MemOS key configured; independent adapter key configured; file mode 0600.", flush=True)


def stop():
    pid_path = ROOT / "adapter.pid"
    if pid_path.exists():
        pid = int(pid_path.read_text().strip())
        proc = Path("/proc") / str(pid)
        if proc.exists():
            if (proc / "cwd").resolve() != ROOT or b"server.py" not in (proc / "cmdline").read_bytes():
                raise SystemExit("PID does not identify this adapter; refusing to stop it")
            os.kill(pid, signal.SIGTERM)
            for _ in range(40):
                if not proc.exists() or ") Z " in (proc / "stat").read_text():
                    break
                time.sleep(0.1)
            else:
                raise SystemExit("Previous adapter did not stop")
    pid_path.unlink(missing_ok=True)


def restart():
    stop()
    pid_path = ROOT / "adapter.pid"
    with (ROOT / "adapter.log").open("ab") as log, open(os.devnull, "rb") as null:
        process = subprocess.Popen(
            ["/usr/bin/python3", "-u", str(ROOT / "server.py")], cwd=ROOT,
            env=environment(), stdin=null, stdout=log, stderr=log, start_new_session=True,
        )
    pid_path.write_text(str(process.pid) + "\n")
    for _ in range(50):
        if process.poll() is not None:
            raise SystemExit("Adapter exited; inspect adapter.log")
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=2) as response:
                assert json.load(response)["status"] == "ok"
            print("Adapter restarted; health OK; PID=" + str(process.pid), flush=True)
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise SystemExit("Health check failed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def smoke(base_url=None, allow_insecure_http=False):
    env = environment()
    if not env.get("MEMOS_API_KEY") or not env.get("AML_API_KEY"):
        raise SystemExit("Both keys must be configured")
    base = (base_url or "http://127.0.0.1:" + env.get("PORT", "8765")).rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise SystemExit("Use an origin URL without credentials, path, query, or fragment")
    allowed_http = parsed.scheme == "http" and (parsed.hostname == "127.0.0.1" or allow_insecure_http)
    if not parsed.hostname or (parsed.scheme != "https" and not allowed_http):
        raise SystemExit("Authenticated smoke requires HTTPS or loopback HTTP")
    opener = urllib.request.build_opener(NoRedirect())
    checks = []
    report = {"base_url": base, "started_at": datetime.now(timezone.utc).isoformat(), "checks": checks}
    def passed(name, **metadata):
        checks.append({"name": name, **metadata})
        print("PASS: " + name + (" " + json.dumps(metadata) if metadata else ""), flush=True)

    def call(path, body, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + env["AML_API_KEY"]
        request = urllib.request.Request(base + path, json.dumps(body).encode(), headers, method="POST")
        try:
            with opener.open(request, timeout=1750) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, {"detail": "HTTP request rejected"}

    run = uuid.uuid4().hex
    user = "adapter-smoke:" + run
    report["synthetic_user_id"] = user
    payload = {
        "request_id": user + ":chunk-0", "user_id": user, "session_id": user + ":session-0",
        "messages": [{"role": "user", "content": "This is a synthetic adapter test. My favorite fruit is mango and my favorite color is turquoise."}],
    }
    status, _ = call("/search", {}, False)
    assert status == 401, "Unauthenticated request was not rejected"
    passed("unauthenticated requests rejected")
    started = time.monotonic()
    status, result = call("/add", payload)
    if status != 200:
        raise SystemExit("Add failed (HTTP " + str(status) + "); inspect server diagnostics")
    assert result == {"success": True, "request_id": payload["request_id"], "user_id": user, "session_id": payload["session_id"]}
    passed("synchronous Add and response IDs", seconds=round(time.monotonic() - started, 2))
    status, repeated = call("/add", payload)
    assert status == 200 and repeated == result
    passed("identical Add retry")
    changed = dict(payload, messages=[{"role": "user", "content": "Different synthetic payload"}])
    status, _ = call("/add", changed)
    assert status == 409, "Changed Add payload was not rejected"
    passed("changed payload with same request_id rejected")
    started = time.monotonic()
    status, found = call("/search", {"user_id": user, "query": "What is my favorite fruit?", "top_k": 100})
    if status != 200:
        raise SystemExit("Search failed (HTTP " + str(status) + "); inspect server diagnostics")
    rows = found["data"]
    assert 0 < len(rows) <= 100, "Search returned no records or too many"
    assert all(isinstance(row.get("id"), str) and isinstance(row.get("content"), str) and row["content"] for row in rows)
    assert any("mango" in row["content"].lower() or "芒果" in row["content"] for row in rows), "Written fact not retrieved"
    passed("live MemOS Search retrieved synthetic fact", records=len(rows), seconds=round(time.monotonic() - started, 2))
    status, isolated = call("/search", {"user_id": user + ":other-user", "query": "What is my favorite fruit?", "top_k": 100})
    assert status == 200 and isolated == {"data": []}, "Isolation test failed"
    passed("different user_id cannot retrieve test memory")
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["result"] = "passed"
    destination = ROOT / "data" / ("smoke-" + run + ".json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(report, out, indent=2)
        out.write("\n")
    print("Report: " + str(destination), flush=True)
    print("SMOKE PASSED (adapter-level test, not official AML Smoke).", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["configure", "stop", "restart", "smoke"])
    parser.add_argument("--base-url", help="HTTPS origin for public smoke; defaults to loopback")
    parser.add_argument("--allow-insecure-http", action="store_true", help="Explicitly permit public HTTP after accepting unencrypted credential transmission")
    args = parser.parse_args()
    if args.action == "smoke":
        smoke(args.base_url, args.allow_insecure_http)
    else:
        if args.base_url or args.allow_insecure_http:
            parser.error("Smoke flags only apply to smoke")
        {"configure": configure, "stop": stop, "restart": restart}[args.action]()
