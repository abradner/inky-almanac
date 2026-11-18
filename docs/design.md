# Design notes: why the poster is the way it is

This file records the reasoning behind the poster's information design, data
sources and refresh behaviour. It is here so a later change, by a person or an
agent, does not quietly undo a decision whose reason is not visible in the
code. If you change one of these behaviours, update the note and say why.

The interactive mockup the side panel was designed in ("Inky Panel Lab", a
private claude.ai artifact) has worst-case presets and a clipping detector. The
renderer's own worst-case tests (`server/tests/panel_fixtures.py`,
`test_panel.py`) are the source of truth now.

## At a glance

The short version of the choices below, for a reader in a hurry:

- **The server renders; the frame only draws.** A 4-bit indexed PNG whose
  palette indices are the panel's inks (section 11).
- **No clock on the poster.** An hourly timestamp would force a 40-second
  refresh every hour for nothing. Stale data is flagged instead.
- **Evenings describe tomorrow**: its photo and its forecast, from 6 pm.
- **The coming hour leads, ranked by how unusual it is** (sections 2 and 3),
  and a remarkable hour is re-checked between the hourly refreshes
  (section 4).
- **Observed beats official beats model** (section 5).
- **Warnings that ask for action redraw within minutes; notices wait for the
  hour** (section 15).
- **Frame code updates itself over the air, signed, with a rollback guard
  that is never updated** (section 12).
- **Nothing personal in the repo** (sections 8 and 17).

## 1. What the poster is for

- **Glanceable from across the room.** The panel is 800×480 with seven inks,
  and a full refresh takes long enough to be noticed. Every element has to
  earn its place, and redraws are rationed (section 4).
- **The coming hour leads; the day supports.** The poster refreshes hourly, so
  the most useful thing it can say is "what the next hour holds" and "where
  the day is heading". The day's forecast moved above the rule, smaller.
- **Show what is unusual, not everything.** Many facts are true about any
  hour. The panel shows the handful that are most out of the ordinary, ranked
  (section 3). An unremarkable hour gives a quiet panel.

## 2. The side panel, top to bottom

| Band | What | Rule |
|---|---|---|
| Header | Day name and date, left; forecast-day icon and one max/min pair, right | The date never moves. "Tomorrow" is a property of the forecast, not the date. |
| Forecast line | Précis text, plus the day's rain chance and amount from BOM | From 6 pm it starts "Tomorrow:" because the forecast is tomorrow's. At most two lines. |
| Rule | | |
| Hero | Coming hour's icon, hour label, big temperature; top-ranked fact, right | The icon is never squeezed; the fact may wrap. |
| Facts table | Next ≤ 4 facts | Hazard rows read "Hail · next hour", name first. |
| Chart | 6 am–9 pm rain-chance bars and temperature line | Takes whatever height is left (48–130 px). |
| Sun and moon | Rise, set, day length; moon phase | The moon is drawn as seen from the southern hemisphere (a waxing moon is lit on the left). |
| Caption | Photo title and credit, or the warnings list in map mode | Laid out bottom-up so it never collides. |

**One max/min pair, with a lead rule.** Showing the day's max and min twice
(header and hero) wasted space. The pair appears once, and the *next* extreme
is the big one, because that is where the day is heading:

- before the "high leads" hour (default 9 am, `ALMANAC_HIGH_LEADS_FROM`):
  big blue overnight low, small red "high today";
- from then until 6 pm: big red "HIGH TODAY", small blue "low tonight";
- from 6 pm: big blue "LOW TONIGHT", small red "high tomorrow".

**Widths.** The text panel is 270 px beside a photo and 322 px beside a BOM
warning map (448 px wide). Long day names ("Wednesday") step the header font
and the forecast icon down; the worst-case fixtures exercise this.

**The day's rain must stay.** The A2 port briefly lost the day-level "chance
and amount" because only the hourly bars survived. The day figure comes from
BOM's town forecast (chance of any rain, expected range), never from the
model's daily sum.

## 3. Fact scoring (`server/almanac/facts.py`)

