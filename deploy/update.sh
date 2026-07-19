#!/usr/bin/env bash
# Push the latest code to the droplet without re-provisioning.
# Usage: deploy/update.sh root@<droplet-ip>
set -euo pipefail
HOST="${1:?usage: deploy/update.sh user@host}"
ssh "$HOST" '
  set -e
  cd /opt/gobo
  sudo -u gobo git pull --ff-only
  sudo -u gobo /usr/local/bin/uv sync
  systemctl restart gobo
  systemctl --no-pager status gobo | head -5
'
