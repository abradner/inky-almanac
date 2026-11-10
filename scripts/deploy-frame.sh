#!/usr/bin/env bash
# Copy the frame app to an Inky Frame over USB.
#
#   scripts/deploy-frame.sh [--local [PORT]] [--wifi-config PATH] [--clean-stock] [--reset]
#
# --local [PORT]      the board is on THIS machine's USB; mpremote comes from
#                     mise (mise.toml). PORT defaults to the first
#                     /dev/cu.usbmodem*. Without --local the board is on HOST
#                     and mpremote runs there over ssh.
# --wifi-config PATH  also upload PATH as WIFI_CONFIG.py (never committed; keep
#                     it 0600). Remotely it travels over ssh into a 0700 dir on
#                     HOST and is deleted there straight after the copy.
# --ota-key PATH      also install the over-the-air signing key (64 hex, 0600)
#                     as OTA_KEY.py. Without it on the board, OTA stays off.
# --clean-stock       delete Pimoroni's stock launcher files from the board.
# --reset             reset the board afterwards. Any mpremote call stops the
#                     running app, so you almost always want this.
#
# Remote deploys read HOST, DEVICE, MPREMOTE and SSH_HOST_KEY_ALIAS from
# deploy.env (copy deploy.env.example). Needs frame/config.py (copy
# frame/config.py.example).
set -euo pipefail

_here=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$_here/deploy.env" ] && . "$_here/deploy.env"
HOST=${HOST:-}
DEVICE=${DEVICE:-}
MPREMOTE=${MPREMOTE:-'~/inky-venv/bin/mpremote'}
SSH_OPTS="-o BatchMode=yes${SSH_HOST_KEY_ALIAS:+ -o HostKeyAlias=$SSH_HOST_KEY_ALIAS -o StrictHostKeyChecking=yes}"
WIFI=""
CLEAN=0
LOCAL=""
RESET=0
OTAKEY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --local)
      if [ -n "${2:-}" ] && [ "${2#--}" = "$2" ]; then
        LOCAL="$2"; shift 2
      else
        LOCAL=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1 || true); shift
        [ -n "$LOCAL" ] || { echo "no /dev/cu.usbmodem* found; is the frame plugged in?" >&2; exit 1; }
      fi ;;
    --wifi-config) WIFI="$2"; shift 2 ;;
    --clean-stock) CLEAN=1; shift ;;
    --ota-key) OTAKEY="$2"; shift 2 ;;
    --reset) RESET=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

here=$_here
[ -f "$here/frame/config.py" ] || { echo "frame/config.py missing: copy frame/config.py.example and fill it in" >&2; exit 1; }
stock="daily_activity.py inky_helper.py led_pwm.py nasa-apod-daily nasa_apod.py news_headlines.py random_joke.py rtc_demo.py word_clock.py state.json"
if [ -n "$WIFI" ] && [ ! -s "$WIFI" ]; then
  echo "wifi config $WIFI is empty or missing" >&2; exit 1
fi
KEYPY=""
if [ -n "$OTAKEY" ] && [ -z "$LOCAL" ]; then
  # Over ssh the key would sit on the renderer's host, which the renderer
  # must never have (it could then sign releases). Install it locally only.
  echo "--ota-key needs --local: plug the frame into this machine" >&2; exit 2
fi
if [ -n "$OTAKEY" ]; then
  [ "$(tr -d '[:space:]' < "$OTAKEY" | wc -c | tr -d ' ')" = 64 ] || { echo "$OTAKEY: expected 64 hex characters" >&2; exit 1; }
  KEYDIR=$(mktemp -d); chmod 700 "$KEYDIR"; trap 'rm -rf "$KEYDIR"' EXIT
  KEYPY="$KEYDIR/OTA_KEY.py"
  ( umask 077; printf 'KEY = "%s"\n' "$(tr -d '[:space:]' < "$OTAKEY")" > "$KEYPY" )
fi

if [ -n "$LOCAL" ]; then
  M() { mise -C "$here" exec -- mpremote connect "$LOCAL" resume "$@"; }
  if [ "$CLEAN" = 1 ]; then
    for f in $stock; do M fs rm ":$f" >/dev/null 2>&1 || true; done
    M fs rm -r :lib >/dev/null 2>&1 || true
  fi
  for f in main.py almanac_frame.py config.py; do M fs cp "$here/frame/$f" ":$f" >/dev/null; done
  if [ -n "$WIFI" ]; then M fs cp "$WIFI" :WIFI_CONFIG.py >/dev/null; fi
  if [ -n "$KEYPY" ]; then M fs cp "$KEYPY" :OTA_KEY.py >/dev/null; fi
  # A hand install supersedes any over-the-air trial: forget it, and refuse
  # releases signed before now (they can't be newer than this code).
  M fs rm :almanac_frame.py.prev >/dev/null 2>&1 || true
  M exec "import json; f = open('/ota.json', 'w'); json.dump({'version': $(date +%s)}, f); f.close()" >/dev/null
  M fs ls :
  if [ "$RESET" = 1 ]; then
    M reset >/dev/null 2>&1 || true
    echo "board reset; the app is running"
  else
    echo "deployed. The app is stopped until you reset: rerun with --reset"
  fi
  exit 0
fi

: "${HOST:?set HOST in deploy.env, or use --local}"
: "${DEVICE:?set DEVICE in deploy.env}"
ssh $SSH_OPTS "$HOST" 'umask 077; rm -rf ~/inky-upload && mkdir -p ~/inky-upload'
scp $SSH_OPTS -q "$here"/frame/main.py "$here"/frame/almanac_frame.py "$here"/frame/config.py "$HOST":inky-upload/
if [ -n "$WIFI" ]; then
  scp $SSH_OPTS -q "$WIFI" "$HOST":inky-upload/WIFI_CONFIG.py
fi

ssh $SSH_OPTS "$HOST" "bash -s" <<REMOTE
set -euo pipefail
M=$MPREMOTE
cd ~/inky-upload
trap 'rm -f ~/inky-upload/WIFI_CONFIG.py' EXIT
if [ "$CLEAN" = 1 ]; then
  for f in $stock; do \$M connect $DEVICE resume fs rm ":\$f" >/dev/null 2>&1 || true; done
  \$M connect $DEVICE resume fs rm -r :lib >/dev/null 2>&1 || true
fi
for f in *.py; do \$M connect $DEVICE resume fs cp "\$f" ":\$f" >/dev/null; done
\$M connect $DEVICE resume fs rm :almanac_frame.py.prev >/dev/null 2>&1 || true
\$M connect $DEVICE resume exec "import json; f = open('/ota.json', 'w'); json.dump({'version': $(date +%s)}, f); f.close()" >/dev/null
\$M connect $DEVICE resume fs ls :
if [ "$RESET" = 1 ]; then \$M connect $DEVICE reset >/dev/null 2>&1 || true; echo "board reset; the app is running"; fi
REMOTE
[ "$RESET" = 1 ] || echo "deployed. The app is stopped until you reset: rerun with --reset"