Each candidate fact gets a score for how impactful and how far from normal it
is. The highest leads in the hero, the next four fill the table, and a score
of 0 is omitted. Principles behind the numbers:

- **Hazards always lead.** Hail 100, storms 90. Every non-hazard score is
  capped at 89, because a worst-case test showed a 148 km/h southerly change
  outranking hail. Fog (50) and "storms later" (60) can be outranked by, say,
  a very hot day.
- **Chance and depth together.** Rain reads "60% · 0.6 mm", never one without
  the other. A chance under 25% with no depth scores 0, so "0% · 0.0 mm"
  never appears.
- **"Since 9am" reads unambiguously.** The headline value is the total since
  9 am; the last hour is the small sub-text ("18 mm · 7 mm last hr"). An
  earlier layout read as the reverse.
- **Normal depends on month and hour.** UV 14 at noon in January is expected;
  UV 7 at 5 pm in July is not. Temperature and UV are compared with
  month × hour normals (section 5), not fixed thresholds.
- **A change matters more than a level.** 5 km/h of wind is not interesting.
  A swing from northerly to southerly in the next hour is. Observed changes
  beat forecast ones: a southerly seen at the coast but not yet home is
  "Southerly change arriving" (80); one that has arrived scores 50.

The exact thresholds are named constants with tests on both sides of each
boundary.

## 4. Refresh behaviour

- **Hourly poster, two-minute alert poll.** The frame redraws on the hour and
  polls `/alert.json` every 2 minutes on USB power. The poll is cheap: it
  collects data. A redraw happens only when there is an important *delta*.
- **Tiers from the top fact's score:** 90+ re-checks every 10 minutes, 75+
  every 15, 50+ every 30, otherwise hourly. **10 minutes is the floor**: a
  redraw is slow and visible, and the frame caps non-urgent alert redraws at
  one per 10 minutes anyway.
- **"Roughly the same" defers.** At a re-check the panel redraws only if,
  compared with what was last *drawn* (not the last check, so small drifts
  can't add up): a fact appeared or disappeared, the hero changed kind, a score
  rose by more than 5 (worse news), or fell by more than 10 (better news).
  Small changes in mm or km/h wait for the hourly redraw.
- **No flicker.** The frame redraws at once on a first alert *and* on a clear,
  so a raised state is held until its interval ends. A higher tier is still
  noticed at its own pace.
- **Data freshness bounds the tiers.** While a tier is active, the forecast is
  fetched as often as the tier re-checks, and station observations every 10
  minutes. Re-checking faster than the data changes is pointless.
- **Don't re-sign unchanged frame code.** Every signed OTA release has a new
  version, so the frame downloads it and does a trial draw: a second flash
  straight after the deploy's own. If `frame/` has not changed since the last
  release, deploy the server without signing (keep the existing
  `server/ota/manifest.json`).

## 5. Data sources and which one wins

**Precedence: observed (BOM station) > BOM forecast > model.** BOM is the
official forecaster: its forecasts blend models with forecaster judgement and
local knowledge (sea breezes, southerlies). Open-Meteo is raw global model
output. But BOM's *free* machine-readable forecast is daily only, so the model
still supplies the hour-by-hour shape.

| Need | Source | Notes |
|---|---|---|
| Hour-by-hour forecast | Open-Meteo, hourly | Model output. Its temperature curve is rescaled so its peak and trough match BOM's max and min; the shape (timing) comes from the model. |
| Day max/min, rain chance and range, précis | BOM town forecast `IDN11060.xml` (anonymous FTP) | Issued about 4 times a day. BOM often omits today's min after the morning; anchor only the end it gives. |
| Now, since 9am, last hour | BOM 10-minute NSW observations `IDN60920.xml` | Default station Horsley Park AWS (94760); cross-checks Sydney Olympic Park, Bankstown. Since 9am is the gauge's `rainfall`; last hour is the difference of successive readings, with the 9 am reset handled. Older than 30 min counts as missing, and the facts fall back to the model. |
| Southerly change, observed | Same feed, coastal stations leading | Kurnell, Sydney Airport, Fort Denison, Observatory Hill. |
| Warnings and maps | BOM warnings, RFS, HazardWatch | As issued. |
| Normals | ERA5 via the Open-Meteo archive, 1991–2020; ARPANSA measured UV | See below. |

Things learned the hard way:

- **Open-Meteo is not a nowcast.** Its `current` block and `minutely_15` data
  are model output; outside Europe and North America `minutely_15` is
  interpolated from hourly. The models update every 1–6 hours. Its gusts ran
  well above BOM station gusts in a sample. Its BOM model option
  (`bom_access_global`) stopped updating in June 2025. Fetching it every 10
  minutes gains nothing.
- **The manual station 94764 is not an observation feed**: it reports once a day at
  9 am. It is useless for "now".
- **BOM terms.** The anonymous FTP and `/fwo/` products are for personal,
  non-commercial use. Poll at each product's cadence (10 min for
  observations, about 5 for radar). Scraping bom.gov.au pages is prohibited,
  and the undocumented `api.weather.bom.gov.au` says not to use it. Publishing
  or commercial use needs BOM's paid Registered User services.
