"""Climate normals: what the weather usually does here, by month and hour.

A fact scorer compares a forecast against these ("4 degrees above normal for
3 pm in November"). Every cell is keyed by (month 1-12, hour 0-23) of local
*clock* time, daylight saving included, because that is how the poster reads.

Two tables feed one `Normals` object:

- **Temperature, wind, rain and southerly changes** come from Open-Meteo's
  ERA5 archive (`era5_seamless`, 1991-2020) at the configured location. They
  are computed at runtime, never committed: the location is private, and so
  is anything computed for its grid cell. `start_background_build` pulls 30
  yearly chunks, politely and in the background, into `climate-normals.json`
  in the cache volume, and rebuilds it when it is about a year old or the
  location changes. Until then, or if the build fails, `PLACEHOLDER` (rough
  central-Sydney monthly values) answers instead. Rendering never waits.
- **UV** comes from ARPANSA's measured 1-minute UV index for Sydney, averaged
  to hourly means (`data/uv_normals.json`, built by `tools/build_uv_normals.py`
  and committed, since it describes a public site). UV observations courtesy
  of ARPANSA.

`NORMALS` is the object the rest of the server holds. Its `normal_max` and
`normal_uv` are the two calls the fact scorer was written against.
"""

import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"
MODEL = "era5_seamless"
FIRST_YEAR, LAST_YEAR = 1991, 2020  # the WMO 1991-2020 normals period
HOURLY_VARS = ("temperature_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "precipitation")
SCHEMA = 1

CACHE_NAME = "climate-normals.json"
CHUNK_DIR = "climate-normals-chunks"  # per-year pulls, so a failed build resumes
MAX_AGE_S = 365 * 86400  # rebuild a year-old table
RETRY_AFTER_S = 6 * 3600  # after a failed build
RECHECK_S = 24 * 3600  # how often the background thread looks at the age
REQUEST_GAP_S = 3.0  # between yearly requests: sequential and unhurried
ATTEMPTS = 5

WET_MM = 0.1  # ERA5 drizzles: an hour is wet only from 0.1 mm
DAY_MIN_HOURS = 20  # a local day needs this many hours to give a max/min
SOUTHERLY_MIN_KMH = 20.0

SECTORS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
_BEFORE_SOUTHERLY = {"N", "NE", "NW", "W"}
_SOUTHERLY = {"S", "SE", "SW"}

UV_CREDIT = "UV observations courtesy of ARPANSA"
UV_PATH = Path(__file__).parent / "data" / "uv_normals.json"


# ------------------------------------------------------------------ statistics


def percentile(values, p):
    """The p-th percentile (0-100) by linear interpolation between closest
    ranks (numpy's default). None for no values."""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    lo = math.floor(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def sector(degrees):
    """8-point compass sector a wind blows *from*; N is 337.5-22.5 degrees."""
    return SECTORS[int(((degrees % 360) + 22.5) // 45) % 8]


def is_southerly_change(prev, cur):
    """A reanalysis proxy for a southerly change: the wind swings from a
    N/NE/NW/W sector to S/SE/SW between two consecutive hours, and blows at
    least SOUTHERLY_MIN_KMH after the swing. `prev`/`cur` are Obs one hour
    apart (the caller checks the spacing)."""
    if prev is None or prev.wind_dir is None or cur.wind_dir is None or cur.wind is None:
        return False
    return (
        sector(prev.wind_dir) in _BEFORE_SOUTHERLY
        and sector(cur.wind_dir) in _SOUTHERLY
        and cur.wind >= SOUTHERLY_MIN_KMH
    )


def to_local(naive, utc_offset_s, tz):
    """A timestamp labelled at a fixed UTC offset, as local clock time.

    Open-Meteo labels a whole request with ONE offset (`utc_offset_seconds`;
    observed to be the zone's offset when the request is made, not when the
    data is from), so a pull made in summer labels winter hours in daylight
    time, and one made in winter labels summer hours in standard time. NASA POWER's LST and ARPANSA's CSVs are standard time all
    year. Going through UTC fixes all three."""
    utc = (naive - timedelta(seconds=utc_offset_s)).replace(tzinfo=timezone.utc)
    return utc.astimezone(tz)


@dataclass(frozen=True)
class Obs:
    """One hourly observation; `utc` is aware."""

    utc: datetime
    temp: float = None
    wind: float = None  # km/h
    wind_dir: float = None  # degrees, from
    gust: float = None  # km/h
    rain: float = None  # mm over the hour


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


class _Cell:
    __slots__ = ("n", "temp", "wind", "gust", "rain", "wet", "dirs", "southerly")

    def __init__(self):
        self.n = 0
        self.temp, self.wind, self.gust, self.rain, self.wet = [], [], [], [], []
        self.dirs = Counter()
        self.southerly = 0


def compute(observations, tz):
    """The ERA5 normals table from chronological hourly Obs."""
    tz = ZoneInfo(tz) if isinstance(tz, str) else tz
    cells = [[_Cell() for _ in range(24)] for _ in range(12)]
    days = {}  # local date -> [max, min, hours]
    month_years = set()
    events = Counter()  # month -> southerly changes over the whole period
    prev = None
    for o in observations:
        local = o.utc.astimezone(tz)
        c = cells[local.month - 1][local.hour]
        c.n += 1
        month_years.add((local.year, local.month))
        if o.temp is not None:
            c.temp.append(o.temp)
            d = days.setdefault(local.date(), [o.temp, o.temp, 0])
            d[0], d[1], d[2] = max(d[0], o.temp), min(d[1], o.temp), d[2] + 1
        if o.wind is not None:
            c.wind.append(o.wind)
        if o.gust is not None:
            c.gust.append(o.gust)
        if o.wind_dir is not None:
            c.dirs[sector(o.wind_dir)] += 1
        if o.rain is not None:
            c.rain.append(o.rain)
            if o.rain >= WET_MM:
                c.wet.append(o.rain)
        if prev is not None and o.utc - prev.utc == timedelta(hours=1) and is_southerly_change(prev, o):
            c.southerly += 1
            events[local.month] += 1
        prev = o

    hourly = []
    for m in range(12):
        row = []
        for c in cells[m]:
            ndir = sum(c.dirs.values())
            row.append(
                {
                    "n": c.n,
                    "temp": {
                        "mean": _r(_mean(c.temp)),
                        "std": _r(_std(c.temp)),
                        "p10": _r(percentile(c.temp, 10)),
                        "p50": _r(percentile(c.temp, 50)),
                        "p90": _r(percentile(c.temp, 90)),
                    },
                    "wind": {
                        "p50": _r(percentile(c.wind, 50)),
                        "p90": _r(percentile(c.wind, 90)),
                        "gust_p50": _r(percentile(c.gust, 50)),
                        "gust_p90": _r(percentile(c.gust, 90)),
                        "sectors": {s: _r(c.dirs[s] / ndir, 4) if ndir else 0.0 for s in SECTORS},
                    },
                    "rain": {
                        "p_wet": _r(len(c.wet) / len(c.rain), 4) if c.rain else None,
                        "wet_mean": _r(_mean(c.wet)),
                        "wet_p90": _r(percentile(c.wet, 90)),
                    },
                    "southerly": _r(c.southerly / c.n, 5) if c.n else 0.0,
                }
            )
        hourly.append(row)

    daily = []
    for m in range(1, 13):
        full = [d for day, d in days.items() if day.month == m and d[2] >= DAY_MIN_HOURS]
        maxes, mins = [d[0] for d in full], [d[1] for d in full]
        daily.append(
            {
                "days": len(full),
                "max": {"mean": _r(_mean(maxes)), "p10": _r(percentile(maxes, 10)), "p90": _r(percentile(maxes, 90))},
                "min": {"mean": _r(_mean(mins)), "p10": _r(percentile(mins, 10)), "p90": _r(percentile(mins, 90))},
            }
        )
    years = Counter(m for _, m in month_years)
    per_month = [_r(events[m] / years[m], 3) if years[m] else 0.0 for m in range(1, 13)]
    return {"schema": SCHEMA, "hourly": hourly, "daily": daily, "southerly_per_month": per_month}


# ------------------------------------------------------------------ UV table


def uv_hours(samples, tz, min_minutes=45, peaks=None):
    """Mean UV per (local date, clock hour) from (aware datetime, uv) samples,
    keeping hours with at least `min_minutes` readings. With a `peaks` dict,
    also records each local date's highest reading and reading count there
    (date -> [max, n])."""
    tz = ZoneInfo(tz) if isinstance(tz, str) else tz
    acc = {}
    for when, uv in samples:
        local = when.astimezone(tz)
        a = acc.setdefault((local.date(), local.hour), [0.0, 0])
        a[0] += uv
        a[1] += 1
        if peaks is not None:
            p = peaks.setdefault(local.date(), [0.0, 0])
            p[0], p[1] = max(p[0], uv), p[1] + 1
    return {k: s / n for k, (s, n) in acc.items() if n >= min_minutes}


def uv_table(hour_means, meta, day_peaks=None):
    """The committed UV table: mean and p90 of hourly means per (month, hour),
    and per month the median and p90 of each day's peak reading (what a
    forecast's daily UV maximum is closest to)."""
    cells = [[[] for _ in range(24)] for _ in range(12)]
    for (day, hour), v in hour_means.items():
        cells[day.month - 1][hour].append(v)
    hourly = [
        [{"n": len(c), "mean": _r(_mean(c)), "p90": _r(percentile(c, 90))} for c in row] for row in cells
    ]
    months = [[] for _ in range(12)]
    for day, peak in (day_peaks or {}).items():
        months[day.month - 1].append(peak)
    peak = [{"days": len(v), "p50": _r(percentile(v, 50)), "p90": _r(percentile(v, 90))} for v in months]
    return {"schema": SCHEMA, **meta, "daily_peak": peak, "hourly": hourly}


def parse_arpansa_csv(lines, utc_offset_s=10 * 3600):
    """(aware UTC datetime, uv) from an ARPANSA 1-minute CSV.

    Columns are Date-Time,Lat,Lon,UV_Index; timestamps are Australian
    Eastern *Standard* Time all year (every day has 1440 rows, and the
    summer peak sits at solar noon ~12:05, not 13:05). Blank, unparseable
    and clearly negative readings are skipped; tiny negatives are zero."""
    for line in lines:
        parts = line.strip().lstrip("﻿").split(",")
        if len(parts) < 4 or not parts[0][:1].isdigit():
            continue
        try:
            naive = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
            uv = float(parts[3])
        except ValueError:
            continue
        if math.isnan(uv) or uv < -0.5:
            continue
        yield (naive - timedelta(seconds=utc_offset_s)).replace(tzinfo=timezone.utc), max(0.0, uv)


def parse_power(payload, longitude, parameter="ALLSKY_SFC_UV_INDEX"):
    """(aware UTC datetime, uv) from a NASA POWER hourly point response with
    time-standard=LST. POWER's LST is UTC + round(longitude / 15) hours,
    with no daylight saving."""
    offset = round(longitude / 15) * 3600
    fill = payload.get("header", {}).get("fill_value", -999.0)
    for stamp, v in payload["properties"]["parameter"][parameter].items():
        if v is None or v == fill or v < 0:
            continue
        naive = datetime.strptime(stamp, "%Y%m%d%H")
        yield (naive - timedelta(seconds=offset)).replace(tzinfo=timezone.utc), float(v)


# ------------------------------------------------------------------ placeholder

# Rough central-Sydney monthly values (Observatory Hill climatology, rounded),
# for until the real table is built. January first.
_PH_MAX = (26.0, 26.0, 24.8, 22.6, 20.0, 17.6, 17.0, 18.4, 20.6, 22.5, 23.8, 25.4)
_PH_MIN = (19.5, 19.7, 18.4, 15.4, 12.4, 10.1, 8.8, 9.6, 11.8, 14.0, 16.2, 18.0)
_PH_WET = (0.09, 0.10, 0.10, 0.09, 0.08, 0.09, 0.07, 0.06, 0.06, 0.07, 0.08, 0.08)
_PH_WIND = (14, 13, 12, 12, 12, 13, 14, 15, 15, 15, 15, 14)  # p50 km/h
_PH_SOUTHERLY = (2.5, 1.8, 1.2, 0.6, 0.3, 0.2, 0.2, 0.4, 0.8, 1.6, 2.4, 2.8)  # per month
_PH_UV_PEAK = (11.5, 10.5, 8.5, 6.0, 3.8, 2.8, 3.0, 4.2, 6.0, 8.0, 10.0, 11.0)


def _diurnal(hour):
    """0 at 05:00, 1 at 15:00: a cosine up over 10 hours, down over 14."""
    x = (hour - 5) % 24
    if x <= 10:
        return 0.5 - 0.5 * math.cos(math.pi * x / 10)
    return 0.5 + 0.5 * math.cos(math.pi * (x - 10) / 14)


def placeholder_table():
    hourly = []
    for m in range(12):
        row = []
        lo, hi, w = _PH_MIN[m], _PH_MAX[m], _PH_WIND[m]
        for h in range(24):
            t = round(lo + (hi - lo) * _diurnal(h), 1)
            row.append(
                {
                    "n": 0,
                    "temp": {"mean": t, "std": 3.0, "p10": round(t - 3.8, 1), "p50": t, "p90": round(t + 3.8, 1)},
                    "wind": {"p50": w, "p90": w * 2, "gust_p50": w * 1.8, "gust_p90": w * 3, "sectors": {s: 0.125 for s in SECTORS}},
                    "rain": {"p_wet": _PH_WET[m], "wet_mean": 0.8, "wet_p90": 2.0},
                    # the month's changes, spread over 12:00-17:59 of 30 days
                    "southerly": round(_PH_SOUTHERLY[m] / 30 / 6, 5) if 12 <= h <= 17 else 0.0,
                }
            )
        hourly.append(row)
    daily = [
        {"days": 0, "max": {"mean": _PH_MAX[m], "p10": None, "p90": None}, "min": {"mean": _PH_MIN[m], "p10": None, "p90": None}}
        for m in range(12)
    ]
    return {
        "schema": SCHEMA,
        "source": "placeholder: rough central-Sydney monthly values",
        "hourly": hourly,
        "daily": daily,
        "southerly_per_month": list(_PH_SOUTHERLY),
    }


def placeholder_uv_table():
    """Only if the committed UV table is unreadable: a bell around solar
    noon (13:00 clock time in daylight-saving months, 12:00 otherwise),
    scaled to a rough monthly peak."""
    hourly = []
    for m in range(12):
        noon = 13.0 if m in (0, 1, 2, 9, 10, 11) else 12.0
        row = []
        for h in range(24):
            x = (h + 0.5 - noon) / 6.0
            v = round(_PH_UV_PEAK[m] * max(0.0, 1 - x * x) ** 2, 2)
            row.append({"n": 0, "mean": v, "p90": round(v * 1.15, 2)})
        hourly.append(row)
    return {"schema": SCHEMA, "source": "placeholder", "hourly": hourly}


PLACEHOLDER = placeholder_table()


# ------------------------------------------------------------------ the interface


@dataclass(frozen=True)
class TempNormal:
    mean: float
    std: float
    p10: float
    p50: float
    p90: float


@dataclass(frozen=True)
class WindNormal:
    p50: float  # km/h
    p90: float
    gust_p50: float
    gust_p90: float
    sectors: dict  # "N".."NW" -> share of hours


@dataclass(frozen=True)
class RainNormal:
    p_wet: float  # chance the hour has >= WET_MM
    wet_mean: float  # mm, over wet hours only
    wet_p90: float


@dataclass(frozen=True)
class UVNormal:
    mean: float
    p90: float


@dataclass(frozen=True)
class DailyNormal:
    max_mean: float
    min_mean: float
    max_p90: float = None
    min_p10: float = None


def _check(month, hour=None):
    if not 1 <= month <= 12:
        raise ValueError(f"month {month} not in 1..12")
    if hour is not None and not 0 <= hour <= 23:
        raise ValueError(f"hour {hour} not in 0..23")


class Normals:
    """Normals for one location. Months are 1-12, hours 0-23 local clock
    time. `placeholder` is True when the ERA5 table isn't built yet."""

    def __init__(self, table, uv_table=None, placeholder=False):
        self._t = table
        self._uv = uv_table or placeholder_uv_table()
        self.placeholder = placeholder
        self.source = table.get("source", "")
        self.uv_source = self._uv.get("source", "")

    def _cell(self, month, hour):
        _check(month, hour)
        return self._t["hourly"][month - 1][hour]

    def temp(self, month, hour):
        return TempNormal(**self._cell(month, hour)["temp"])

    def wind(self, month, hour):
        return WindNormal(**self._cell(month, hour)["wind"])

    def rain(self, month, hour):
        return RainNormal(**self._cell(month, hour)["rain"])

    def southerly_rate(self, month, hour):
        """Chance (0-1) that a southerly change arrives in this clock hour on
        a given day of this month (reanalysis proxy: see is_southerly_change;
        it likely under-counts and mistimes real changes)."""
        return self._cell(month, hour)["southerly"]

    def southerly_per_month(self, month):
        """Mean number of southerly changes in this month, per year."""
        _check(month)
        return self._t["southerly_per_month"][month - 1]

    def daily(self, month):
        _check(month)
        d = self._t["daily"][month - 1]
        return DailyNormal(d["max"]["mean"], d["min"]["mean"], d["max"].get("p90"), d["min"].get("p10"))

    def daily_max(self, month):
        """Normal (mean) daily maximum, from hourly values (so a little under
        a thermometer's true maximum)."""
        return self.daily(month).max_mean

    def daily_min(self, month):
        return self.daily(month).min_mean

    def uv(self, month, hour):
        _check(month, hour)
        return UVNormal(**{k: self._uv["hourly"][month - 1][hour][k] for k in ("mean", "p90")})

    def uv_peak(self, month):
        """The normal daily peak UV for the month: the median of each day's
        highest reading (or, for a table without peaks, the highest hourly
        mean)."""
        _check(month)
        peaks = self._uv.get("daily_peak")
        if peaks and peaks[month - 1].get("p50") is not None:
            return peaks[month - 1]["p50"]
        return max(c["mean"] or 0.0 for c in self._uv["hourly"][month - 1])

    # -------- percentile helpers

    def temp_z(self, month, hour, value):
        """Standard score of a temperature against this cell."""
        t = self.temp(month, hour)
        return (value - t.mean) / t.std if t.std else 0.0

    def temp_percentile(self, month, hour, value):
        """Approximate percentile (0-100) of a temperature in this cell,
        assuming a normal distribution about the cell mean."""
        return 50.0 * (1.0 + math.erf(self.temp_z(month, hour, value) / math.sqrt(2)))

    # -------- the fact scorer's interface (see facts.py on the panel branch)

    def normal_max(self, month, hour=None):
        """Normal daily maximum for the month; with an hour, the normal
        (mean) temperature at that clock hour."""
        return self.daily_max(month) if hour is None else self.temp(month, hour).mean

    def normal_uv(self, month, hour=None):
        """Normal UV at that clock hour (mean of hourly means); without an
        hour, the normal daily peak (uv_peak), to set beside a forecast's
        daily UV maximum."""
        return self.uv_peak(month) if hour is None else self.uv(month, hour).mean


# ------------------------------------------------------------------ loading


def load_uv_table(path=UV_PATH):
    try:
        t = json.loads(Path(path).read_text())
        if t.get("schema") != SCHEMA or len(t["hourly"]) != 12:
            raise ValueError("unexpected UV table")
        return t
    except (OSError, ValueError, KeyError) as e:
        print(f"climate: UV table unusable ({e!r}); using placeholder UV", flush=True)
        return None


def _location(cfg):
    return {"lat": round(cfg.latitude, 4), "lon": round(cfg.longitude, 4), "tz": cfg.timezone}


def read_cache(cfg):
    """The cached ERA5 table, or None if missing, unreadable, from another
    schema or for another location."""
    path = cfg.cache_dir / CACHE_NAME
    try:
        t = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if t.get("schema") != SCHEMA or t.get("requested") != _location(cfg) or len(t.get("hourly", ())) != 12:
        return None
    return t


def needs_build(cfg, now=None):
    now = time.time() if now is None else now
    t = read_cache(cfg)
    return t is None or now - t.get("built_at", 0) > MAX_AGE_S


def load(cfg, uv_path=UV_PATH):
    """Normals from the cache when it exists, the placeholder otherwise.
    A stale cache is still used while its replacement builds."""
    uv = load_uv_table(uv_path)
    t = read_cache(cfg)
    if t is None:
        return Normals(PLACEHOLDER, uv, placeholder=True)
    return Normals(t, uv)


# ------------------------------------------------------------------ building


def _url(cfg, year):
    q = {
        "latitude": cfg.latitude,
        "longitude": cfg.longitude,
        "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31",
        "hourly": ",".join(HOURLY_VARS),
        "models": MODEL,
        "timezone": cfg.timezone,
        "wind_speed_unit": "kmh",
    }
    return ARCHIVE_API + "?" + urllib.parse.urlencode(q)


def _http_json(cfg):
    def fetch(url):
        req = urllib.request.Request(url, headers={"User-Agent": cfg.user_agent})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    return fetch


def _with_retry(fetch, url, sleep, attempts=ATTEMPTS):
    """Retry transient failures with exponential backoff (10 s, 30 s, 90 s,
    270 s). A 4xx other than 429 is our fault: no retry."""
    for i in range(attempts):
        try:
            return fetch(url)
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code != 429 or i == attempts - 1:
                raise
            print(f"climate: HTTP {e.code}; retrying", flush=True)
        except Exception as e:
            if i == attempts - 1:
                raise
            print(f"climate: fetch failed ({e!r}); retrying", flush=True)
        sleep(10 * 3**i)
    raise RuntimeError("unreachable")


def check_payload(payload):
    """Raise unless an archive response has every series at full length."""
    h = payload["hourly"]
    n = len(h["time"])
    if n < 8000:  # a year is 8760/8784 hours
        raise ValueError(f"short archive response ({n} hours)")
    for v in HOURLY_VARS:
        if len(h[v]) != n:
            raise ValueError(f"{v} has {len(h[v])} values for {n} hours")
    if sum(x is None for x in h["temperature_2m"]) > n // 10:
        raise ValueError("archive response is mostly empty")
    int(payload["utc_offset_seconds"])


def observations(payload):
    """Obs (UTC) from one archive response."""
    h = payload["hourly"]
    off = int(payload["utc_offset_seconds"])
    for i, t in enumerate(h["time"]):
        utc = (datetime.fromisoformat(t) - timedelta(seconds=off)).replace(tzinfo=timezone.utc)
        yield Obs(
            utc=utc,
            temp=h["temperature_2m"][i],
            wind=h["wind_speed_10m"][i],
            wind_dir=h["wind_direction_10m"][i],
            gust=h["wind_gusts_10m"][i],
            rain=h["precipitation"][i],
        )


def build(cfg, fetch=None, sleep=time.sleep, now=None, years=range(FIRST_YEAR, LAST_YEAR + 1)):
    """Pull the archive year by year, compute, and write the cache. Years
    already pulled for this location (by an earlier, failed build) are
    reused, not fetched again. Returns the table."""
    fetch = fetch or _http_json(cfg)
    chunks = cfg.cache_dir / CHUNK_DIR
    chunks.mkdir(parents=True, exist_ok=True)
    loc = _location(cfg)
    started = time.monotonic()
    payloads = []
    fetched = 0
    for year in years:
        path = chunks / f"{year}.json"
        payload = None
        if path.exists():
            try:
                saved = json.loads(path.read_text())
                if saved.get("requested") == loc:
                    payload = saved["payload"]
                    check_payload(payload)
            except (ValueError, KeyError):
                payload = None
        if payload is None:
            if fetched:
                sleep(REQUEST_GAP_S)
            payload = _with_retry(fetch, _url(cfg, year), sleep)
            check_payload(payload)
            fetched += 1
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"requested": loc, "payload": payload}))
            tmp.replace(path)
        payloads.append(payload)

    table = compute((o for p in payloads for o in observations(p)), cfg.timezone)
    first = payloads[0]
    table.update(
        {
            "source": f"Open-Meteo archive, {MODEL}, {FIRST_YEAR}-{LAST_YEAR}",
            "period": [min(years), max(years)],
            "grid": {"lat": first.get("latitude"), "lon": first.get("longitude")},
            "requested": loc,
            "built_at": time.time() if now is None else now,
            "build_seconds": round(time.monotonic() - started, 1),
            "requests": fetched,
        }
    )
    path = cfg.cache_dir / CACHE_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(table, separators=(",", ":")))
    tmp.replace(path)
    for p in chunks.glob("*.json"):
        p.unlink()
    try:
        chunks.rmdir()
    except OSError:
        pass
    return table


