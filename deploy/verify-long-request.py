#!/usr/bin/env python3
"""Probe the staged HTTPS proxy on loopback using a disposable trusted QA cert."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=int, default=135)
args = parser.parse_args()
if not 1 <= args.seconds <= 1700:
    parser.error("--seconds must be between 1 and 1700")
root = Path(__file__).resolve().parents[1]


class Probe(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        time.sleep(args.seconds)
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


with tempfile.TemporaryDirectory(prefix="long-request-qa-", dir=root / "data") as directory:
    path = Path(directory)
    cert, key = path / "cert.pem", path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Probe)
    thread = threading.Thread(target=origin.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    snippet = (root / "deploy/memos-aml-18794.nginx.conf").read_text()
    snippet = snippet.replace("listen 18794 ssl;", "listen 127.0.0.1:" + str(port) + " ssl;")
    snippet = snippet.replace("http://127.0.0.1:8765", "http://127.0.0.1:" + str(origin.server_port))
    snippet = snippet.replace("/etc/letsencrypt/live/memos-aml-adapter/fullchain.pem", str(cert))
    snippet = snippet.replace("/etc/letsencrypt/live/memos-aml-adapter/privkey.pem", str(key))
    config = path / "nginx.conf"
    config.write_text("daemon off;\npid " + str(path / "nginx.pid") + ";\nerror_log stderr;\nevents {}\nhttp {\n"
                      + "access_log off;\n"
                      + "".join(name + " " + str(path / name) + ";\n" for name in (
                          "client_body_temp_path", "proxy_temp_path", "fastcgi_temp_path", "uwsgi_temp_path", "scgi_temp_path"))
                      + snippet + "\n}\n")
    with (path / "nginx.log").open("wb") as log:
        child = subprocess.Popen(["/usr/sbin/nginx", "-p", str(path), "-c", str(config)], stdout=log, stderr=log)
        try:
            for _ in range(50):
                if child.poll() is not None:
                    raise RuntimeError("QA proxy exited: " + (path / "nginx.log").read_text())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.1)
            context = ssl.create_default_context(cafile=str(cert))
            print("QA HTTPS proxy ready on loopback; delaying first response for " + str(args.seconds) + " seconds", flush=True)
            started = time.monotonic()
            with urllib.request.urlopen("https://127.0.0.1:" + str(port) + "/health", context=context,
                                        timeout=args.seconds + 30) as response:
                assert response.status == 200 and json.load(response) == {"status": "ok"}
            report = {"result": "passed", "delay_seconds": args.seconds, "elapsed_seconds": round(time.monotonic() - started, 2),
                      "scope": "staged Nginx configuration on loopback; disposable QA certificate; not public ingress",
                      "proxy_read_timeout_seconds": 1800}
            (root / "data/long-request-test.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report), flush=True)
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            origin.shutdown()
            origin.server_close()