- **There is no free BOM API key.** Hourly history is a paid Climate Data
  Services request.

**Climate normals** (`providers/climate.py`):

- Temperature, wind and rain normals are built at runtime from ERA5 for the
  *configured* location, cached in the cache volume, and rebuilt yearly or
  when the location changes. A committed central-Sydney placeholder answers
  until then. They are never committed for the home grid cell.
- UV normals come from ARPANSA's measured Sydney UV (2007 onwards), committed
  as a small table. ERA5 has no UV, and Open-Meteo's UV only goes back to 2023.
- Open-Meteo's archive stamps a whole response with the UTC offset in force
  when the request is made, so a pull in summer labels winter hours an hour
  late. Convert every timestamp through UTC.
- ERA5 is a 25 km reanalysis: it drizzles too often (use a 0.1 mm wet-hour
  threshold), smooths Hills-vs-coast differences, and under-counts and
  mistimes southerly changes.

## 6. Providers are adapters

The project is not a product, but someone may fork it for another city or
country. Every external source sits behind an interface in
`server/almanac/providers/base.py`, and config selects the adapter. Scoring,
layout and scheduling see only domain types (`domain.py`). A test
(`tests/test_boundaries.py`) fails if business code imports a provider module
or names a provider's fields. See the README's "Adding or swapping a provider".

## 7. Chart rules

- **Rain bars are 0–100% absolute,** not scaled to the day's range. A bar of
  4% or less is not drawn.
- **Max and min temperature labels are red and blue,** outlined in white and
  drawn last, so nothing covers them.
- **Rain labels mark the wettest and driest hours,** only on drawn bars. White
  inside a bar at least 14 px tall, blue above a shorter one.
- **Nudge before dropping.** A label that would hit another label or the now
  marker moves half a bar, then a whole bar, to whichever side is free. It is
  dropped only when boxed in.
- **The now marker inverts:** black dashes over paper, white over a bar,
  chosen per pixel. Before this, the marker's dashes swallowed the "3" of a
  "34%" label, which looked like "4%".
- **No temperature gridlines.** Tried; the numeric max and min were enough
  and the dots were noise.

## 8. Privacy

- The home location, host and Wi-Fi details live only in gitignored
  `server.env`, `deploy.env` and `frame/config.py`. Never commit them or
  anything derived from them (home-cell normals, a radar crop around home).
- Tests, fixtures and stills use central Sydney. External test queries use
  Sydney Observatory Hill (−33.86, 151.21) or public station IDs. Even an
  "arbitrary" test coordinate once matched the home latitude to one decimal
  place: scan diffs against the real values before pushing.

## 9. Worst cases

A layout that looks fine on a mild day can clip on a bad one. The fixtures
include a Wednesday in late September at 46–47°, hail, a 148 km/h
northwest-to-south change, 248 mm since 9 am, a two-line forecast, a long
fire badge and photo title (photo and map modes), and a −4° snow day in June.
The panel tests assert that no band overflows `SIDE_W` or runs into the
caption, and the hazard always leads.

## 10. Radar mode, as built

