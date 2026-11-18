# MemOS Cloud AML Adapter

Textual-track adapter exposing the Agent Memory Leaderboard contract and forwarding memory operations to MemOS Cloud.

## Endpoints

- `GET /health` is unauthenticated and returns `{"status":"ok"}`.
- `POST /add` maps `session_id` to MemOS `conversation_id`, converts Unix-millisecond timestamps to `chat_time`, and uses synchronous writes. The adapter conservatively splits large AML chunks before forwarding them and recursively splits again when MemOS returns `40305`. Completed sub-parts are recorded independently so an interrupted retry does not repeat successful writes. Repeated AML `request_id` values with identical payloads are idempotent; a changed payload returns HTTP 409.
- `POST /search` maps the same `user_id` to MemOS and returns ranked facts/preferences/profile/events/tool/skill memories as AML `data` items. For AML `top_k: 100`, it queries four disjoint MemOS memory-view groups at the provider's 25-result cap, merges duplicate IDs using their best score, globally reranks, and returns no more than the requested `top_k`. Oversized queries are split and their candidates are merged in the same way.
- Add defaults to two concurrent AML requests and uses a 30,000 estimated-token target for new coding histories; Search remains serialized. Upstream starts are paced, transient 429/5xx responses receive bounded internal retries and cooldown, and additional AML requests wait in a bounded queue. The per-request Add split plan is frozen in SQLite: requests that already began under the older 6,000-token plan continue with that exact plan after deployment or restart, while new requests use the larger chunks. MemOS business error codes are preserved in safe error details instead of being collapsed into a generic 502.

Add/Search accept `Authorization: Bearer <key>`, `Authorization: Token <key>`, or `X-Api-Key: <key>`. The adapter listens on `127.0.0.1` by default; put an HTTPS reverse proxy in front before binding public evaluation endpoints.

## Configuration

Copy `.env.example` to `.env`, then run `python3 manage.py configure` on the server. It receives the MemOS key through hidden input and creates an independent adapter key when one is not already configured. It writes `.env` with mode 0600. Keep this file private and out of version control.

## Run

```sh
python3 manage.py restart
python3 manage.py smoke
# Real authenticated HTTPS integration test (consumes MemOS quota):
python3 manage.py smoke --base-url https://YOUR-VERIFIED-HTTPS-ORIGIN
```

The restart command loads `.env`, starts a detached process, and records its PID. The smoke command consumes a small number of MemOS API calls using a unique synthetic user. It verifies Add, identical retry, changed-payload rejection, retrieval, and user isolation. Credential-bearing requests never follow redirects. Successful reports under `data/smoke-*.json` contain only test metadata. This is an adapter integration test, not the official AML Smoke.

## HTTP gateway

The optional gateway listens on port 18794 and forwards `/health`, `/add`, and `/search` to the local adapter. HTTP sends the adapter key and request body without encryption. Use HTTPS for remote traffic that includes credentials.

```sh
python3 gateway.py status
python3 gateway.py start
python3 gateway.py stop
```

## User-level automatic recovery

```sh
python3 supervisor.py status
python3 supervisor.py check
python3 supervisor.py install
python3 supervisor.py uninstall
```

The watchdog checks process identity and can recover the adapter, gateway, and Quick Tunnel. It leaves the adapter to systemd when the systemd unit is installed. Uninstalling the watchdog leaves running processes in place.

## HTTPS deployment

The Linux Nginx and systemd templates use port 18794, the example address `192.0.2.10`, and the service account `memos-aml`. Before installing, replace the example address with your server IP in both `deploy/install-system.sh` and `deploy/memos-aml-18794.nginx.conf`. Update the TLS paths in both files to point to a certificate and private key for that IP. Give the service account access to the checkout at `/opt/memos-aml-adapter` and write access to its `data/` directory. If you use a different account or checkout path, update the installer and systemd unit together.

Stop the HTTP gateway if it is using port 18794, then run the installer from the configured checkout.

```sh
sudo bash deploy/install-system.sh
sudo systemctl status memos-aml
```

Use `sudo systemctl restart memos-aml` once systemd manages the adapter. Verify HTTPS with `python3 manage.py smoke --base-url https://YOUR-SERVER-IP:18794`.

## Tests

```sh
python3 -m unittest -q
python3 deploy/verify-long-request.py --seconds 135
```

The long-request probe requires Nginx and OpenSSL. It runs against a disposable TLS certificate and a synthetic slow origin on loopback.

## Temporary HTTPS

```sh
python3 tunnel.py status
python3 tunnel.py start
python3 tunnel.py stop
```

Cloudflare Quick Tunnels require `cloudflared`. Their hostnames change when recreated. The current URL is recorded locally in `data/tunnel.json`. Check the [Cloudflare Quick Tunnel documentation](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) before using one for requests with long deadlines.

The SQLite ledger deduplicates completed writes. If the upstream write succeeds but the adapter crashes or loses the response before recording completion, a retry may write again. A `running` Add response is rejected.
