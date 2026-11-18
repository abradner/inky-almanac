# Data sources

The mechanics of each source the renderer reads: which one wins, how the
station observations, the rain radar and the climate normals work. *Why*
these sources were chosen, and what was learned about them, is in
[design.md](design.md#5-data-sources-and-which-one-wins); the traps are in
[gotchas.md](gotchas.md#data-sources).

## Which source wins

Where more than one source gives a figure, the poster uses the most
authoritative one that has it:

| Rank | Source | Figures it supplies |
|---|---|---|
| 1 | Observed: the BOM station (Horsley Park AWS by default) | rain since 9 am and in the last hour; the coming hour's temperature, from a reading under 30 minutes old; a southerly change it has seen |
| 2 | BOM forecast: the town précis (IDN11060) | the day's max and min, forecast text, icon and chance of rain |
| 3 | Model: Open-Meteo | everything hourly (temperature curve, rain-chance bars, wind, UV), sun times, and any daily figure BOM leaves out |

The model's hourly temperatures are anchored to BOM's figures
(`anchor.py`), so the chart, the coming hour and the "vs normal" fact agree
with the header. The model's peak between 6 am and 9 pm becomes BOM's max
and its trough between midnight and 9 am becomes BOM's min; temperatures in
between map linearly, so the curve keeps the model's timing. With only one
of BOM's figures (BOM drops today's min after the morning), the curve shifts
to match that one. The rain-chance bars stay the model's, because BOM
publishes no free hourly product. The day's rain on the forecast line
("· 80% · 2–8 mm") is BOM's chance of any rain and its range; with no BOM
outlook the line shows no rain figure rather than the model's.

The poster labels none of this: an observed temperature and a forecast one
look the same.

## Station observations

The `bom` observations adapter reads BOM's state 10-minute observations
product over anonymous FTP (`ftp://ftp.bom.gov.au/anon/gen/fwo/IDN60920.xml`
for NSW: every station, latest observation only, about 540 KB). It fetches
at most every 10 minutes, the product's own cadence, backs off (20 minutes,
doubling, at most an hour) when the fetch fails, and keeps a three-hour
rolling log of the configured stations in the cache volume
(`bom-obs-log.json`). Requests read the log only, so rendering never waits
for FTP. Elements are read by their `type` attribute; any of them may be
missing (Fort Denison and Kurnell report wind only).

`observed.py` turns the log into facts, with named constants for every rule:

- A reading older than 30 minutes counts as missing, and the facts fall back
  to the forecast's modelled rain. When the home station is silent, the first
  cross-check station with a fresh reading stands in.
- **Since 9 am** is the gauge's own figure. **Last hour** differences two
  cumulative readings an hour apart (within 10 minutes); across the 9 am reset
  it adds what fell before 9 from the previous period's closing total. A gap
  in the log makes it unknown rather than guessed.
- **A southerly change** at a station: the wind has swung into SE-S-SW (at
  least 45°, at 10 km/h or more) within the last hour, and the temperature
  fell 2° or more or the gust rose 15 km/h or more. A swing alone doesn't
  count. Seen at a leading (coastal) station but not at home, it is a strong
  "Southerly change arriving" fact (score 80, re-checked every 15 minutes),
  and it replaces a forecast southerly change; seen at home it is "Southerly
  change: arrived".

BOM's anonymous FTP products are for personal, non-commercial use (see BOM's
copyright notice). The renderer doesn't use `api.weather.bom.gov.au` (its
responses forbid reuse) and doesn't scrape bom.gov.au pages. Station IDs are
WMO numbers; find others in the product itself (`<station wmo-id=…
description=…>`).

## Rain radar

The `bom` radar adapter (`providers/bom_radar.py`) reads BOM's radar frames
over anonymous FTP, and only in radar mode: a background thread asks it every
minute while the mode is on (and at once when C is pressed), and never
otherwise.

- **Frames**: `ftp://ftp.bom.gov.au/anon/gen/radar/IDR714.T.<UTC stamp>.png`,
  one about every 5 minutes, about 25 kept. The adapter lists only
  `IDR714.T.*` (the full listing is about 1 MB) and only once the next frame
  is due: the newest stamp plus the spacing the listing shows, plus a minute
  for BOM to publish it. A late frame is looked for at most once a minute;
  failures back off from 2 minutes, doubling, to 15.