Button C (or the status page) swaps the photo for BOM's Sydney rain radar.
The rules live in `server/almanac/radar.py` and are pure.

- **The mode lives on the server**, in `radar-mode.json` in the cache
  volume, so it survives a restart. The frame sends
  `POST /mode?set=radar|normal`; the target is "the other mode from the one
  last drawn", which the frame learns from `/frame.json`'s `mode`.
  *Rejected:* a `?mode=radar` query on `/frame.json`. The exit rules must
  run while the frame sleeps, and the status page needs a button too, so the
  frame can't be the source of truth.
- **It ends by itself** at whichever comes first: local midnight, or at
  least 2 hours in with the newest frame showing no rain. "Rain" means
  levels 2 and up in connected clusters of at least 160 pixels (10 km²):
  on a clear night (18 Nov) level-1 ground clutter and specks up to 76
  pixels had kept the mode open and the cadence fast. The poster still
  draws every level. A stale (over 20 minutes)
  or missing frame never ends it early. Entering at 23:00 gives an hour.
- **A new warning ends it**, so the warning's map shows. "New" means a
  specific hazard whose `(kind, level, where)` wasn't in force while radar
  mode was on. The alert id couldn't be reused for this: it includes BOM's
  issue time, so it changes on every reissue. An escalation counts as new; a
  broad notice, a reissue, or a warning that clears and returns doesn't.
  Trade-off: a second "Flood Warning" for another river doesn't count,
  because `detail` is left out of the key (a reworded reissue would
  otherwise count).
- **Cadence rides on `/alert.json`.** In radar mode it adds `mode` and
  `min_gap_s`, and the id gains `radar:<frame time>`. `min_gap_s` is 240
  with rain within 64 km of home (under BOM's ~5-minute cadence, so every
  new frame draws), otherwise 600. The frame clamps it to 240–600 and treats
  anything else as 600. A change of mode redraws at once. **Roughly the
  same frame waits:** the radar part of the id changes only when the
  counted rain near home arrives or leaves, its nearest pixel moves more
  than 5 km, its area changes by more than 25%, or its heaviest band (blue,
  green, yellow/orange, red; bands, because adjacent levels flicker inside a
  storm) changes, measured against the last frame drawn; the first check in
  a new hour re-baselines, as for facts. In normal mode
  `/alert.json` answers exactly as it did before radar mode existed.
- **Battery never stays awake for it.** On battery the radar shows at the
  usual hourly wakes; the fast cadence needs USB power.
