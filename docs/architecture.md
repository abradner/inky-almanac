# Architecture: a map of the codebase

How a poster gets from a dozen public feeds onto the panel, and where each
piece lives. *Why* it is built this way is in [design.md](design.md); the
traps are in [gotchas.md](gotchas.md).

## The big picture

```
 public feeds                renderer (Python, docker, :8077)                       Inky Frame 7.3" (Pico W)
 ────────────                ───────────────────────────────────────                ────────────────────────
 Open-Meteo          ┐       background threads (the ONLY network path)             main.py  boot guard
 BOM FTP / RSS       │  ──►    feeds 5 min · warnings 90 s · obs 60 s     disk       (never updated over the air)
 RFS, HazardWatch    │         photo pool 6 h · radar 60 s (radar mode)  cache        │
 Wikimedia Commons   │         ERA5 normals (once, then yearly)           │           ▼
 Open-Meteo ERA5     ┘                                                     │        almanac_frame.py
                             request path (cache-only)  ◄──────────────────┘          wake ─► GET /frame.json
                               providers ─► domain types ─► facts/anchor/observed      │ hash changed? GET /frame.png?h=
                               ─► render.py ─► 4-bit PNG, indices = inks  ──────────►  │ draw (PNG_COPY), maybe OTA
                                                                                       │ sleep: battery = power off
                             GET /alert.json  ◄──────────── every 2 min on USB power ─ │        USB = buttons + alert poll
```

Two rules hold everything together:

1. **Requests never touch the network.** The request path builds its
   providers with `get_text=cache.offline`, so they only read the disk
   caches the background threads keep warm. A dead feed can't make the
   frame's 60-second poll time out.
2. **Business logic sees only domain types.** Every external source is an
   adapter under `server/almanac/providers/`; scoring, layout and
   scheduling read `domain.py` and `hazards.Hazard`. `tests/test_boundaries.py`
   enforces it.

## HTTP surface (`server/almanac/app.py`)

| Route | Who calls it | What it returns |
|---|---|---|
| `GET /frame.json` | the frame, each wake | `hash`, `size`, `png` (`/frame.png?h=<hash>`), `sleep_minutes`, `day`, `tomorrow`, `photo`, `alert`, `alert_poll_minutes` (2), `mode`, `server_time`, and `code` (the signed OTA manifest) when a release exists. Records the frame's last check-in. |
| `GET /frame.png?h=<hash>` | the frame | exactly the bytes that hash described (a few recent renders are kept); 404 if evicted, and the frame re-reads `/frame.json` |
| `GET /alert.json` | the frame, every 2 min on USB | `{"id": ...}`; in radar mode also `mode` and `min_gap_s` |
| `GET /ota/almanac_frame.py` | the frame, when `code` is newer | the signed release (404 if none) |
| `POST /mode?set=radar\|normal` | button C, status page | enters or leaves radar mode (409 if no radar provider); waits up to 20 s for a first radar frame |
| `POST /shuffle` | button B, status page | a different photo for the rest of the day |
| `GET /preview.png` | a browser | the poster in estimated ink colours |
| `GET /` | a browser | status page: preview, `frame_info`, conditions, when the frame last checked in, the two buttons |
| `GET /healthz` | docker, deploy script | `ok` |

Any GET takes `?at=2026-11-10T19:30` to render another moment. POSTs refuse
a cross-origin `Origin`.

## The poster cache and the alert id

`Almanac.poster(now)` caches renders under the key
`(hour slot, poster day, shuffle count, alert id)`, keeping the last six.
Because the alert id is in the key, anything that changes it mid-hour makes
a new poster at once.

The alert id is up to three parts joined with `+` (`Almanac._alert`):

| Part | From | Present when |
|---|---|---|
| `<12 hex>` | `hazards.alert_id`: hash of the specific hazards' identities (BOM's include the issue time) | a specific hazard is in force |
| `wx:<8 hex>` | `facts.digest` of the shown facts, held by `_fact_alert` | the top fact's score sets a re-check tier (90+/75+/50+ → 10/15/30 min) |
| `radar:<frame time>` | `radar.View.alert` | radar mode is on |

