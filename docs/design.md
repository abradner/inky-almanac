# Design notes: why the poster is the way it is

This file records the reasoning behind the poster's information design, data
sources and refresh behaviour. It is here so a later change, by a person or an
agent, does not quietly undo a decision whose reason is not visible in the
code. If you change one of these behaviours, update the note and say why.

The interactive mockup the side panel was designed in ("Inky Panel Lab", a
private claude.ai artifact) has worst-case presets and a clipping detector. The
renderer's own worst-case tests (`server/tests/panel_fixtures.py`,
`test_panel.py`) are the source of truth now.

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

## 10. Planned: radar mode

Agreed design (in progress on branch `radar`):

- Button C on the picture poster switches to radar mode; C again returns.
- It lasts until midnight, or, after at least 2 hours, until no rain is
  visible on the radar.
- Radar is fetched just in time, only in radar mode, never faster than BOM
  issues frames (about every 5–6 minutes). The 64 km Sydney (Terrey Hills)
  view, with BOM's separately published background layers.
- With rain within 64 km of home, the screen updates on each new frame;
  otherwise about every 10 minutes.
- Warning badges stay on. A *new* warning switches back to normal mode to show
  the warning map.
- Rain colours map to inks: pale blue for light rain, yellow, orange and red
  for heavy.
- Needs a frame change (button C, a server-set minimum redraw gap), so it
  ships as a server deploy plus a signed OTA.

## 11. Open questions

- The southerly-change thresholds are a first guess; tune them against real
  events.
- The normals window (1991–2020 vs a more recent span).
- Whether 19 years of central-Sydney UV is representative enough.
- Whether the observed southerly should also come from BOM's paid half-hourly
  history for better normals.