- **Fetched just in time**: a background thread asks the provider once a
  minute only while the mode is on, and the provider lists FTP only when the
  next frame is due. Details in [data-sources.md](data-sources.md#rain-radar).
- **Drawn in fixed inks, no diffusion**, so a frame looks the same every
  time and nothing shimmers between redraws: light rain is a pale blue
  stipple, then blue, green, yellow, orange, red, and red/black for the two
  extreme levels; mixed levels use a fixed 4×4 ordered stipple. White land
  and sea, green coast and range rings, black labels over the rain, a ring
  at home. Topography is left out because any fill fought the light-rain
  stipple.
- **Known gaps**: the 64 km image can't see the far side of the 64 km circle
  around home (home isn't at the radar); fetching IDR713 (128 km) too would
  close it. A USB-powered frame redraws at about 00:0x when the mode ends at
  midnight. USB-push mode has no `min_gap_s`. Button C was confirmed on the real
  board on 18 Nov 2026: a press switched modes and drew the radar poster.

## 11. Render on the server; the frame draws an indexed PNG

**Decision.** A Python/Pillow renderer in docker composes the whole 800×480
poster and serves it as a 4-bit indexed PNG whose palette indices are the
panel's pen numbers. The frame decodes it with `pngdec`'s `PNG_COPY`, which
puts each index straight onto its ink, with no re-dithering.

**Why.** The Pico W can't lay out text well or dither a photo. The e-ink
keeps the last picture with no power, so a server outage costs nothing
visible until the stale-data flags appear. A container moves between hosts
unchanged. **Rejected:** drawing everything on the device (plainer, no
photographs).

**Consequences.**
- The frame depends on a LAN service, and the server owns every colour
  decision.
- Photos are dithered with Atkinson diffusion, matched in CIELAB against
  the inks' measured-looking colours (`palette.INK_RGB`).
  Floyd–Steinberg turned dark midtones into red/green confetti, and plain
  RGB distance turned pale skies green.
- `/frame.json` carries a hash and `/frame.png?h=<hash>` serves exactly
  those bytes, even if the hour turns between the two requests. The frame
  skips the 40-second redraw when the hash hasn't changed.
- The poster changes only when its content does, so nothing on it may drift
  for no reason: no clock, AQI as a category rather than a number, stale
  stamps in coarse buckets.

## 12. Signed over-the-air updates

**Decision.** The frame updates its own `almanac_frame.py` from releases
the deploying machine signs. `main.py` is a small boot guard that is never
updated over the air.

**Why.** Once the frame is on the wall, a USB cable for every frame change
is a chore, and a frame that runs whatever the renderer hands it would turn
any compromise of the renderer's host into code on the board.

How it holds together (`scripts/sign_ota.py`, `maybe_update` and
`ota_commit` in `frame/almanac_frame.py`, `frame/main.py`):

- **HMAC-SHA256** over `inky-almanac-ota-v1:<name>:<version>:<sha256>:<size>`.
  The key (64 hex characters) exists in three places only: the signing
  machine (`OTA_KEY_FILE`, 0600), the operator's password manager, and the
  frame (`OTA_KEY.py`). The renderer serves releases but never holds the
  key, so it can't forge one. `deploy-frame.sh --ota-key` refuses to run
  without `--local`, because the remote path would leave the key on the
  renderer's host. No key on the board means OTA is off.
- **A signed version stops replay.** The version is the signing time in
  unix seconds; the frame refuses anything not newer than its committed
  version. A USB install resets the baseline to the install time.
- **Order of install**: one unproven version at a time; skip known-bad
  hashes; check the signature (constant-time compare); download, stopping
  at the promised size; check sha256; record the trial in `/ota.json`
  *before* touching files; keep the current file as `.prev`; swap with
  renames; reset. An update error is logged and never costs the refresh.
- **Commit only after a draw.** The reboot after an install forces a redraw,
  and only a cycle that drew commits the trial. An earlier version
  committed after merely fetching `/frame.json`, which proved nothing about
  the draw path.
- **The guard** restores `.prev` if the app file is missing (a power cut
  mid-swap), ignores a recorded trial whose hash isn't the installed file
  (power went before the swap), counts trial boots, and arms a 30-minute
  one-shot timer during a trial, because a USB-powered board never reboots
  by itself. It rolls back after 3 trial boots without a commit or on an
  import failure. **Bad marking is by attempts**: an import failure marks a
  version bad at once, otherwise it takes 2 failed trials, because one
  failure may have been a network outage, not the code. Any error inside
  the guard waits 60 s and resets; it never drops to the REPL, which would
  need someone with a cable.
- **Power-cut safe** because LittleFS `rename()` replaces atomically, so
  there is never a moment without an `almanac_frame.py`.
- **Scope**: only `almanac_frame.py` updates over the air. `main.py`,
  `config.py`, `WIFI_CONFIG.py` and `OTA_KEY.py` change only over USB. The
  signer refuses uncommitted frame code.

**Consequence.** Every signed release is newer than the last, so signing on
a deploy that didn't change `frame/` still makes the frame download, reboot
and trial-draw: an extra flash for nothing. Sign only when `frame/` changed
([gotchas.md](gotchas.md#deploy-and-ota)).

## 13. Wi-Fi mode and USB mode

Two separate axes:

- **Mode** (`MODE` in `frame/config.py`). In `wifi` mode, the normal one,
  the frame polls the renderer. In `usb` mode, plan B for when the frame's
  network can't reach the renderer, `main.py` starts nothing; the renderer
  (with `ALMANAC_USB_DEVICE` set, `deploy-server.sh --usb`) copies each new
  poster over the serial cable with `mpremote` and calls `usb_show()`. The
  buttons, OTA and radar `min_gap_s` don't apply in USB mode. It was used
  to draw the first poster before the firewall rule existed.
- **Power.** On battery the board powers off between RTC wakes, so it can't
  poll anything between hourly refreshes (battery operation is written but
  unproven on this board). On USB power it stays up: it watches the buttons
  and polls `/alert.json` every 2 minutes, which is what makes fast warning
  redraws and the radar cadence possible, and what exposed the heap and
  timer-wrap bugs in [gotchas.md](gotchas.md#hardware-and-firmware).

## 14. Photos: Commons landscapes

**Decision.** The picture is a Wikimedia Commons *Featured Picture* from 18
nature categories (`CATEGORIES` in `providers/commons.py`: mountains, named
national parks, forests, lakes, waterfalls, volcanoes, landscapes).
**Rejected:** the first version's paintings (Art Institute of Chicago); the
operator wanted natural scenery.

- **Screened for the inks.** `palette.colourful_enough` needs mean
  saturation ≥ 70, mean brightness 70–215 and brightness spread ≥ 40 (0–255
  scales), because misty, grey or night scenes come out as mud on seven
  muted inks. Candidates must also be landscape-ish JPEGs at least
  1200×700. A failed candidate is remembered and skipped.
- **Chosen by a stable hash of the date**, so every render that day shows
  the same photo; button B (or "Different photo") bumps a per-day shuffle
  count.
- **Fetched gently.** The pool refreshes every 14 days in a background
  thread, in batches, with a descriptive User-Agent that names the repo:
  Commons rate-limits generic agents with 429s. Only a 404 or 410 rejects a
  photo for good; a 403 or 429 burst must not burn through the pool. With
  no network the last shown photo is reused.
- **Credited on the poster** from the file's metadata: "Photo: <Artist> ·
  <LicenseShortName>", under a tidied title (catalogue junk such as
  "-- 2014 -- 7056" is stripped). Titles DejaVu can't draw (CJK) come out as
  boxes; featured nature titles are almost all Latin or Cyrillic.

## 15. Hazards and warnings

**Decision.** Hazards come from three adapters: NSW RFS major incidents
(official GeoJSON), NSW SES warnings read from the CAP-AU alerts and
polygons HazardWatch embeds in its own page, and BOM's NSW warnings RSS.
**Rejected:** BOM's JSON API (its responses say "You must not use, copy or
share it"); waiting for SES API access (SES has no open API; the README
names its contact). Consequence: the SES source is fragile, and says so on
the poster when it breaks.

- **Near home** means home inside the warning polygon (0 km) or within
  `ALMANAC_HAZARD_RADIUS_KM` (30) of it, measured to the nearest *edge*:
  a long edge can pass 5 km away between corners 47 km off. RFS "Not
  Applicable" and planned burns are skipped; BOM titles must name a
  configured area and none of the ignore words; BOM final and cancellation
  notices are dropped.
- **Specific versus broad.** Specific hazards pinpoint a threat that asks
  for action: BOM severe thunderstorm, severe weather and flood warnings
  (not watches), and RFS/SES Watch and Act or Emergency Warning. Broad
  ones are notices: flood watches, fire weather and other region-wide BOM
  products, RFS/SES **Advice** (demoted after a live SES storm advice proved
  not worth a redraw), Total Fire Ban, fire danger and air quality. Only
  specific hazards feed the alert id, trigger fast redraws or bring a map.
- **The alert id** is a hash of the specific hazards' identities. BOM's
  identity includes the issue time, so a reissue with a moved storm is a
  change. The renderer re-reads the hazard feeds every 90 s; the frame polls
  `/alert.json` every 2 minutes on USB power.
- **Redraw rules (frame side, `alert_redraw_due`).** The first alert and the
  all-clear redraw at once; any other change (reissues, edge-of-radius
  flicker) at most every 10 minutes, so the panel can't flash itself to
  death. The all-clear was originally under the cap too, which left a
  cancelled warning's map on the wall for up to 10 minutes.
- **Stale after 90 minutes.** An old Advice may have become an Emergency, or
  been cleared. A source with no usable data shows a neutral
  "NO LIVE HAZARD DATA: <source>" badge, so an outage never looks like a
  quiet day. Each source, and each feature within a source, fails on its
  own.
- **Badges** use the Australian Warning System colours (Advice yellow,
  Watch and Act orange, Emergency red; BOM's titled warnings get their own
  white-and-red style). Order: emergencies, other specific hazards, the
  no-data badge, Total Fire Ban, broad notices, fire danger (HIGH and up),
  air quality (US AQI 101+ in the day's remaining hours). At most four (two
  over a map), the last becoming "+N MORE".
- **Fire danger is dated by fetch time.** RFS's "today" and "tomorrow" are
  relative to when the feed was fetched, so after an offline night the
  cached "today" was yesterday's (a stale Total Fire Ban could show). It is
  mapped by fetch date, and a fetch before the first refresh hour counts as
  the previous day's.

## 16. Warning maps

**Decision.** While a specific BOM warning with a map is in force, the
warning's threat-area map replaces the photo.

- **Source.** BOM's detailed warning pages embed the map as a base64 PNG;
  there is no image URL. The detailed severe thunderstorm warning (IDN21035)
  is preferred because it shows the storm cells and their 30-minute track.
  A page that says it is a cancellation yields no map, even if the RSS
  title hasn't caught up.
- **Fixed inks, no dithering** (`dither.map_ink`). BOM draws from a small
  palette, so each colour gets a deliberate ink: the immediate-threat area
  orange, the warning area yellow, severe cells red, town labels (blue on
  BOM's map) and region names black, boundary lines (grey) green, sea solid blue, land white. Labels stay crisp.
  Known coupling: the table is tuned to BOM's map colours.
- **Layout.** The map is fitted whole at its own width (448 px for BOM's
  maps), and the text panel takes the freed space (322 px instead of 270).
  The warnings in force are listed in the text panel in sentence case
  instead of as badges over the map.
- **Cached per issue, stale after 10 minutes.** The cache key is the page
  URL plus the warning's identity, because BOM reuses product pages
  (IDN21035) across events and an old storm track must never sit under a new
  warning. A storm map older than 10 minutes is history, not a forecast.
- **Defensive.** The PNG is checked (signature, a pixel cap, `verify()`)
  before it is cached, and a map that fails to draw falls back to the photo
  rather than taking the poster down.

## 17. The clean, public-safe repository

**Decision (10 Nov 2026).** The project restarted in a new private repo
with everything in a single initial commit (`e6dbf50`). The original
history, PRs #1–#9 included, is in `abradner/inky-almanac-deprecated`.

**Why.** The original history carried the real location, host details and
screenshots of the home area. A fresh single commit gave a repo that is safe
to show; rewriting history was not attempted.

- Defaults became central Sydney; tests, fixtures and stills use it.
- The real values live only in gitignored `frame/config.py`, `deploy.env`
  and `server.env`, each with an `.example`; the scripts read `deploy.env`
  (including `SSH_HOST_KEY_ALIAS` for connecting by IP).
- **Consequences.** PR numbers restart at #1 in this repo, so "#7" is
  ambiguous without the repo name ([history.md](history.md)). And the first
  deploys after the move silently rendered Sydney, because `server.env`
  didn't exist yet: an unset location falls back to the defaults without
  complaint.

## 18. Open questions

- The southerly-change thresholds are a first guess; tune them against real
  events.
- The normals window (1991–2020 vs a more recent span).
- Whether 19 years of central-Sydney UV is representative enough.
- Whether the observed southerly should also come from BOM's paid half-hourly
  history for better normals.
- Button C works on USB power (confirmed 18 Nov 2026). Still unconfirmed:
  that a C press wakes the board on battery.
- Whether to fetch the 128 km radar (IDR713) as well, so "rain within 64 km"
  can see the far side of the circle around home.
- A quieter OTA trial: after an install the frame forces a full redraw to
  prove the draw path, which is a second flash when the poster hasn't
  changed. Proving the decode without a panel refresh was agreed and then
  parked; nothing is built.
- Battery operation (power-off between RTC wakes) is written for but not
  yet proven on this board.
