# Gotchas: what bit us, and what to do about it

Hard-won lessons, grouped by where they bite. Each one says what you see,
why it happens and what to do. Where the code already handles it, the entry
says where, so nobody "simplifies" the fix away.

- [Hardware and firmware](#hardware-and-firmware)
- [Networking and deploy](#networking-and-deploy)
- [Deploy and OTA](#deploy-and-ota)
- [Data sources](#data-sources)
- [Rendering](#rendering)
- [Testing and process](#testing-and-process)
- [Git and GitHub](#git-and-github)

## Hardware and firmware

The board is a Pimoroni **Inky Frame 7.3"** (800×480, seven-colour ACeP
e-ink, Raspberry Pi Pico W) running Pimoroni's MicroPython build
`pico_w_inky-v1.29.0` (inky-frame release v1.29.0). Reflash steps are in
[operations.md](operations.md#recovery).

**MemoryError after a day or two on USB power.**
*Symptom:* the error screen reads `MemoryError('memory allocation failed,
allocating 48156 bytes')`; the renderer log shows each poster downloaded and
never drawn. *Cause:* `pngdec.PNG()` needs one contiguous ~48 KB block. On
USB the board never resets, the heap fragments, and a decoder created per
draw eventually can't get its block. Battery wakes start from a clean heap,
so this is USB-only. *Fix (in `almanac_frame.py`):* one decoder per boot,
preallocated at startup (`png_decoder`); a `MemoryError` more than 10
minutes after boot resets the board (the reboot redraws), while one sooner
than that is an ordinary failure so it can't reset-loop; and after 24 hours
of uptime the next wake starts with a reset. Don't create big objects per
cycle on the frame.

**Buttons go deaf after about 12 days.** *Cause:* Pimoroni's `Button.read()`
debounces with plain `ticks_ms` subtraction, which breaks when `ticks_ms`
wraps (~12.4 days). *Fix:* `rest()` does its own rising-edge detection on
`button.raw()`, and a button already held when `rest()` starts doesn't count.
Use `raw()` and `ticks_diff`, never `read()` in a long-running loop. (`read()`
is still used once at boot to see which button woke the board.)

**The hardware watchdog can't cover a refresh.** The RP2040 WDT tops out at
about 8.3 s; a panel refresh takes ~40 s. The frame uses a one-shot
`machine.Timer` as a 5-minute cycle watchdog instead. **Keep a reference to
any Timer**: rp2 deinitialises a garbage-collected Timer. A security review
caught the guard's trial timer going that way, which would have quietly
disabled it; `main.py` keeps it in the module-level `_trial_timer`.

**Filesystems differ.** The SD card is FAT: `rename()` onto an existing file
fails, so log rotation removes `almanac.log.1` first. The internal flash is
LittleFS: `rename()` replaces atomically, which the OTA swap relies on. Don't
add a `remove()` before a LittleFS rename; it opens a power-cut window.

**The RTC can read year 2000.** Before the first NTP sync, `localtime()` is
in 2000; the frame won't start its 6-hour error timer until the year is
plausible (`_failed`).

**Wi-Fi sometimes won't join straight after a reset.** Two of four boots
once sat in "connecting" or "link fail" for the whole 30 s. `connect_wifi`
power-cycles the interface and tries once more. Separately, a link left idle
for most of an hour on USB may look connected while it isn't; Wi-Fi is
switched off after every cycle so each wake starts fresh.

**A replug that looked like it did nothing.** The "unchanged poster, skip
the redraw" check used to swallow power-on. Any boot that isn't a scheduled
RTC wake now forces a redraw.

**mpremote stops the app.** Any `mpremote` command interrupts the running
program, so the frame stops updating until it is reset. Always finish with
`mpremote ... reset` (or `deploy-frame.sh ... --reset`). The deploy script
chains commands with `resume` so mpremote doesn't soft-reset between them.
When writing a file with `mpremote exec`, close it explicitly
(`f.close()`): an unclosed file once left an empty `/ota.json` on the board.

**Hold E at power-on for safe mode.** The app doesn't start, so `mpremote`
or Thonny can connect cleanly.

**Back up the stock filesystem first.** Pimoroni's stock files hold the
Wi-Fi details the board was set up with. `mpremote fs cp -r :. ./stock-fs`
before the first install, and keep the copy private.

## Networking and deploy

**The frame lives on an isolated IoT network.** On the operator's network it
is on its own VLAN (VLAN 40), and a firewall rule allows exactly one flow:
the frame's address to the renderer's address, TCP 8077. Nothing needs to
reach the frame. The rule is per address, so **moving the renderer to
another host needs a new rule**, and `BIND` in `deploy.env` must publish
the container on the allowed address.

**Prefer the renderer's IP on the frame; use a name only if you own its record.**
The renderer host's internal DNS name once started resolving to a public CDN
address (a wildcard record under a public zone), and every refresh failed with
`OSError(103)`. An IP can't do that, so `SERVER` in `frame/config.py` was an IP.
A name is fine when it is a local A record you control (the renderer on a
cluster sits behind a load-balancer address that a name hides): until that
record exists the name falls through to the public wildcard, so the fetch fails
and the frame retries on its normal back-off. The same fault broke ssh by name;
deploys connect to `$HOST` by IP with `SSH_HOST_KEY_ALIAS` set, so ssh still
checks the host key pinned under the DNS name (`-o HostKeyAlias=... -o
StrictHostKeyChecking=yes`).

**A cluster pod's cache is an `emptyDir`: normals refetch on every start.**
The ERA5 normals (a 30-year pull) rebuild in the background after a restart,
and the poster uses its placeholder until they finish; radar mode resets to
off. Nothing is lost that the pod can't rebuild, but expect a slow first
hour and don't read it as a fault. A durable cache (object storage) is the
planned fix.

**A startup probe needs minutes, not seconds.** The server warms every feed
cache before it listens (up to about a minute on a cold cache), so until then
`/healthz` refuses connections. A liveness probe that starts at once kills
the pod in a loop. Give `/healthz` a startup probe with at least two minutes
of budget and let liveness and readiness begin after it. The image's own
`HEALTHCHECK` (docker only) polls every five minutes for the same reason.

**An unset location renders Sydney, silently.** Without `server.env` the
container uses the central-Sydney defaults; this happened after the move to
the clean repo. If the poster shows the wrong town, check `server.env` made
it to the host.

## Deploy and OTA

**Don't re-sign unchanged frame code.** Every signed release gets a new
version (the signing time), and the frame installs anything newer. Deploying
with `OTA_KEY_FILE` set when `frame/` hasn't changed makes the frame
download the same code, reboot and trial-draw: an extra download and a
second flash for nothing. Deploy with `OTA_KEY_FILE=` and leave
`server/ota/manifest.json` as it is.

**"Two refreshes in a row" after a deploy was the OTA install.** The
sequence is an hourly draw, the OTA download, a reboot, and the forced trial
draw of the same poster. That is by design (the trial must prove it can
draw). Expect it after every frame release, and avoid it for server-only
changes by not signing.

**Confirm an OTA from the renderer log, not the board.** With the board on
the wall, `/ota.json` can't be read. Read the renderer's access log
(`docker logs inky-almanac` on `$HOST`): one GET of `/ota/almanac_frame.py`,
then a `/frame.json` and `/frame.png` within seconds (the trial draw), then
polls continuing **with no second download**. A failed trial rolls back,
and the old code would download the release again, so no repeat download
means the trial committed. It's an inference; say so when you report it.

**Never let the key reach argv, the renderer or git.** The OTA key was
rotated once after it appeared in a command line (a `git grep` for it).
Scan for secrets by reading values from a file (`grep -F -f`), never by
typing them. `deploy-frame.sh --ota-key` works only with `--local`, so the
key never lands on the renderer's host.

**`sign_ota.py` refuses uncommitted frame code.** Commit first; the release
should be a reviewed state. (Its docstring's message format omits the
version; the code, which includes it, is right.)

## Data sources

**Open-Meteo is not a nowcast.** Its `current` block and `minutely_15` data
are model output; outside Europe and North America `minutely_15` is
interpolated from hourly. The underlying models update every 1–6 hours, so
re-fetching every 10 minutes gains nothing. Use station observations for
"now" and Open-Meteo for the hours ahead.

**Open-Meteo's gusts run high.** In one sample its gust was 43 km/h while
nearby BOM stations reported 22–28. A grid-cell model gust is not a point
gust; don't compare them directly.

**ACCESS-G is dead.** Open-Meteo's BOM model (`bom_access_global`) has had
no updates since June 2025. Don't select it.

**The ERA5 archive labels a whole response with one UTC offset**, the one in
force when the request is made. A summer pull labels winter hours an hour
late. Convert every timestamp through UTC (`providers/climate.py`
`to_local`); a recorded fixture pins it.

**ERA5 drizzles.** As a 25 km reanalysis it reports light rain far too
often. Count an hour as wet from 0.1 mm. It also smooths hills-versus-coast
differences and under-counts and mistimes southerly changes; treat its
southerly rate as relative.

**ARPANSA's UV CSVs are in standard time all year** (1440 rows a day; the
summer peak sits near 12:05). Convert to clock time.

**BOM station 94764 is a daily manual station.** It reports once, at 9 am;
it's useless for "now". Use the state 10-minute XML (`IDN60920.xml` for NSW),
which carries every station's latest reading. Parse elements by their `type`
attribute; any may be missing (wind-only stations).

**BOM's web pages refuse automated fetchers, and HEAD gets 403.** Use GET on
the FTP and `/fwo/` products. Don't scrape bom.gov.au pages (BOM's copyright
notice prohibits it).

**`api.weather.bom.gov.au` is off limits.** It's undocumented and every
response says "You must not use, copy or share it".

**BOM's anonymous feeds are for personal, non-commercial use.** Keep the
poster and status page private; publishing needs BOM's paid Registered User
services. Poll no faster than each product's cadence (10 minutes for
observations, about 5 for radar, hourly-ish for forecasts and warnings).

**BOM's town forecast drops today's minimum after the morning**, and the
afternoon issue can drop the max and swap the précis. Anchor only the end it
gives (`anchor.py`), and keep the first-seen max, précis and icon per day
(`bom_precis._keep_first_seen`) so the poster doesn't change mid-afternoon.

**Radar product numbers.** `IDR714` is the Sydney (Terrey Hills) 64 km view
at 0.25 km per pixel (rings at 80/160/240 px are 20/40/60 km); `IDR713` is
128 km at 0.5 km/px. Rain frames and the map layers
(`radar_transparencies/`) are separate files, so no subtraction is needed.
Palette indices vary between frames: match rain colours by RGB. List only
`IDR714.T.*` (the server globs NLST); the full listing is about 1 MB.

**Radar clutter looks like rain.** On a clear night, ground clutter west of
Sydney paints thousands of pixels of BOM's faintest level, plus scattered
specks of other levels up to 76 pixels, which once kept radar mode on a
5-minute cadence and stopped the no-rain exit. Decisions count only levels
2 and up in connected clusters of at least 160 pixels (`radar.py`); the
poster still draws everything. Two clear-night frames are recorded as
fixtures alongside the 16 Nov rain frame.

**RFS's "today" is relative to the fetch.** After an offline night, the
cached feed's "today" is yesterday's. `rfs_fire_danger.py` dates the feed
by when it was fetched.

**Commons rejects generic User-Agents** with 429s. Send a descriptive agent
with a contact URL (`ALMANAC_USER_AGENT`), batch slowly, and treat a 429 or
403 as "try later", never as "photo gone".

## Rendering

**The now-marker swallowed a digit.** Its black dashes ran through the "3" of
a "34%" label, which read as "4%". The marker now picks its ink per pixel:
black over paper, white over a bar.

**Labels collide; nudge before dropping.** A chart label that would hit
another label or the marker moves half a bar, then a whole bar, to whichever
side is free, and is dropped only when boxed in. Temperature labels are
never dropped.

**DejaVu is wide, and "Wednesday" is the worst case.** The header steps the
day font down (Bold 30 → … → Condensed Bold 20) and shrinks the icon until
it fits. Mockup fonts (Roboto and friends in the Panel Lab) are much
narrower stand-ins: a layout that fits in the mockup can clip in DejaVu.
Always check the real render.

**Two panel widths.** The text panel is 270 px beside a photo and 322 px
beside a warning map (448 px wide). Test both; the worst-case fixtures do.

**Nothing on the poster may drift for no reason.** A clock, an exact AQI or
an "N h old" stamp changes the hash every hour and costs a 40 s refresh.
Use coarse buckets or categories.

**The images in `docs/img` are generated.** Regenerate them with
`tests/render_stills.py` (see [development.md](development.md#previewing-posters));
never edit them by hand.

## Testing and process

**Prove a test can fail.** Every behaviour fix comes with a test that was
seen to fail with the fix reverted. Mutation passes (one deliberate break
at a time, full suite each time) found real gaps on every feature PR:
roughly 1 in 10 mutants survived the first pass. The runner isn't in the
repo; the method is to copy `server/` to a scratch dir, apply one textual
replacement, run the test image against it with `--network none`, and
record which tests fail.

**Weak tests let mutants survive.** Seen here: fixtures that were already in
the order the code was meant to produce; a no-network test whose error was
swallowed (a tripwire that could never trip); a helper-only test that never
reached the real path; a cache-less test that never reached the BOM branch;
a download-cap test passing because a later size check also raised. Ask
what *else* would satisfy the assertion.

**Worst cases, not typical days.** A layout that fits on a mild day clips on
a bad one. The fixtures include a 46° Wednesday with hail, a 148 km/h change
and 248 mm, long text, both panel widths, and a snow day. Add a fixture
before changing the layout.

**A stale `.pyc` gave a misleading traceback.** After a quick edit, the
traceback pointed at code that no longer existed. Delete `__pycache__` and
run with `PYTHONDONTWRITEBYTECODE=1` when testing in a mounted tree.

**Secret-scan every diff against the real values.** An "arbitrary" test
latitude once matched the configured home latitude to one decimal place, and
a public district name from the real config turned up in fixtures. Scan the
diff and commit messages against every value in the gitignored
`server.env`, `deploy.env` and `frame/config.py` (reading them from the
files, never printing them), plus private-IP, coordinate and key patterns.
Fixtures and stills use central Sydney.

**A delegated report is a claim.** Briefs and reports have been wrong in
both directions: a brief assumed the alert id already told a new warning
from a reissue (it didn't: it includes BOM's issue time); another diagnosed
a header-versus-chart mismatch in the wrong place. A research agent tried
to scan a range of BOM station IDs and was stopped; its station list came
from the feed instead. Read the diff, run the command, and check against
the code before relaying anything.

## Git and GitHub

**`gh pr merge --match-head-commit` needs a full 40-character SHA**; a short
one is refused ("Could not coerce value").

**This repo merges with merge commits.** A stacked PR's diff therefore
includes its base until the base merges, then shrinks to its own changes;
say so in the PR body. A PR whose head is already in main (because a later
PR containing it merged first) closes as merged by itself.

**zsh aborts a whole command on an unmatched glob.** `ls /dev/cu.usbmodem*`
with nothing plugged in kills the command line in zsh, not just that word.
Quote or guard globs in one-liners.

**`gh repo create` and similar can be blocked by an agent harness** until
the operator approves in chat. Ask; don't route around it.
