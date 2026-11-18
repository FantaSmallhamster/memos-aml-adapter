#!/usr/bin/env python3
"""Unprivileged syntax check; uses a disposable certificate, not production keys."""
import pathlib
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="https-qa-", dir=root / "data") as temporary:
    path = pathlib.Path(temporary)
    cert, key = path / "qa-cert.pem", path / "qa-key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    snippet = (root / "deploy/memos-aml-18794.nginx.conf").read_text()
    snippet = snippet.replace("/etc/letsencrypt/live/memos-aml-adapter/fullchain.pem", str(cert))
    snippet = snippet.replace("/etc/letsencrypt/live/memos-aml-adapter/privkey.pem", str(key))
    config = path / "nginx.conf"
    config.write_text(
        "pid " + str(path / "nginx.pid") + ";\nerror_log stderr;\nevents {}\nhttp {\n"
        + "access_log off;\n"
        + "".join(name + " " + str(path / name) + ";\n" for name in (
            "client_body_temp_path", "proxy_temp_path", "fastcgi_temp_path", "uwsgi_temp_path", "scgi_temp_path"
        ))
        + snippet + "\n}\n"
    )
    subprocess.run(["/usr/sbin/nginx", "-t", "-p", str(path), "-c", str(config)], check=True)
print("PASS: Nginx configuration syntax (production certificate access requires administrator).")
