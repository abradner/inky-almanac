# Operations

Running the frame day to day: over-the-air updates, USB mode, the network,
where credentials live, recovery and known limits. Deploy commands are in
[development.md](development.md#deploying).

## Over-the-air updates

After one USB install (the boot guard plus the signing key), frame code
updates over WiFi:

1. `deploy-server.sh` signs `frame/almanac_frame.py`, refusing uncommitted
   code. The signature is HMAC-SHA256 over the name, version, sha256 and
   size.
2. The renderer advertises the release in `/frame.json`.
3. After a refresh that drew, the frame checks that the release is newer
   than what it runs and that the signature, size and sha256 all match. It
   records a trial, keeps the old file as `.prev`, and reboots.
4. The new version stays on trial until it draws a poster. `main.py` restores
   `.prev` if the new version won't import, or doesn't draw within 3 boots or
   30 minutes. A version that won't import, or fails two trials, is never
   retried.

**Checking that an update took.** With the frame on the wall, read the
renderer's log instead of the board: one download of
`/ota/almanac_frame.py`, a trial draw seconds later, and no second download
afterwards means the trial committed ([gotchas.md](gotchas.md#deploy-and-ota)).
Expect two redraws close together after an install: the trial must draw.

The **signing key** lives on the machine you deploy from (`OTA_KEY_FILE` in
`deploy.env`), in your password manager, and on the frame (`OTA_KEY.py`,
installed with `deploy-frame.sh --local --ota-key`). It is never on the renderer, which can serve releases but can't
forge one. Only `almanac_frame.py` updates over the air; `main.py`, `config.py`
and the WiFi details change only over USB. The board records its update state
in `/ota.json`.

## Running on a cluster

The renderer can run as a container on Kubernetes instead of on a docker host
(`scripts/deploy-server.sh`). The pieces:

- **Image.** `.github/workflows/docker.yml` runs the server tests, then
  publishes a multi-arch image to `ghcr.io/<owner>/inky-almanac` on every push
  to `main` (tag: the long commit sha) and on `v*` tags (the tag name as well).
  There is no `:latest`; deployments pin the digest the run prints. The image
  is generic: no location, no settings, no signed release inside.
- **Settings.** The `ALMANAC_*` variables from `server.env` become environment
  variables on the container, from a Secret. Nothing in a public repo names a
  place.
- **The frame release.** The signed `manifest.json` and `almanac_frame.py` are
  mounted read-only at `/app/ota` (the server's `ota_dir`) from a Secret. With
  the mount empty or absent the renderer serves no release (`/frame.json` has
  no `code`, `/ota/almanac_frame.py` is a 404) and everything else works.
- **Writes.** The root filesystem can be read-only. The server writes only
  under `/var/cache/almanac` (feed caches, `radar-mode.json`,
  `frame-last-seen.json`, `shuffle.json`, photos and ERA5 chunks). Mount an
  `emptyDir` there. Nothing needs `/tmp`.
- **Probes.** The server warms every feed before it listens, up to about a
  minute on a cold cache, so a startup probe on `/healthz` needs a couple of
  minutes of budget, and liveness and readiness start only after it passes.
- **Cache lifetime.** With an `emptyDir` the cache dies with the pod: after a
  restart the ERA5 normals rebuild in the background (the poster uses its
  placeholder meanwhile) and radar mode resets to off. Both recover alone.

### Releasing frame code to the cluster

On the docker host, `deploy-server.sh` signs and ships in one step. On the
cluster the key stays on your machine and the signed files travel through the
password manager:

1. Commit the frame change. Run `scripts/release-ota.sh`. It signs (via
   `sign_ota.py`), re-checks the manifest against the file, and prints the
   exact commands for the next steps.
2. Attach `manifest.json` and `almanac_frame.py` to the `inky-almanac-ota`
   item with the printed `op item edit ... 'name[file]=path'` command. Files,
   not text fields: the frame checks the sha256, so the bytes must arrive
   exactly as signed.
3. Force the sync of the `inky-almanac-ota` ExternalSecret (printed too), or
   wait for its refresh interval. The kubelet updates the mounted files in
   place within about a minute; no restart.
4. `curl http://<renderer>:8077/frame.json | jq .code` shows the new version.

The release `version` must be higher than the one the frame runs, or the frame
ignores it.

### Rolling back to the docker host

The host path still works untouched: `scripts/deploy-server.sh` rebuilds the
container there. The frame reaches the renderer by the `SERVER` in its
`config.py`; if that is a DNS name, repoint the name's A record at the docker
host's address (and allow the frame to reach it) and the frame follows on its
next wake. If `SERVER` is an IP, the frame has to be re-pointed over USB or by
an over-the-air release that changes it.

## USB mode (plan B)

If the frame's WiFi can't reach the server:
1. Set `MODE = "usb"` in `frame/config.py`, and redeploy the frame.
2. Run `scripts/deploy-server.sh --usb`.

The container then pushes each new poster down the USB cable. The buttons do
nothing in this mode.

---

## Network

The frame needs to reach the renderer's port 8077 over WiFi; nothing needs to
reach the frame. If the frame lives on an isolated IoT network, allow just
that: the frame's address to the renderer's address, TCP 8077. Give the frame
a DHCP reservation, point `SERVER` in `frame/config.py` at the renderer's IP
(an IP is more robust than a name for a device like this, unless the name is a
local DNS record you control: see [gotchas.md](gotchas.md#networking-and-deploy)),
and publish the container on that address only (`BIND` in `deploy.env`).

## Credentials and local settings

None are in this repo. Your own values live in gitignored files, each with an
`.example` next to it:

| File | Holds |
|---|---|
| `frame/config.py` | `MODE`, `SERVER` |
| `deploy.env` | renderer host, published address, the frame's serial device, `OTA_KEY_FILE` |
| `server.env` | your location and other `ALMANAC_*` settings for the renderer (on a cluster: the same variables, from a Secret) |
| `WIFI_CONFIG.py` (on the board only) | `SSID`, `PSK`, `COUNTRY` (see `frame/WIFI_CONFIG.example.py`) |
| `OTA_KEY.py` (on the board only) | the over-the-air signing key |

Keep the OTA key file at 0600 and a copy in your password manager. Before the
first install, back up the board's stock filesystem (it holds the WiFi details
Pimoroni's firmware was set up with): `mpremote fs cp -r :. ./stock-fs`.

## Recovery

**The app misbehaves.** Hold **E** while plugging in (safe mode), or connect
with `mpremote`, which stops the app. Fix the files, then reset.

**Reflash MicroPython.** The firmware is Pimoroni's Inky Frame build for the
Pico W: `pico_w_inky-v1.29.0-micropython.uf2` from
<https://github.com/pimoroni/inky-frame/releases/tag/v1.29.0>
(sha256 `23f96c51c899dcfd2ef5f40845596763893bf8bc136fde1ebc92285f51ea6855`).

1. Enter the bootloader: `mpremote exec "import machine; machine.bootloader()"`,
   or hold **BOOTSEL** on the Pico W (on the back) while plugging in.
2. Copy the `.uf2` onto the `RPI-RP2` drive that appears. If the filesystem is
   corrupt, flash Raspberry Pi's `flash_nuke.uf2` first.
3. Reinstall:
   ```bash
   scripts/deploy-frame.sh --local --clean-stock --wifi-config PATH/TO/WIFI_CONFIG.py --ota-key "$OTA_KEY_FILE" --reset
   ```

**Back to stock.** Copy your `stock-fs` backup back with `mpremote fs cp -r`.

**Remove the server.** On the renderer's host:
```bash
docker rm -f inky-almanac; docker rmi inky-almanac:dev; docker volume rm inky-almanac-cache; rm -rf ~/inky-almanac ~/inky-venv
```

## Known limits

- The SES source reads HazardWatch's page, since SES publishes no open API.
  If the page changes, the poster says so. For a sanctioned feed, SES asks
  you to email its warnings team: warnings.capability@ses.nsw.gov.au.
- Battery operation (power-off between refreshes, RTC wake) is written for
  but not yet proven on this board; it has run on USB.
- Keep the poster and the status page private. BOM's data, including the
  anonymous FTP observations, is licensed for personal, non-commercial use,
  and SES wants its warnings republished in full.
