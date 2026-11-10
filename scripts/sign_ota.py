#!/usr/bin/env python3
"""Sign frame/almanac_frame.py as an over-the-air release.

Writes server/ota/almanac_frame.py and server/ota/manifest.json, which the
renderer serves to the frame. The signature is HMAC-SHA256 over
"inky-almanac-ota-v1:<name>:<sha256>:<size>" with a key that lives only on
the signing machine (KEY_FILE, 0600) and on the frame (OTA_KEY.py). The renderer
never holds the key, so it can serve code but not forge it.

    scripts/sign_ota.py [KEY_FILE]          # default $OTA_KEY_FILE, else ~/.config/inky-almanac/ota-key.txt
    scripts/sign_ota.py --new-key KEY_FILE  # create a key (never printed)
"""

import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "almanac_frame.py"
DOMAIN = b"inky-almanac-ota-v1"
DEFAULT_KEY = Path(os.environ.get("OTA_KEY_FILE") or Path.home() / ".config/inky-almanac/ota-key.txt")


def message(name, version, sha, size):
    return b":".join([DOMAIN, name.encode(), str(version).encode(), sha.encode(), str(size).encode()])


def sign(key_hex, src, out_dir, version=None):
    """version: a number that only goes up (default: now, in unix seconds).
    The frame refuses any release not newer than the one it runs."""
    version = int(time.time()) if version is None else int(version)
    data = Path(src).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    sig = hmac.new(bytes.fromhex(key_hex), message(NAME, version, sha, len(data)), hashlib.sha256).hexdigest()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out_dir / NAME)
    manifest = {"name": NAME, "version": version, "sha256": sha, "size": len(data), "sig": sig, "path": "/ota/" + NAME}
    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def new_key(path):
    path = Path(path)
    if path.exists():
        sys.exit(f"{path} exists; refusing to overwrite a key the frame may hold")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(32))
    print(f"new key written to {path} (0600)")


def main(argv):
    if argv[:1] == ["--new-key"]:
        return new_key(argv[1] if len(argv) > 1 else DEFAULT_KEY)
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "frame/" + NAME], capture_output=True, text=True).stdout.strip()
    if dirty and "--allow-dirty" not in argv:
        sys.exit(f"frame/{NAME} has uncommitted changes; commit first (or pass --allow-dirty)")
    argv = [a for a in argv if a != "--allow-dirty"]
    version = None
    if "--version" in argv:  # e.g. to recover from a clock that ran ahead
        i = argv.index("--version")
        version = int(argv[i + 1])
        del argv[i : i + 2]
    key_file = Path(argv[0]) if argv else DEFAULT_KEY
    key = key_file.read_text().strip()
    if len(key) != 64:
        sys.exit(f"{key_file}: expected 64 hex characters")
    m = sign(key, ROOT / "frame" / NAME, ROOT / "server" / "ota", version=version)
    print(f"signed {NAME} v{m['version']} sha256 {m['sha256'][:12]} size {m['size']}")


if __name__ == "__main__":
    main(sys.argv[1:])