- **The map underneath** comes from separate transparent layers in
  `/anon/gen/radar_transparencies/` (background, topography, range, locations),
  fetched once and re-checked weekly. A frame holds only the rain, a grey
  copyright bar along the top and a line of text along the bottom, so nothing
  is subtracted.
- **Rain colours** are BOM's rain-rate scale, matched by RGB (palette indices
  vary between frames) to levels 1-15, light to extreme. Levels 1-13 come from
  real frames' palettes; 14 and 15 from BOM's legend. `radar_poster.RAIN_INKS`
  maps each level to an ink or a 4x4 ordered stipple of two inks.
- **Geometry**: the radar site is the image centre, 0.25 km per pixel at the
  64 km range (verified from the range rings and town dots). "Rain within
  64 km" counts rain pixels inside that circle around home; the part of the
  circle off the image (home is not at the radar) can't be seen. Fewer than 12
  rain pixels count as clutter, not rain.
- **The poster** asks the frame for a faster redraw through `/alert.json`:
  in radar mode it carries `mode` and `min_gap_s` (240 with rain near home,
  600 otherwise), and the alert id gains `radar:<frame time>`. The frame clamps
  `min_gap_s` to 4-10 minutes and ignores anything else. In normal mode
  `/alert.json` answers exactly as before.
- **The mode** is kept in the cache volume (`radar-mode.json`), so it survives
  a restart. `POST /mode?set=radar|normal` changes it.

## Climate normals

`providers/climate.py` answers "what does this hour usually look like here?", for
facts like "4° warmer than a normal November 3 pm". Every cell is a
(month, hour) of local clock time, daylight saving included.

```python
from almanac.providers.climate import NORMALS
NORMALS.temp(11, 15)            # TempNormal(mean, std, p10, p50, p90)
NORMALS.daily_max(1)            # normal daily maximum
NORMALS.wind(9, 14)             # WindNormal(p50, p90, gust_p50, gust_p90, sectors)
NORMALS.rain(2, 16)             # RainNormal(p_wet, wet_mean, wet_p90)
NORMALS.uv(1, 13)               # UVNormal(mean, p90)
NORMALS.southerly_rate(12, 17)  # chance of a southerly change in that hour
NORMALS.temp_percentile(11, 15, 31.0)
NORMALS.normal_max(month, hour=None), NORMALS.normal_uv(month, hour=None)  # the fact scorer's calls
```

- **Temperature, wind and rain** are computed at runtime from Open-Meteo's
  ERA5 archive (`era5_seamless`, 1991-2020) at `ALMANAC_LAT`/`ALMANAC_LON`.
  The renderer pulls 30 yearly chunks in the background on first start (about
  2.5 minutes, 30 requests), writes `climate-normals.json` to the cache volume,
  and rebuilds it once a year or when the location changes. Until then, or if
  a build fails (it retries every 6 hours, resuming from the years it already
  has), a rough central-Sydney placeholder answers. Nothing is committed,
  because the location is private.
- **Rain**: an hour is wet from 0.1 mm, because ERA5 drizzles far too often.
  `p_wet` is the chance of a wet hour; `wet_mean` and `wet_p90` are over wet
  hours only.
- **Southerly changes** are a reanalysis proxy: the wind swings from N, NE,
  NW or W to S, SE or SW between two consecutive hours and blows at least
  20 km/h afterwards. A 25 km grid smooths real changes, which often take
  more than an hour to swing or arrive between grid points, so this
  under-counts them badly (about 0.2-0.9 a month at Observatory Hill) and
  mistimes them. Treat it as relative, month against month.
- **Daily max/min** are from hourly values, so they sit a little inside a
  thermometer's true extremes.
- **UV** is ARPANSA's measured 1-minute UV index for Sydney, 2007-2025,
  averaged to hourly means (`uv(month, hour)`), plus each day's peak reading
  (`uv_peak(month)`, which is what `normal_uv(month)` returns). It describes
  a public site, so the table is committed; rebuild it with
  `tools/build_uv_normals.py` (about 400 MB of downloads, cached under
  `cache/`). UV observations courtesy of ARPANSA.

## Credits

Photos: Wikimedia Commons Featured Pictures, each under its own licence and
credited on the poster. Weather: the Bureau of Meteorology's public forecast,
warning, observation and radar products, and [Open-Meteo](https://open-meteo.com)
(CC BY 4.0), including its ERA5 archive for the climate normals. UV
observations courtesy of ARPANSA (CC BY 2.5 AU). Hazards: NSW RFS, and NSW SES
via HazardWatch. Fonts: DejaVu.