`_fact_alert` holds its answer for the tier's interval and changes it only
when `facts.worth_redrawing` says the facts differ enough from what was last
*drawn* ([design.md §4](design.md#4-refresh-behaviour)). The frame decides
when to act on a change (`alert_redraw_due`, below).

## Server modules

Pure modules take domain data and a moment and return values; they are where
most tests live.

| Module | Owns |
|---|---|
| `app.py` | HTTP handler, `Almanac` (composition root: builds providers, poster cache, alert id, radar mode), background threads in `main()` |
| `config.py` | every `ALMANAC_*` setting and its default (central Sydney) |
| `domain.py` | the domain types and `apply_outlook` |
| `hazards.py` | `Hazard`, ranking, `alert_id`, dedupe, distance to a polygon edge |
| `conditions.py` | gathers fire danger, air quality, hazards and the warning map into one `Conditions` |
| `facts.py` | the coming hour's facts, their scores, re-check tiers, the defer rule, the digest (pure) |
| `anchor.py` | rescales the model's hourly temperatures to the official max and min (pure) |
| `observed.py` | rain since 9 am and last hour, southerly detection, freshness, from station logs (pure) |
| `normals.py` | the `Normals` interface (`normal_max`, `normal_uv`) and a placeholder table |
| `schedule.py` | next wake (HH:01 within the refresh window, computed in UTC), poster day (tomorrow from 18:00), hour slot |
| `radar.py` | radar mode's rules: what counts as rain (not clutter), entering, leaving, rain near home, `min_gap_s`, the roughly-same hold, reissue-proof hazard keys |
| `render.py` | the poster: photo or map or radar on the left, the side panel, chart, badges, caption |
| `radar_poster.py` | a radar frame drawn in fixed inks and stipples |
| `dither.py` | Atkinson dithering in CIELAB; `map_ink`, the fixed ink table for BOM warning maps |
| `palette.py` | the seven inks (pen order and estimated RGB), preview conversion, the photo colour screen |
| `icons.py` | weather and moon glyphs in solid inks |
| `astro.py` | moon phase, computed locally |
| `usbpush.py` | USB mode: render on the frame's schedule and push changed posters over serial with `mpremote` |

## Providers (`server/almanac/providers/`)

Each adapter is `Adapter(cfg, get_text=None)`, returns domain types, and
returns None or empty instead of raising when its source is down. The
registry (`providers/__init__.py`) maps each `ALMANAC_*` setting to a class;
`none` turns a kind off. The interface table and the recipe for adding one
are in [development.md](development.md#adding-or-swapping-a-provider).

| Module | Kind | Source |
|---|---|---|
| `base.py` | | the Protocols; `CachedHazardFeed` (fetch, check, cache, parse; stale after 90 min) |
| `cache.py` | | `get_text`, `offline`, and `cached()`: a disk cache that never lets a failed `check` replace good data |
| `openmeteo.py` | forecast | Open-Meteo hourly and daily (20 min TTL, shortened while a tier is active) |
| `bom_precis.py` | outlook | BOM town forecast `IDN11060.xml`; keeps the first-seen max, précis and icon per day |
| `bom_obs.py` | observations | BOM state 10-minute XML `IDN60920.xml`; a three-hour rolling log |
| `rfs_incidents.py` | hazards | RFS major incidents GeoJSON |
| `hazardwatch.py` | hazards | SES warnings from HazardWatch's embedded page data |
| `bom_warnings.py` | hazards, warning maps | BOM NSW warnings RSS; maps from the embedded PNG in warning pages |
| `rfs_fire_danger.py` | fire danger | RFS ratings and Total Fire Ban, dated by fetch time |
| `openmeteo_air.py` | air quality | Open-Meteo US AQI forecast |
| `commons.py` | photos | Wikimedia Commons Featured Pictures pool, screened and credited |
| `bom_radar.py` | radar | BOM radar frames and transparency layers over FTP |
| `climate.py` | normals | ERA5 hourly normals built at runtime, plus the committed ARPANSA UV table |

## Domain types (`domain.py`, `hazards.py`)

| Type | Holds |
|---|---|
| `Forecast` | `days` (`{date: Day}`), `current_temp`, `fetched_at`, `source` |
| `Day` | daily code, max, min, rain, UV, sun times, `hours`, plus the outlook's overrides (`label_override`, `icon_override`, `rain_text`, `rain_chance`) |
| `Hour` | hour, temp, rain chance and mm, WMO code, wind, gust, UV, wind direction |
| `DayOutlook` | an official day: max, min, précis, rain chance and range, icon |
| `Reading`, `StationLogs` | one station observation; successive readings per station with `home`, `crosscheck`, `leading` |
| `MapImage` | a warning map's PNG, title, area, issue time, credit |
| `RadarImage` | a radar frame as rain levels plus base layers and geometry |
| `FireDanger`, `AirQualityHour`, `Photo` | as named |
| `Hazard` | kind, AWS level, where, km, `specific`, `ident`, `url`, `detail`, `titled` |

Units are fixed: °C, km/h, mm, degrees the wind blows *from*, WMO weather
codes. `source` fields are poster credits only; no logic branches on them.

## The frame (`frame/`)

`main.py` is the boot guard ([design.md §12](design.md#12-signed-over-the-air-updates)):
safe mode if button E is held; nothing at all in USB mode; otherwise
recover any interrupted OTA, count a trial boot, arm the trial timer, then
import `almanac_frame` and call `run()`.

`almanac_frame.main()`:

1. Copy the external RTC to the Pico's clock, mount the SD card, load
   `/state.json`, and preallocate the one PNG decoder while the heap is
   clean.
2. Work out why it woke. A button held at wake counts as a press. Anything
   but a scheduled RTC wake (plugging in, a reset, A) forces a redraw, as
   does `force_next` left by an OTA install or a fresh-heap restart.
3. `cycle()`: busy LED on, a 5-minute one-shot watchdog timer, join Wi-Fi
   (two tries), sync the clock daily, POST `/shuffle` or `/mode` for B or C,
   GET `/frame.json`, and if the hash changed (or forced) download the PNG
   to the SD card (size-capped) and draw it. A draw commits any OTA trial;
   then `maybe_update` may install a newer signed release and reset.
   Wi-Fi is switched off at the end of every cycle.
4. Failures back off 5, 10, 20, then 30 minutes; after 6 hours of failure
   an error screen replaces the poster. A `MemoryError` more than 10
   minutes after boot resets the board instead.
5. `rest()`: arm the RTC alarm and drop the power latch. On battery the
   board is now off. On USB power it is still running, so it watches A, B
   and C with its own edge detection on `raw()` and polls `/alert.json`
   every `alert_poll_minutes` (not while cycles are failing). It returns
   early for a press or for an alert change that `alert_redraw_due` allows:
   at once for a first alert, an all-clear or a mode change; otherwise no
   sooner than 10 minutes (or radar mode's `min_gap_s`, clamped to 4–10)
   after the last alert redraw.
6. After 24 hours of uptime the next wake starts with a reset, so a
   USB-powered frame begins each day on a fresh heap.

`run()` wraps it all: any uncaught error is logged, then the board waits 30
minutes and resets. The board's log is `almanac.log` on the SD card (24 KB,
one rotation).

## Deploying

| Script | Does |
|---|---|
| `scripts/deploy-server.sh [--usb]` | signs the frame code if `OTA_KEY_FILE` is set, rsyncs `server/` and `server.env` to `HOST`, runs the test image there, rebuilds and restarts the `inky-almanac` container (cache volume `inky-almanac-cache`, published on `BIND:8077`), waits for `/healthz` |
| `.github/workflows/docker.yml` | tests, then publishes the generic multi-arch image to `ghcr.io/<owner>/inky-almanac` (sha tag on `main`, plus the tag name on `v*`) |
| `scripts/release-ota.sh [KEY_FILE]` | signs a release with `sign_ota.py`, checks it, and prints the commands that load it into the cluster's `inky-almanac-ota` 1Password item |
| `scripts/sign_ota.py [KEY_FILE]` | writes `server/ota/almanac_frame.py` and `manifest.json` (gitignored); `--new-key PATH` makes a key; refuses uncommitted frame code |
| `scripts/deploy-frame.sh --local [PORT] ... --reset` | copies `main.py`, `almanac_frame.py`, `config.py` (and optionally `WIFI_CONFIG.py`, `OTA_KEY.py`) over USB, clears any OTA trial, resets the OTA baseline; without `--local` it runs `mpremote` on `HOST` |
| `tools/build_uv_normals.py` | rebuilds the committed ARPANSA UV table (~400 MB of downloads) |

On a cluster the renderer is the same image with its state moved out: settings
arrive as `ALMANAC_*` environment variables, the signed frame release is
mounted read-only at `/app/ota` (the code reads `ota/` next to `almanac/`, so
the mount replaces whatever the image carries), and the only writable path is
the cache directory (`ALMANAC_CACHE`, default `/var/cache/almanac`), which
holds every file the server writes. See
[operations.md](operations.md#running-on-a-cluster).

## Tests

| Suite | Run | Notes |
|---|---|---|
| Server | `docker build -f server/Dockerfile.test -t inky-almanac-test server && docker run --rm inky-almanac-test` | in docker because layout needs the DejaVu fonts the image installs. No network: providers are tested against recorded fixtures in `server/tests/fixtures/`. |
| Frame | `mise run frame-test` | CPython with the Pico modules stubbed (`frame/tests/conftest.py`): control flow, buttons, retries, OTA and the guard. Drawing and networking are verified only on the board. |
| Stills | `docker run --rm -v "$PWD/server:/app" -v "$PWD/docs/img:/out" inky-almanac-test python tests/render_stills.py /out` | regenerates `docs/img/panel-*.png` and `poster-radar*.png`; never hand-edit them |

Worth knowing about the suites:

- `server/tests/panel_fixtures.py` holds the side-panel presets, including
  the worst cases (`worstPhoto`, `worstMap`, `worstCold`: a Wednesday at
  46°, hail, a 148 km/h change, 248 mm, long text; a −4° snow day).
  `test_panel.py` renders each one in photo and map mode and asserts no band
  overflows the panel or runs into the caption.
- `test_boundaries.py` walks every module outside `providers/` and fails on
  an import of a concrete adapter or a provider's name or field format in
  code, with a positive control.
- `deploy-server.sh` runs the server suite on the renderer host before it
  restarts the container.
- Mutation passes (one deliberate break at a time, full suite each time)
  were run by hand for every feature PR; the runner isn't in the repo.
  See [gotchas.md](gotchas.md#testing-and-process).