_build_lock = threading.Lock()


def build_if_needed(cfg, **kw):
    """Build when the cache is missing, stale or for another place. Returns
    True if a build ran. Only one build at a time."""
    if not needs_build(cfg, kw.get("now")):
        return False
    if not _build_lock.acquire(blocking=False):
        return False
    try:
        print("climate: building normals in the background", flush=True)
        t = build(cfg, **kw)
        print(f"climate: normals built in {t['build_seconds']} s ({t['requests']} requests)", flush=True)
        return True
    finally:
        _build_lock.release()


def start_background_build(cfg):
    """A daemon thread that keeps the normals cache built, never blocking a
    render: it checks at once, then daily; after a failure it waits
    RETRY_AFTER_S."""

    def run():
        while True:
            wait = RECHECK_S
            try:
                build_if_needed(cfg)
            except Exception as e:
                print(f"climate: normals build failed ({e!r}); placeholder stays", flush=True)
                wait = RETRY_AFTER_S
            time.sleep(wait)

    t = threading.Thread(target=run, name="climate-normals", daemon=True)
    t.start()
    return t


# ------------------------------------------------------------------ the shared object


class LiveNormals:
    """Normals that follow the cache: each call answers from the newest
    table on disk (rereading only when the file changes), and from the
    placeholder until one exists. Construct with a Config, or let it make
    the default one on first use."""

    _API = (
        "temp", "wind", "rain", "uv", "uv_peak", "daily", "daily_max", "daily_min",
        "southerly_rate", "southerly_per_month", "temp_z", "temp_percentile",
        "normal_max", "normal_uv", "placeholder", "source", "uv_source",
    )

    def __init__(self, cfg=None, uv_path=UV_PATH):
        self._cfg = cfg
        self._uv_path = uv_path
        self._lock = threading.Lock()
        self._stamp = object()
        self._normals = None

    def current(self):
        if self._cfg is None:
            from .config import Config

            self._cfg = Config()
        path = self._cfg.cache_dir / CACHE_NAME
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        with self._lock:
            if stamp != self._stamp or self._normals is None:
                self._normals = load(self._cfg, self._uv_path)
                self._stamp = stamp
            return self._normals

    def __getattr__(self, name):
        if name in LiveNormals._API:
            return getattr(self.current(), name)
        raise AttributeError(name)


NORMALS = LiveNormals()
