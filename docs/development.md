# Development

Setting up, running the tests, previewing posters, deploying, configuring the
renderer, and adding a data source. The code map is in
[architecture.md](architecture.md); the traps are in [gotchas.md](gotchas.md).

## Setup

```bash
mise install                                   # Python, mpremote and pytest (see mise.toml)
cp frame/config.py.example frame/config.py     # then set SERVER
cp deploy.env.example deploy.env               # then set HOST, BIND, ...
cp server.env.example server.env               # your location (ALMANAC_* settings)
scripts/sign_ota.py --new-key ~/.config/inky-almanac/ota-key.txt   # once, for OTA
```

Server tests run in docker, because rendering needs the DejaVu fonts that ship
in the image:

```bash
docker build -f server/Dockerfile.test -t inky-almanac-test server && docker run --rm inky-almanac-test
```

Frame tests run under CPython, with the Pico's modules stubbed:

```bash
mise run frame-test
```

Every behaviour fix in this repo comes with a test that was seen to fail
without the fix. Keep it that way: write the test, revert the fix, watch it
fail, restore.

## Previewing posters

Every image in `docs/img` comes from `server/tests/render_stills.py` and the
committed fixtures, at central Sydney:

- `panel-*.png`: the side-panel cases the tests render (worst cases in photo
  and map mode, a normal day, a quiet morning, an evening, a southerly),
  beside a synthetic landscape;
- `poster-*.png`: the README's full posters, with a real Commons photo
  (`server/tests/fixtures/photos/`, "Dülmen, Umland" by Dietmar Rabich,
  CC BY-SA 4.0, committed already dithered), an unlabelled synthetic warning
  map, and the recorded 16 Nov 2026 BOM radar frame;
- `behaviour-*.png`: crops of those for `docs/behaviours.md` (`CROPS` in the
  script).

The presets live in `server/tests/panel_fixtures.py`. Regenerate the images
rather than edit them (Pillow writes them without metadata), and look at
every one that changed:

```bash
docker run --rm -v "$PWD/server:/app" -v "$PWD/docs/img:/out" inky-almanac-test python tests/render_stills.py /out
```

The renderer's status page, `http://<renderer>:8077/`, shows the current
poster, the photo, the conditions, and when the frame last checked in.

- `/preview.png` is the poster in estimated ink colours.
- Add `?at=2026-11-10T19:30` to render any moment, for example to check the
  evening view.
- `/frame.png` is what the frame actually downloads, an indexed PNG.

## Deploying

| What changed | Run |
|---|---|
| Server code, or the frame app (`almanac_frame.py`) | `scripts/deploy-server.sh`. Runs the tests, rebuilds and restarts the container. It also signs `almanac_frame.py`, which the frame installs over the air after its next refresh. |
| `main.py`, `config.py`, WiFi or OTA key | `scripts/deploy-frame.sh --local --reset` with the frame on this machine's USB. |

Any `mpremote` command stops the app on the frame, so always finish with a
reset (`--reset`).

`deploy-server.sh` needs `deploy.env` (`HOST`, `BIND`, optionally
`SSH_HOST_KEY_ALIAS`): it rsyncs `server/` and `server.env` to `HOST` over
ssh, runs the test image there, then rebuilds and restarts the container and
waits for `/healthz`.

