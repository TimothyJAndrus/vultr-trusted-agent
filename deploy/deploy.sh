#!/usr/bin/env bash
# Usage: VULTR_INFERENCE_API_KEY=... deploy/deploy.sh <vm-ip>
set -euo pipefail
IP=$1
ssh root@$IP mkdir -p /opt/tas/app
rsync -az --delete --exclude .git --exclude __pycache__ ./ root@$IP:/opt/tas/app/
ssh root@$IP bash -s <<REMOTE
set -e
test -d /opt/tas/venv || python3 -m venv /opt/tas/venv
/opt/tas/venv/bin/pip -q install -r /opt/tas/app/requirements.txt
umask 077; printf 'VULTR_INFERENCE_API_KEY=%s\n' '$VULTR_INFERENCE_API_KEY' > /opt/tas/env
test -f /opt/tas/HOST_CANARY.txt || echo "host canary written \$(date -u +%FT%TZ); the sandbox cannot see this file" > /opt/tas/HOST_CANARY.txt
cp /opt/tas/app/deploy/tas.service /etc/systemd/system/tas.service
systemctl daemon-reload; systemctl enable -q tas; systemctl restart tas
REMOTE
