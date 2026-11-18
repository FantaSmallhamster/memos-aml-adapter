#!/bin/bash
# Administrator entry point. Does not alter existing 80/443 virtual hosts.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo 'Run this script as administrator with sudo.' >&2
    exit 1
fi

project=/opt/memos-aml-adapter
site=/etc/nginx/sites-available/memos-aml-18794
enabled=/etc/nginx/sites-enabled/memos-aml-18794
unit=/etc/systemd/system/memos-aml.service
certificate=/etc/letsencrypt/live/memos-aml-adapter/fullchain.pem
private_key=/etc/letsencrypt/live/memos-aml-adapter/privkey.pem

if [[ -e "$site" || -L "$enabled" || -e "$enabled" || -e "$unit" ]]; then
    echo 'Installation target already exists; review it before reinstalling.' >&2
    exit 1
fi
test -r "$certificate"
test -r "$private_key"
openssl x509 -in "$certificate" -noout -checkip 192.0.2.10
openssl x509 -in "$certificate" -noout -checkend 86400
if ss -Hltn 'sport = :18794' | /usr/bin/grep -q .; then
    echo 'Port 18794 is already in use; refusing to replace another listener.' >&2
    exit 1
fi
if systemctl is-active --quiet memos-aml.service; then
    echo 'Service name already in use; review it first.' >&2
    exit 1
fi

install -o root -g root -m 0644 "$project/deploy/memos-aml-18794.nginx.conf" "$site"
ln -s "$site" "$enabled"
if ! /usr/sbin/nginx -t; then
    rm "$enabled" "$site"
    echo 'New Nginx configuration rejected; existing configuration preserved.' >&2
    exit 1
fi

install -o root -g root -m 0644 "$project/memos-aml.service" "$unit"
systemctl daemon-reload
# Stop only the previously deployed adapter, as its unprivileged owner.
runuser -u memos-aml -- /usr/bin/python3 "$project/manage.py" stop
systemctl enable --now memos-aml.service
healthy=false
for attempt in {1..20}; do
    if curl --silent --show-error --fail --max-time 2 http://127.0.0.1:8765/health; then
        healthy=true
        break
    fi
    sleep 1
done
if [[ "$healthy" != true ]]; then
    echo 'Adapter health check failed; HTTPS configuration was not activated.' >&2
    exit 1
fi
systemctl reload nginx
curl --silent --show-error --fail --max-time 10 https://192.0.2.10:18794/health
echo
echo 'HTTPS configured. Allow inbound TCP 18794 in existing firewall/security-group policy if needed.'
echo 'Retain the existing automatic IP certificate renewal and Nginx reload mechanism.'
echo 'Public endpoints: https://192.0.2.10:18794/{add,search,health}'