**Only sign when `frame/` changed.** With `OTA_KEY_FILE` set, every run signs
a new release with a new version number, so the frame downloads it, reboots
and does a trial draw: a second panel flash straight after the deploy's own,
for code that didn't change. When `frame/almanac_frame.py` is unchanged since
the last release, deploy with `OTA_KEY_FILE=` (empty) so the existing
`server/ota/manifest.json` stays. The OTA design is in
[operations.md](operations.md#over-the-air-updates) and
[design.md](design.md#12-signed-over-the-air-updates).

To run the renderer locally without a host to deploy to:

```bash
docker build -t inky-almanac:dev server
docker run -d --name inky-almanac -p 8077:8077 --env-file server.env \
  -v inky-almanac-cache:/var/cache/almanac inky-almanac:dev
```

Add `-e ALMANAC_PHOTOS=none -e ALMANAC_NORMALS=placeholder` to skip the
Commons crawl and the 30-request ERA5 normals build while you iterate.

## Configuration

Set these in `server.env` (gitignored; `deploy-server.sh` passes it to the
container). The defaults live in `server/almanac/config.py`.

| Variable | Default |
|---|---|
| `ALMANAC_PLACE`, `ALMANAC_LAT`, `ALMANAC_LON`, `ALMANAC_TZ` | Sydney, -33.87, 151.21, Australia/Sydney |
| `ALMANAC_FIRST_HOUR`, `ALMANAC_LAST_HOUR` | 6, 22 (refresh window, inclusive) |
| `ALMANAC_TOMORROW_FROM` | 18 |
| `ALMANAC_HIGH_LEADS_FROM` | 9 (before this hour the header leads with the overnight low) |
| `ALMANAC_BOM_TOWN` | Sydney (a town name from BOM's IDN11060 forecast product) |
| `ALMANAC_HAZARD_RADIUS_KM` | 30 |
| `ALMANAC_BOM_AREAS`, `ALMANAC_BOM_IGNORE` | which BOM warning titles count (comma separated) |
| `ALMANAC_FIRE_DISTRICT` | Greater Sydney Region (empty turns the fire badge off) |
| `ALMANAC_USB_DEVICE` | empty (USB mode off) |
| `ALMANAC_FORECAST`, `ALMANAC_OUTLOOK`, `ALMANAC_OBSERVATIONS` | openmeteo, bom, bom (which provider supplies each kind of data; `none` turns a kind off) |
| `ALMANAC_HAZARDS` | rfs,hazardwatch,bom |
| `ALMANAC_WARNING_MAPS`, `ALMANAC_FIRE_DANGER`, `ALMANAC_AIR_QUALITY`, `ALMANAC_PHOTOS`, `ALMANAC_NORMALS` | bom, rfs, openmeteo, commons, era5 (`placeholder` for a fixed table) |
| `ALMANAC_OBS_STATION` | 94760 (Horsley Park AWS; a WMO id from BOM's state observations product) |
| `ALMANAC_OBS_CROSSCHECK` | 95765,94765 (Sydney Olympic Park, Bankstown: stand in when the home station is silent) |
| `ALMANAC_OBS_LEADING` | 95756,94767,94769,94768 (Kurnell, Sydney Airport, Fort Denison, Observatory Hill: where a southerly arrives first) |
| `ALMANAC_OBS_PRODUCT` | IDN60920 (BOM's NSW 10-minute observations; other states have their own) |
| `ALMANAC_RADAR`, `ALMANAC_RADAR_PRODUCT` | bom, IDR714 (radar mode's source and product: Sydney, Terrey Hills, 64 km; `none` turns radar mode off: C then just redraws) |

The photo categories are `CATEGORIES` in `providers/commons.py`. The pool refreshes every
14 days; to force a refresh, delete `photo-pool.json` from the cache volume.

## Adding or swapping a provider

Every external source sits behind an adapter in `server/almanac/providers/`,
one module per source. The scorer, the layout and the scheduler only ever see
plain domain data (`domain.py`, `hazards.Hazard`), so a fork for another
state or country replaces adapters and leaves the rest alone.

| Kind | Interface (`providers/base.py`) | Shipped adapters | Setting |
|---|---|---|---|
| forecast | `ForecastProvider.forecast()` → `Forecast` | `openmeteo` | `ALMANAC_FORECAST` |
| official daily outlook | `OutlookProvider.outlook()` → `{date: DayOutlook}` | `bom` (IDN11060 town forecasts) | `ALMANAC_OUTLOOK` |
| station observations | `ObservationsProvider.refresh()`, `.logs()` → `StationLogs` | `bom` (state 10-minute observations) | `ALMANAC_OBSERVATIONS` |
| hazards | `HazardProvider.hazards()` → `[Hazard]` or None | `rfs`, `hazardwatch`, `bom` | `ALMANAC_HAZARDS` (several) |
| warning maps | `WarningMapProvider.warning_map()` → `MapImage` | `bom` | `ALMANAC_WARNING_MAPS` |
| fire danger | `FireDangerProvider.fire_danger()` → `FireDanger` | `rfs` | `ALMANAC_FIRE_DANGER` |
| air quality | `AirQualityProvider.air_quality()` → `[AirQualityHour]` | `openmeteo` | `ALMANAC_AIR_QUALITY` |
| photos | `PhotoProvider.photo_for()` → `Photo` | `commons` | `ALMANAC_PHOTOS` |
| rain radar | `RadarProvider.refresh()`, `.latest()` → `RadarImage` | `bom` (radar frames over anonymous FTP) | `ALMANAC_RADAR` |
| climate normals | `normals.Normals` (`normal_max`, `normal_uv`) | `era5` (+ ARPANSA UV), `placeholder` | `ALMANAC_NORMALS` |

Sun and moon are not providers: sunrise and sunset arrive with the forecast,
and the moon phase is computed locally (`astro.py`).

To add one:

1. Write `providers/<source>.py` with a class constructed as
   `Adapter(cfg, get_text=None)` that satisfies the interface. Turn the
   source's format into domain types there and nowhere else: units as
   `domain.py` says (°C, km/h, mm, degrees the wind blows from), weather as
   WMO codes, a short `source` credit for the poster.
2. Fetch through `get_text(url)` and `providers.cache.cached(...)`. Give it a
   `check=` that rejects junk, so a bad response never replaces a good cache,
   and a `stale_after=` that suits how fast the source changes. On the
   request path `get_text` is `cache.offline`, so requests only ever read
   caches; the background refresher in `app.py` does the fetching.
3. Parse each item on its own, so one bad item can't hide the rest, and
   return None or empty rather than raise when the source is down.
4. Register it in `REGISTRY` in `providers/__init__.py` and select it with
   the setting. Test it against a recorded fixture: no network in tests.

`tests/test_boundaries.py` fails if a module outside `providers/` imports a
concrete adapter (only `app.py` composes, through the registry), or carries a
provider's name or field names in its code.

Known coupling left: the warning map's ink table (`dither.map_ink`) is tuned
to BOM's map colours; a map provider with a different palette would want its
own table or to deliver ink-ready images.

To add a badge, add it in `render.badges_for`. Use the inks that mean
something (`palette.py`), and keep the text free of numbers that drift. Every
change to the picture costs a 40-second refresh.
