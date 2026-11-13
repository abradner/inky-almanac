#!/usr/bin/env bash
# Build and (re)start the renderer container on HOST.
#
#   scripts/deploy-server.sh          # WiFi mode: the frame polls :8077
#   scripts/deploy-server.sh --usb    # USB mode: also drive the frame over serial
#
# Settings come from deploy.env (copy deploy.env.example): HOST, BIND, DEVICE
# (USB mode), OTA_KEY_FILE, and optionally SSH_HOST_KEY_ALIAS.
# Cleanup: docker rm -f inky-almanac; docker rmi inky-almanac:dev;
#          docker volume rm inky-almanac-cache; rm -rf ~/inky-almanac
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$here/deploy.env" ] && . "$here/deploy.env"
: "${HOST:?set HOST in deploy.env (see deploy.env.example)}"
: "${BIND:?set BIND in deploy.env}"
DEVICE=${DEVICE:-}
SSH_OPTS="-o BatchMode=yes${SSH_HOST_KEY_ALIAS:+ -o HostKeyAlias=$SSH_HOST_KEY_ALIAS -o StrictHostKeyChecking=yes}"
export RSYNC_RSH="ssh $SSH_OPTS"
USB=""
if [ "${1:-}" = "--usb" ]; then
  : "${DEVICE:?set DEVICE in deploy.env for USB mode}"
  USB="-v /dev:/dev --device-cgroup-rule='c 166:* rmw' --group-add 20 -e ALMANAC_USB_DEVICE=$DEVICE"
fi

# Sign the frame code as an over-the-air release when the key is here.
OTA_KEY_FILE=${OTA_KEY_FILE:-}
if [ -n "$OTA_KEY_FILE" ] && [ -s "$OTA_KEY_FILE" ]; then
  python3 "$here/scripts/sign_ota.py" "$OTA_KEY_FILE"
else
  echo "no OTA key (OTA_KEY_FILE): deploying without an over-the-air release"
fi
rsync -a --delete --exclude __pycache__ "$here/server/" "$HOST:inky-almanac/server/"
# Your location and other ALMANAC_* settings (server.env, gitignored).
ENVFILE=""
if [ -f "$here/server.env" ]; then
  rsync -a "$here/server.env" "$HOST:inky-almanac/server.env"
  ENVFILE="--env-file \$HOME/inky-almanac/server.env"
else
  echo "no server.env: the renderer uses the defaults in server/almanac/config.py"
fi
ssh $SSH_OPTS "$HOST" "bash -s" <<REMOTE
set -euo pipefail
cd ~/inky-almanac/server
docker build -q -f Dockerfile.test -t inky-almanac-test . >/dev/null
docker run --rm inky-almanac-test python -m pytest -q | tail -1
docker rmi inky-almanac-test >/dev/null
docker build -q -t inky-almanac:dev . >/dev/null
docker rm -f inky-almanac >/dev/null 2>&1 || true
docker run -d --name inky-almanac --restart unless-stopped -p $BIND:8077:8077 \
  -v inky-almanac-cache:/var/cache/almanac --label project=inky-almanac $ENVFILE $USB inky-almanac:dev >/dev/null
# The server warms every feed cache before it starts listening (up to ~1 min).
for i in \$(seq 1 45); do curl -fsS -m 3 $BIND:8077/healthz 2>/dev/null && exit 0; sleep 2; done
echo "renderer did not come up; docker logs inky-almanac" >&2; exit 1
REMOTE
