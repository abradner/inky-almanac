#!/usr/bin/env bash
# Sign the frame code as an over-the-air release and print the commands that
# publish it to the cluster. For the renderer running on Kubernetes, where the
# signed release is not baked into the image but mounted from a 1Password item
# (see docs/operations.md, "Releasing frame code to the cluster").
#
#   scripts/release-ota.sh [KEY_FILE] [--version N] [--allow-dirty]
#
# KEY_FILE and the other arguments go to scripts/sign_ota.py unchanged
# (default key: $OTA_KEY_FILE, else ~/.config/inky-almanac/ota-key.txt).
# This script signs and PRINTS; it never calls `op` or `kubectl` itself, so
# nothing is published until you run the printed commands.
#
# Settings (environment, all optional):
#   OTA_OP_ITEM   the 1Password item holding the release   (default inky-almanac-ota)
#   OTA_OP_VAULT  its vault: the one the cluster's ExternalSecret store reads
#                 (default "Tooling - Athena")
#   OTA_K8S_NS    the namespace of the ExternalSecret      (default asn-production)
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
item=${OTA_OP_ITEM:-inky-almanac-ota}
vault=${OTA_OP_VAULT:-Tooling - Athena}
ns=${OTA_K8S_NS:-asn-production}
out="$here/server/ota"

python3 "$here/scripts/sign_ota.py" "$@"

# The cluster serves these two files byte for byte, and the frame checks the
# sha256 in the manifest against what it downloads. Re-check what we are about
# to publish before anything leaves this machine.
python3 - "$out" <<'PY'
import hashlib, json, sys
from pathlib import Path
d = Path(sys.argv[1])
m = json.loads((d / "manifest.json").read_text())
data = (d / m["name"]).read_bytes()
assert hashlib.sha256(data).hexdigest() == m["sha256"], "almanac_frame.py does not match its manifest"
assert len(data) == m["size"], "size mismatch"
print(f"checked: {m['name']} v{m['version']} matches its manifest")
PY

# The attachment names are the two basenames, exactly: the ExternalSecret
# reads them by name (`<item>/manifest.json`, `<item>/almanac_frame.py`).
# File attachments, not text fields: an attachment is byte-exact, and the
# frame's sha256 check would fail on a trailing newline or a CRLF that a field
# normalised. No file content goes through argv: `[file]=` takes a path.
# The dots are escaped: op reads an unescaped `.` in an assignment as
# section.field, so `manifest.json[file]=` attaches a file named "json" in a
# section "manifest" (seen 25 Nov 2026), which the ExternalSecret can't find.
cat <<EOF

Publish (run these yourself; the vault must be unlocked):

  # 1. Attach both files to the item.
  op item edit '$item' --vault '$vault' \\
    'manifest\\.json[file]=$out/manifest.json' \\
    'almanac_frame\\.py[file]=$out/almanac_frame.py'

  # 2. Check the item holds each name exactly ONCE. The ExternalSecret
  #    refuses a name that matches two attachments; if this prints a name
  #    twice, delete the older attachment in the 1Password app and re-run it.
  op item get '$item' --vault '$vault' --format json | jq -r '.files[].name' | sort

  # 3. Don't wait for the 24h refresh: force the sync, then watch it land.
  kubectl -n $ns annotate externalsecret inky-almanac-ota force-sync="\$(date +%s)" --overwrite
  kubectl -n $ns get externalsecret inky-almanac-ota

  # 4. The pod sees the new files within about a minute (kubelet refreshes
  #    the mounted Secret; no restart). Confirm the renderer advertises it:
  curl -fsS http://inky.asn.casa:8077/frame.json | jq .code

The frame installs it on its next successful refresh. The release version must
be higher than the one the frame runs (sign_ota.py uses the clock; --version N
overrides it).
EOF
